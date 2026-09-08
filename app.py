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


# Actions the application is willing to carry far enough for IBM Verify
# and the Course API to make the authorization decision.
#
# delete_course_history is intentionally included as a negative-test action:
# the LLM may recognize it, but the Course API has no delete implementation.
ALLOWED_ACTIONS = {
    "list_available_courses",
    "enroll_course",
    "list_enrolled_courses",
    "delete_course_history",
}

UNSUPPORTED_ACTION = "unsupported_action"


def is_self_reference(value: str | None, logged_in_subject: str) -> bool:
    if not value:
        return True

    normalized = value.strip().lower()
    return normalized in {
        "self",
        "me",
        "my",
        "mine",
        "myself",
        logged_in_subject.lower(),
    }


async def resolve_target_subject(
    llm_target_subject: str | None,
    logged_in_subject: str,
) -> tuple[str, dict | None, str]:
    """
    Resolve the target subject.

    - self/me/my/logged-in username -> logged-in user
    - any other hint -> IBM Verify Directory lookup through verify_directory.py
    """
    if is_self_reference(llm_target_subject, logged_in_subject):
        return logged_in_subject, None, "logged_in_subject"

    user_hint = (llm_target_subject or "").strip()
    if not user_hint:
        return logged_in_subject, None, "logged_in_subject"

    resolved_user = await find_verify_user(user_hint)

    if not resolved_user:
        raise ValueError(
            f"No IBM Verify user found for hint: {user_hint}. "
            "Use an existing IBM Verify userName or email address."
        )

    resolved_subject = (
        resolved_user.get("userName")
        or resolved_user.get("displayName")
        or resolved_user.get("id")
    )

    if not resolved_subject:
        raise ValueError(
            f"IBM Verify user was found but no usable userName/displayName/id "
            f"was returned for hint: {user_hint}"
        )

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
        return JSONResponse(
            status_code=400,
            content={
                "error": error,
                "error_description": error_description,
            },
        )

    if not code or not state:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing code or state"},
        )

    try:
        tokens = await exchange_auth_code(request, code, state)

        id_token = tokens.get("id_token")
        if not id_token:
            raise ValueError(
                "IBM Verify token response did not contain an id_token"
            )

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
            content={
                "error": "Callback processing failed",
                "details": str(exc),
            },
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
    status = last_result.get("status")

    if status == "SUCCESS":
        api_result = last_result.get("api_result", {})
        action = last_result.get("action")

        if action == "list_available_courses":
            courses = (
                api_result.get("available_courses")
                or api_result.get("courses", [])
            )
            if not courses:
                return "No courses are currently available to enroll."

            return (
                "Courses available to enroll:\n"
                + _format_course_list(courses)
            )

        if action == "enroll_course":
            return api_result.get(
                "message",
                "Enrollment completed successfully.",
            )

        if action == "list_enrolled_courses":
            courses = (
                api_result.get("enrolled_courses")
                or api_result.get("courses", [])
            )
            if not courses:
                return "No enrolled courses found."

            return (
                "Enrolled courses:\n"
                + _format_course_list(courses)
            )

        return "Request completed successfully."

    if status == "DENIED_UNSUPPORTED_ACTION":
        return (
            last_result.get("error")
            or "The requested operation is not supported by this Course Agent."
        )

    if status == "INTENT_CLASSIFICATION_FAILED":
        return (
            last_result.get("error")
            or "Intent classification failed."
        )

    if status == "USER_RESOLUTION_FAILED":
        return (
            last_result.get("error")
            or "IBM Verify user resolution failed."
        )

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
            content={
                "error": "Not logged in. Please login with IBM Verify first."
            },
        )

    # The OAuth access token is not used to establish the logged-in user's
    # identity. It is kept unchanged and supplied only as the subject_token
    # during Token Exchange.
    subject_token = subject_tokens["access_token"]

    # The authenticated Human User is established from the validated OIDC
    # ID token.
    if id_claims.get("exp") and int(id_claims["exp"]) < int(time.time()):
        request.session.clear()
        return JSONResponse(
            status_code=401,
            content={
                "error": (
                    "Login session expired. "
                    "Please login again with IBM Verify."
                )
            },
        )

    logged_in_subject = (
        id_claims.get("preferred_username")
        or id_claims.get("email")
        or id_claims.get("sub")
        or "unknown-user"
    )

    # ------------------------------------------------------------------
    # Stage 1: Intent classification
    #
    # This is deliberately separate from IBM Verify Directory resolution.
    # A classifier problem must not be reported as a user-resolution problem.
    # ------------------------------------------------------------------
    try:
        decision = decide_action(message)
    except Exception as exc:
        traceback.print_exc()

        last_result = {
            "status": "INTENT_CLASSIFICATION_FAILED",
            "user_message": message,
            "llm_enabled": settings.use_llm,
            "llm_model": (
                settings.gemini_model
                if settings.use_llm
                else "deterministic-fallback"
            ),
            "logged_in_subject": logged_in_subject,
            "intent_classification_performed": True,
            "user_resolution_performed": False,
            "token_exchange_performed": False,
            "api_called": False,
            "error": f"Intent classification failed: {str(exc)}",
        }

        return JSONResponse(
            {
                "answer": build_answer(last_result),
                "diagnostic": last_result,
            }
        )

    action = decision.action
    course_id = decision.course_id or "ALL"
    llm_target_subject = decision.target_subject or "self"

    # ------------------------------------------------------------------
    # Stage 1a: Normal unsupported-intent result
    #
    # unsupported_action is not an IBM Verify Directory error.
    # No user lookup, actor token, token exchange, or Course API call occurs.
    #
    # Note: delete_course_history is NOT treated as unsupported_action here.
    # It is a deliberate negative authorization test and is included in
    # ALLOWED_ACTIONS above.
    # ------------------------------------------------------------------
    if action == UNSUPPORTED_ACTION:
        last_result = {
            "status": "DENIED_UNSUPPORTED_ACTION",
            "user_message": message,
            "llm_enabled": settings.use_llm,
            "llm_model": (
                settings.gemini_model
                if settings.use_llm
                else "deterministic-fallback"
            ),
            "llm_intent": {
                "action": action,
                "course_id": course_id,
                "llm_target_subject": llm_target_subject,
                "reason": decision.reason,
            },
            "logged_in_subject": logged_in_subject,
            "intent_classification_performed": True,
            "user_resolution_performed": False,
            "token_exchange_performed": False,
            "api_called": False,
            "error": (
                "The intent classifier recognized a request that is "
                "outside the supported Course Agent operations."
            ),
        }

        return JSONResponse(
            {
                "answer": build_answer(last_result),
                "diagnostic": last_result,
            }
        )

    # A value other than unsupported_action or the explicitly supported
    # classifier actions is treated as an invalid classifier result.
    if action not in ALLOWED_ACTIONS:
        last_result = {
            "status": "INTENT_CLASSIFICATION_FAILED",
            "user_message": message,
            "llm_enabled": settings.use_llm,
            "llm_model": (
                settings.gemini_model
                if settings.use_llm
                else "deterministic-fallback"
            ),
            "llm_intent": {
                "action": action,
                "course_id": course_id,
                "llm_target_subject": llm_target_subject,
                "reason": decision.reason,
            },
            "logged_in_subject": logged_in_subject,
            "intent_classification_performed": True,
            "user_resolution_performed": False,
            "token_exchange_performed": False,
            "api_called": False,
            "error": (
                "Intent classifier returned an unknown action: "
                f"{action}"
            ),
        }

        return JSONResponse(
            {
                "answer": build_answer(last_result),
                "diagnostic": last_result,
            }
        )

    # Partial intent is useful if IBM Verify Directory resolution fails.
    intent = {
        "action": action,
        "course_id": course_id,
        "llm_target_subject": llm_target_subject,
        "reason": decision.reason,
    }

    # ------------------------------------------------------------------
    # Stage 2: IBM Verify target-user resolution
    #
    # Self references do not use the management API.
    # Named users such as John/Rick are resolved by verify_directory.py.
    # ------------------------------------------------------------------
    try:
        (
            target_subject,
            resolved_verify_user,
            target_resolution_source,
        ) = await resolve_target_subject(
            llm_target_subject=llm_target_subject,
            logged_in_subject=logged_in_subject,
        )

    except Exception as exc:
        traceback.print_exc()

        last_result = {
            "status": "USER_RESOLUTION_FAILED",
            "user_message": message,
            "llm_enabled": settings.use_llm,
            "llm_model": (
                settings.gemini_model
                if settings.use_llm
                else "deterministic-fallback"
            ),
            "llm_intent": intent,
            "logged_in_subject": logged_in_subject,
            "intent_classification_performed": True,
            "user_resolution_performed": True,
            "token_exchange_performed": False,
            "api_called": False,
            "error": (
                "IBM Verify user resolution failed for "
                f"'{llm_target_subject}': {str(exc)}"
            ),
        }

        return JSONResponse(
            {
                "answer": build_answer(last_result),
                "diagnostic": last_result,
            }
        )

    # Enrich the intent only after the target has been resolved.
    intent.update(
        {
            "target_subject": target_subject,
            "target_resolution_source": target_resolution_source,
            "resolved_verify_user": resolved_verify_user,
        }
    )

    # ------------------------------------------------------------------
    # Stage 3: Build Rich Authorization Request (RAR)
    # ------------------------------------------------------------------
    authorization_details = [
        build_agent_authorization_details(
            creator=(
                settings.actor_client_id
                or "course-assistant-agent"
            ),
            affected_person=target_subject,
            action=action,
            target_system="course-api",
            resource="courses",
            course_id=course_id,
            logged_in_subject=logged_in_subject,
        )
    ]

    # ------------------------------------------------------------------
    # Stage 4: Agent actor token -> IBM Verify Token Exchange -> Course API
    #
    # The existing Token Exchange request behavior is intentionally kept
    # unchanged here. In particular, this file does not alter how
    # verify_oauth.py supplies the requested scope.
    # ------------------------------------------------------------------
    actor_token = None
    delegated_token = None

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
            "status": (
                "SUCCESS"
                if api_result.get("allowed")
                else "DENIED_BY_API"
            ),
            "user_message": message,
            "llm_enabled": settings.use_llm,
            "llm_model": (
                settings.gemini_model
                if settings.use_llm
                else "deterministic-fallback"
            ),
            "llm_intent": intent,
            "logged_in_subject": logged_in_subject,
            "requested_subject": target_subject,
            "action": action,
            "authorization_details": authorization_details,
            "id_token_claims": id_claims,
            "actor_token_claims": (
                decode_unverified(actor_token)
                if actor_token
                else None
            ),
            "delegated_token_claims": (
                decode_unverified(delegated_token)
                if delegated_token
                else None
            ),
            "intent_classification_performed": True,
            "user_resolution_performed": True,
            "token_exchange_performed": True,
            "api_called": True,
            "api_result": api_result,
        }

    except Exception as exc:
        traceback.print_exc()

        last_result = {
            "status": "DENIED_OR_FAILED",
            "user_message": message,
            "llm_enabled": settings.use_llm,
            "llm_model": (
                settings.gemini_model
                if settings.use_llm
                else "deterministic-fallback"
            ),
            "llm_intent": intent,
            "logged_in_subject": logged_in_subject,
            "requested_subject": target_subject,
            "action": action,
            "authorization_details": authorization_details,
            "id_token_claims": id_claims,
            "actor_token_claims": (
                decode_unverified(actor_token)
                if actor_token
                else None
            ),
            "delegated_token_claims": (
                decode_unverified(delegated_token)
                if delegated_token
                else None
            ),
            "intent_classification_performed": True,
            "user_resolution_performed": True,
            # If actor_token exists, the flow reached at least the token
            # exchange stage. If the exchange itself failed, no delegated
            # token exists.
            "token_exchange_performed": actor_token is not None,
            "api_called": delegated_token is not None,
            "error": str(exc),
        }

    return JSONResponse(
        {
            "answer": build_answer(last_result),
            "diagnostic": last_result,
        }
    )


@app.get("/health")
async def health():
    return {"status": "ok"}