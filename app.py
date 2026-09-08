# Copyright IBM Corp. All Rights Reserved.
# #
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Optional
import time
import traceback

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from config import settings
from token_utils import decode_unverified, verify_id_token
from rar_builder import build_agent_authorization_details
from verify_oauth import (
    build_login_url,
    exchange_auth_code,
    get_actor_token,
    token_exchange,
)
from course_api import call_course_api
from llm_agent import decide_action
from verify_directory import find_verify_user


app = FastAPI(title="UC1 Conversational Agent with IBM Verify")
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
templates = Jinja2Templates(directory="templates")


ALLOWED_ACTIONS = {
    "list_available_courses",
    "enroll_course",
    "list_enrolled_courses",
    "delete_course_history",
}


def is_self_reference(value: str | None, logged_in_subject: str) -> bool:
    if not value:
        return True

    value = value.strip().lower()
    return value in {"self", "me", "my", "mine", "myself", logged_in_subject.lower()}


async def resolve_target_subject(
    llm_target_subject: str | None,
    logged_in_subject: str,
) -> tuple[str, dict | None, str]:
    """
    Resolves target subject without hardcoding users.
    - self/me/my/logged-in username => logged-in user
    - any other hint => IBM Verify Directory SCIM lookup
    """
    if is_self_reference(llm_target_subject, logged_in_subject):
        return logged_in_subject, None, "logged_in_subject"

    user_hint = llm_target_subject.strip()
    resolved_user = await find_verify_user(user_hint)

    if not resolved_user:
        raise ValueError(f"No unique IBM Verify user found for hint: {user_hint}")

    resolved_subject = resolved_user.get("userName") or resolved_user.get("displayName") or resolved_user.get("id")
    if not resolved_subject:
        raise ValueError(f"IBM Verify user found but no usable username/id for hint: {user_hint}")

    return resolved_subject, resolved_user, "ibm_verify_directory"


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    subject_tokens = request.session.get("subject_tokens")
    subject_identity = request.session.get("subject_identity") or {}

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logged_in": bool(
                subject_tokens
                and subject_tokens.get("access_token")
                and subject_identity
            ),
            "claims": subject_identity,
            "llm_enabled": settings.use_llm,
            "gemini_model": settings.gemini_model,
        },
    )


@app.get("/login")
async def login(request: Request):
    return RedirectResponse(build_login_url(request))


@app.get("/callback")
async def callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    if error:
        return JSONResponse(status_code=400, content={"error": error, "error_description": error_description})

    if not code or not state:
        return JSONResponse(status_code=400, content={"error": "Missing code or state"})

    try:
        tokens = await exchange_auth_code(request, code, state)

        id_token = tokens.get("id_token")
        if not id_token:
            raise ValueError("IBM Verify token response did not contain an id_token")

        expected_nonce = request.session.get("oauth_nonce")
        if not expected_nonce:
            raise ValueError("Missing OIDC nonce in session")

        id_claims = verify_id_token(
            id_token,
            expected_nonce=expected_nonce,
        )

        # Keep OAuth tokens separate from the authenticated user's identity.
        # The access token is used only as the subject_token during Token Exchange.
        request.session["subject_tokens"] = tokens
        request.session["subject_identity"] = id_claims

        request.session.pop("oauth_state", None)
        request.session.pop("oauth_nonce", None)
        request.session.pop("code_verifier", None)

        return RedirectResponse("/")
    except Exception as exc:
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": "Callback token exchange failed", "details": str(exc)},
        )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


def _format_course_list(courses) -> str:
    if not courses:
        return ""
    lines = []
    for course in courses:
        if isinstance(course, dict):
            cid = course.get("id", "")
            title = course.get("title", "")
            status = course.get("status")
            if status:
                lines.append(f"- {cid} - {title} ({status})")
            else:
                lines.append(f"- {cid} - {title}")
        else:
            lines.append(f"- {course}")
    return "\n".join(lines)


def build_answer(last_result: dict) -> str:
    if last_result.get("status") == "SUCCESS":
        api_result = last_result.get("api_result", {})
        action = last_result.get("action")

        if action == "list_available_courses":
            courses = api_result.get("available_courses") or api_result.get("courses", [])
            if not courses:
                return "No courses are currently available to enroll."
            return "Courses available to enroll:\n" + _format_course_list(courses)

        if action == "enroll_course":
            return api_result.get("message", "Enrollment completed successfully.")

        if action == "list_enrolled_courses":
            courses = api_result.get("enrolled_courses") or api_result.get("courses", [])
            if not courses:
                return "No enrolled courses found."
            return "Enrolled courses:\n" + _format_course_list(courses)

        return "Request completed successfully."

    return (
        last_result.get("error")
        or last_result.get("api_result", {}).get("reason")
        or "Request denied or failed."
    )


@app.post("/chat")
async def chat(request: Request, message: str = Form(...)):
    subject_tokens = request.session.get("subject_tokens")
    id_claims = request.session.get("subject_identity") or {}

    if (
        not subject_tokens
        or not subject_tokens.get("access_token")
        or not id_claims
    ):
        return JSONResponse(
            status_code=401,
            content={"error": "Not logged in. Please login with IBM Verify first."},
        )

    # The OAuth access token is not used to establish the logged-in user's identity.
    # It is kept unchanged and supplied only as the subject_token during Token Exchange.
    subject_token = subject_tokens["access_token"]

    # The authenticated Human User is established from the validated OIDC ID token.
    if id_claims.get("exp") and int(id_claims["exp"]) < int(time.time()):
        request.session.clear()
        return JSONResponse(
            status_code=401,
            content={"error": "Login session expired. Please login again with IBM Verify."},
        )

    logged_in_subject = (
        id_claims.get("preferred_username")
        or id_claims.get("email")
        or id_claims.get("sub")
        or "unknown-user"
    )

    try:
        decision = decide_action(message)

        action = decision.action
        course_id = decision.course_id or "ALL"
        llm_target_subject = decision.target_subject or "self"

        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"Unsupported action from intent classifier: {action}")

        target_subject, resolved_verify_user, target_resolution_source = await resolve_target_subject(
            llm_target_subject=llm_target_subject,
            logged_in_subject=logged_in_subject,
        )

        intent = {
            "action": action,
            "course_id": course_id,
            "llm_target_subject": llm_target_subject,
            "target_subject": target_subject,
            "target_resolution_source": target_resolution_source,
            "resolved_verify_user": resolved_verify_user,
            "reason": decision.reason,
        }

    except Exception as exc:
        traceback.print_exc()
        last_result = {
            "status": "DENIED_OR_FAILED",
            "user_message": message,
            "llm_enabled": settings.use_llm,
            "llm_model": settings.gemini_model if settings.use_llm else "deterministic-fallback",
            "logged_in_subject": logged_in_subject,
            "error": f"Intent classification or IBM Verify user resolution failed: {str(exc)}",
        }
        return JSONResponse({"answer": build_answer(last_result), "diagnostic": last_result})

    authorization_details = [
        build_agent_authorization_details(
            creator=settings.actor_client_id or "course-assistant-agent",
            affected_person=target_subject,
            action=action,
            target_system="course-api",
            resource="courses",
            course_id=course_id,
            logged_in_subject=logged_in_subject,
        )
    ]


    try:
        actor_tokens = await get_actor_token()
        actor_token = actor_tokens["access_token"]

        exchanged_tokens = await token_exchange(
            subject_token=subject_token,
            actor_token=actor_token,
            authorization_details=authorization_details,
        )

        delegated_token = exchanged_tokens["access_token"]

        api_result = call_course_api(
            delegated_token=delegated_token,
            action=action,
            requested_subject=target_subject,
            logged_in_subject=logged_in_subject,
        )

        last_result = {
            "status": "SUCCESS" if api_result.get("allowed") else "DENIED_BY_API",
            "user_message": message,
            "llm_enabled": settings.use_llm,
            "llm_model": settings.gemini_model if settings.use_llm else "deterministic-fallback",
            "llm_intent": intent,
            "logged_in_subject": logged_in_subject,
            "requested_subject": target_subject,
            "action": action,
            "authorization_details": authorization_details,
            "id_token_claims": id_claims,
            "actor_token_claims": decode_unverified(actor_token),
            "delegated_token_claims": decode_unverified(delegated_token),
            "api_result": api_result,
        }

    except Exception as exc:
        traceback.print_exc()
        last_result = {
            "status": "DENIED_OR_FAILED",
            "user_message": message,
            "llm_enabled": settings.use_llm,
            "llm_model": settings.gemini_model if settings.use_llm else "deterministic-fallback",
            "llm_intent": intent,
            "logged_in_subject": logged_in_subject,
            "requested_subject": target_subject,
            "action": action,
            "authorization_details": authorization_details,
            "error": str(exc),
        }

    return JSONResponse({"answer": build_answer(last_result), "diagnostic": last_result})


@app.get("/health")
async def health():
    return {"status": "ok"}
