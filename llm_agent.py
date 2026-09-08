# Copyright IBM Corp. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from config import settings

try:
    from google import genai
    from google.genai import types
except Exception:  # keeps non-LLM mode working even if google-genai is absent
    genai = None
    types = None


@dataclass
class AgentDecision:
    action: str
    course_id: str
    reason: str
    target_subject: Optional[str] = "self"


ALLOWED_ACTIONS = {
    "list_available_courses",
    "enroll_course",
    "list_enrolled_courses",
    "delete_course_history",
}

UNSUPPORTED_ACTION = "unsupported_action"

COURSE_MAP = {
    "advanced security training": "SEC-301",
    "advanced security": "SEC-301",
    "security training": "SEC-301",
    "advanced security operations": "SEC-301",
    "ai productivity": "GEN-101",
    "productivity basics": "GEN-101",
    "identity governance": "GOV-301",
    "governance for managers": "GOV-301",
}


SYSTEM_INSTRUCTION = """
You are an intent classifier for a course-booking demo agent.
Return ONLY valid JSON. Do not return markdown.

Allowed action values only:
1. list_available_courses
2. enroll_course
3. list_enrolled_courses
4. delete_course_history

Meaning:
- list_available_courses: user asks what courses are available/catalog/can enroll in.
- enroll_course: user asks to enroll/book/register/join a course.
- list_enrolled_courses: user asks what courses are already taken/enrolled/completed by self or someone else.
- delete_course_history: user asks to delete, erase, or remove their course history.
- unsupported_action: anything outside the three supported course operations, including delete/remove/erase/purge/modify/update/cancel/admin requests.

Target subject rules:
- For "my", "me", "mine", "myself" when the requested course data/action is for the signed-in user, return target_subject="self".
- If another person is explicitly named, return that person's name/hint exactly enough for lookup in IBM Verify Directory.
- Possessive phrases identify another person. Example: "John's courses" => target_subject="John".
- "Show me John's courses" still targets John; "me" is only the recipient of the answer.
- Do not invent usernames or user IDs.

Course rules:
- For list_available_courses and list_enrolled_courses, course_id="ALL".
- For enroll_course, identify course_id when possible.
- For unsupported_action, course_id="ALL".
- advanced security training / advanced security operations => SEC-301.
- AI productivity basics => GEN-101.
- identity governance for managers => GOV-301.
- If the requested course is unclear for enroll_course, course_id="UNKNOWN".

Examples:
User: What courses are there to enroll?
{"action":"list_available_courses","course_id":"ALL","target_subject":"self","reason":"User asked for catalog courses available to enroll."}

User: Show available courses
{"action":"list_available_courses","course_id":"ALL","target_subject":"self","reason":"User asked for available course catalog."}

User: Please enroll me into advanced security training
{"action":"enroll_course","course_id":"SEC-301","target_subject":"self","reason":"User asked to enroll self into advanced security training."}

User: Show my enrolled courses
{"action":"list_enrolled_courses","course_id":"ALL","target_subject":"self","reason":"User asked for their enrolled courses."}

User: Which courses are taken by Rick?
{"action":"list_enrolled_courses","course_id":"ALL","target_subject":"Rick","reason":"User asked for another person's enrolled courses."}

User: Show Scott courses
{"action":"list_enrolled_courses","course_id":"ALL","target_subject":"Scott","reason":"User asked for Scott's enrolled courses."}

User: Can you show John's courses?
{"action":"list_enrolled_courses","course_id":"ALL","target_subject":"John","reason":"User asked for John's enrolled courses."}

User: Show me John's courses
{"action":"list_enrolled_courses","course_id":"ALL","target_subject":"John","reason":"User asked for John's enrolled courses."}

User: Please delete my course history
{"action":"delete_course_history","course_id":"ALL","target_subject":"self","reason":"User requested deletion of course history."}
"""


_client = None


def _get_client():
    global _client
    if _client is None:
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        if genai is None:
            raise RuntimeError("google-genai package is not available")
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _extract_course_id(text: str) -> str:
    lowered = text.lower()
    for label, course_id in COURSE_MAP.items():
        if label in lowered:
            return course_id

    # Accept explicit IDs like SEC-301, GEN-101, GOV-301.
    match = re.search(r"\b[A-Z]{2,10}-\d{2,5}\b", text.upper())
    if match:
        return match.group(0)

    return "UNKNOWN"


def _extract_target_hint_without_llm(message: str) -> str:
    """
    Deterministic target extraction used by the non-LLM path and as a guardrail
    when the LLM fails or incorrectly returns "self" for an explicit named target.

    This function extracts only a name/email-like hint. IBM Verify Directory
    resolution still happens later in app.py.
    """
    text = (message or "").strip()
    lowered = text.lower()

    # IMPORTANT: explicit named-target patterns come BEFORE self pronouns.
    # This makes "Show me John's courses" target John rather than "me".

    # "John's courses" / "John’s courses" / "John's enrollment"
    match = re.search(
        r"\b([A-Za-z][A-Za-z0-9._@-]*)['’]s\s+"
        r"(?:course|courses|enrollment|enrollments|course\s+history|enrollment\s+history)\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)

    # "courses for John", "courses taken by John", "courses of John"
    match = re.search(
        r"\b(?:by|for|of)\s+([A-Za-z][A-Za-z0-9._@-]*)\b",
        text,
        re.IGNORECASE,
    )
    if match:
        candidate = match.group(1).strip(" ?.,!\r\n\t")
        if candidate and candidate.lower() not in {"me", "myself", "self"}:
            return candidate

    # "show John courses", "list Rick courses"
    match = re.search(
        r"\b(?:show|list|display|view)\s+([A-Za-z][A-Za-z0-9._@-]*)\s+"
        r"(?:course|courses|enrollment|enrollments)\b",
        text,
        re.IGNORECASE,
    )
    if match:
        candidate = match.group(1).strip(" ?.,!\r\n\t")
        if candidate.lower() not in {
            "available",
            "catalog",
            "my",
            "me",
            "enrolled",
            "all",
        }:
            return candidate

    # No explicit other-user target found. Self references now take effect.
    if re.search(r"\b(my|me|mine|myself)\b", lowered):
        return "self"

    return "self"


def fallback_decide(message: str) -> AgentDecision:
    """
    Deterministic, fail-closed fallback classifier.

    Security behavior:
    - Explicit named targets are preserved (for example, John's courses -> John).
    - Unsupported/destructive operations are never converted to an allowed read.
    - Unknown requests fail closed as unsupported_action.
    """
    text = (message or "").lower()
    target_subject = _extract_target_hint_without_llm(message)

    unsupported_patterns = [
        r"\berase\b",
        r"\bpurge\b",
        r"\bdestroy\b",
        r"\bmodify\b",
        r"\bupdate\b",
        r"\bcancel\b",
        r"\badmin(?:istrative)?\b",
    ]
    if any(w in text for w in ["delete", "erase", "remove", "purge"]):
        return AgentDecision(
        action="delete_course_history",
        course_id="ALL",
        target_subject=target_subject,
        reason="Deterministic fallback: user requested deletion of course history.",
        )

    if any(re.search(pattern, text) for pattern in unsupported_patterns):
        return AgentDecision(
            action=UNSUPPORTED_ACTION,
            course_id="ALL",
            target_subject=target_subject,
            reason="Deterministic fallback: requested operation is not supported.",
        )

    # Read already-enrolled/taken/completed courses BEFORE checking enroll.
    # Word boundaries prevent "enrolled" from being mistaken for "enroll".
    if (
        re.search(r"\b(enrolled|taken|completed)\b", text)
        or re.search(r"\bmy\s+courses\b", text)
        or re.search(r"\bcourse\s+history\b", text)
        or re.search(r"\benrollment\s+history\b", text)
        or (target_subject != "self" and re.search(r"\bcourses?\b", text))
    ):
        return AgentDecision(
            action="list_enrolled_courses",
            course_id="ALL",
            target_subject=target_subject,
            reason="Deterministic fallback: user asked for enrolled/taken courses.",
        )

    # Available catalog requests.
    if (
        "available" in text
        or "catalog" in text
        or "to enroll" in text
        or "can i enroll" in text
        or "courses are there" in text
        or "what courses" in text
    ):
        return AgentDecision(
            action="list_available_courses",
            course_id="ALL",
            target_subject="self",
            reason="Deterministic fallback: user asked for courses available to enroll.",
        )

    # Enrollment requests.
    if re.search(r"\b(enroll|register|join|book)\b", text):
        return AgentDecision(
            action="enroll_course",
            course_id=_extract_course_id(message),
            target_subject=target_subject,
            reason="Deterministic fallback: enrollment request.",
        )

    # Unknown intent fails closed.
    return AgentDecision(
        action=UNSUPPORTED_ACTION,
        course_id="ALL",
        target_subject=target_subject,
        reason="Deterministic fallback: request did not match a supported action.",
    )


def decide_action(message: str) -> AgentDecision:
    # This deterministic extraction is also used as a target guardrail for the
    # LLM path. It does NOT grant access; it only preserves an explicit target
    # named by the user so authorization can evaluate that target correctly.
    explicit_target = _extract_target_hint_without_llm(message)

    if not settings.use_llm:
        return fallback_decide(message)

    try:
        client = _get_client()
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part(text=message)],
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
            ),
        )

        if not response.text:
            raise ValueError("LLM returned empty response")

        data = json.loads(response.text)
        action = str(data.get("action", "")).strip()
        course_id = str(data.get("course_id", "ALL") or "ALL").strip()
        target_subject = str(data.get("target_subject", "self") or "self").strip()
        reason = str(data.get("reason", "Intent classified by LLM") or "Intent classified by LLM")

        # An explicit unsupported_action is a valid classifier result. app.py
        # will reject it because it is not in the application's ALLOWED_ACTIONS.
        if action == UNSUPPORTED_ACTION:
            return AgentDecision(
                action=UNSUPPORTED_ACTION,
                course_id="ALL",
                target_subject=(
                    explicit_target if explicit_target != "self" else target_subject
                ),
                reason=reason,
            )

        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"LLM returned unsupported action: {action}")

        if action in {"list_available_courses", "list_enrolled_courses"}:
            course_id = "ALL"

        if action == "enroll_course" and not course_id:
            course_id = "UNKNOWN"

        # Target-repair guardrail:
        # If the user's text explicitly identifies another person, preserve that
        # explicit target even if the model returns self or a different target.
        # This keeps target extraction consistent with and without the LLM.
        if explicit_target != "self" and target_subject.lower() != explicit_target.lower():
            target_subject = explicit_target
            reason = (
                f"{reason} Target corrected from explicit user text to "
                f"'{explicit_target}'."
            )

        return AgentDecision(
            action=action,
            course_id=course_id,
            target_subject=target_subject,
            reason=reason,
        )

    except Exception as exc:
        fallback = fallback_decide(message)
        fallback.reason = f"LLM classification failed; fallback used. Error: {str(exc)}"
        return fallback
