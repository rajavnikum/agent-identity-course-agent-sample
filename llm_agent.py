# Copyright IBM Corp. All Rights Reserved.
# #
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


# These are the only actions that are allowed to proceed to scope resolution
# and OAuth 2.0 Token Exchange.
ALLOWED_ACTIONS = {
    "list_available_courses",
    "enroll_course",
    "list_enrolled_courses",
}

# The classifier can explicitly return this value for requests outside the
# agent's supported capability set. It is intentionally NOT in ALLOWED_ACTIONS.
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

Allowed classification values:
1. list_available_courses
2. enroll_course
3. list_enrolled_courses
4. unsupported_action

Meaning:
- list_available_courses: user asks what courses are available/catalog/can enroll in.
- enroll_course: user asks to enroll/book/register/join a course.
- list_enrolled_courses: user asks what courses are already taken/enrolled/completed by self or someone else.
- unsupported_action: user asks for any operation outside the three supported course actions.

You MUST use unsupported_action for requests such as:
- delete or erase course history
- remove records
- modify or update course history
- cancel/delete course records
- administrative operations
- any other action outside the three supported course actions

Target subject rules:
- For "my", "me", "mine", "myself" return target_subject="self".
- If another person is named, return only the name/hint exactly enough for lookup in IBM Verify Directory.
- Do not invent usernames.

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

User: Which courses is taken by rick?
{"action":"list_enrolled_courses","course_id":"ALL","target_subject":"rick","reason":"User asked for another person's enrolled courses."}

User: Show scott courses
{"action":"list_enrolled_courses","course_id":"ALL","target_subject":"scott","reason":"User asked for Scott's enrolled courses."}

User: Please delete my course history
{"action":"unsupported_action","course_id":"ALL","target_subject":"self","reason":"Delete operations are not supported by this Course Agent."}

User: Remove my enrollment history
{"action":"unsupported_action","course_id":"ALL","target_subject":"self","reason":"Removing course history is not a supported operation."}
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
    m = re.search(r"\b[A-Z]{2,10}-\d{2,5}\b", text.upper())
    if m:
        return m.group(0)

    return "UNKNOWN"


def _extract_target_hint_without_llm(message: str) -> str:
    """
    Deterministic fallback only. This is not a user registry and does not hardcode users.
    It extracts a simple name hint from phrases such as:
    - courses taken by rick
    - courses for scott
    - show scott courses
    IBM Verify Directory resolution happens later in app.py.
    """
    text = message.strip()
    lowered = text.lower()

    if re.search(r"\b(my|me|mine|myself)\b", lowered):
        return "self"

    # "taken by rick", "enrolled by rick"
    m = re.search(r"\b(?:by|for|of)\s+([A-Za-z0-9._@-]+)", text, re.IGNORECASE)
    if m:
        candidate = m.group(1).strip(" ?.,!\r\n\t")
        if candidate:
            return candidate

    # "show scott courses", "list rick courses"
    m = re.search(
        r"\b(?:show|list|display|view)\s+([A-Za-z0-9._@-]+)\s+(?:course|courses|enrollment|enrollments)\b",
        text,
        re.IGNORECASE,
    )
    if m:
        candidate = m.group(1).strip(" ?.,!\r\n\t")
        if candidate.lower() not in {"available", "catalog", "my", "me", "enrolled"}:
            return candidate

    return "self"


def fallback_decide(message: str) -> AgentDecision:
    """
    Deterministic, fail-closed classifier.

    Important security behavior:
    - destructive/unsupported requests return unsupported_action;
    - unknown requests return unsupported_action;
    - unsupported requests never get silently converted to a read operation.
    """
    text = message.lower()
    target_subject = _extract_target_hint_without_llm(message)

    # Fail closed for destructive or otherwise unsupported operations.
    unsupported_patterns = [
        r"\bdelete\b",
        r"\bremove\b",
        r"\berase\b",
        r"\bpurge\b",
        r"\bdestroy\b",
        r"\bmodify\b",
        r"\bupdate\b",
        r"\bcancel\b",
    ]

    if any(re.search(pattern, text) for pattern in unsupported_patterns):
        return AgentDecision(
            action=UNSUPPORTED_ACTION,
            course_id="ALL",
            target_subject=target_subject,
            reason="Deterministic fallback: requested operation is not supported.",
        )

    # Check read-of-existing-enrollment intent BEFORE enroll intent.
    # Using word boundaries prevents 'enrolled' from being treated as 'enroll'.
    if (
        re.search(r"\b(enrolled|taken|completed)\b", text)
        or re.search(r"\bmy\s+courses\b", text)
        or re.search(r"\bcourse\s+history\b", text)
        or re.search(r"\benrollment\s+history\b", text)
    ):
        return AgentDecision(
            action="list_enrolled_courses",
            course_id="ALL",
            target_subject=target_subject,
            reason="Deterministic fallback: user asked for enrolled/taken courses.",
        )

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

    if re.search(r"\b(enroll|register|join|book)\b", text):
        return AgentDecision(
            action="enroll_course",
            course_id=_extract_course_id(message),
            target_subject=target_subject,
            reason="Deterministic fallback: enrollment request.",
        )

    # Unknown intent must fail closed. Do not default to list_available_courses.
    return AgentDecision(
        action=UNSUPPORTED_ACTION,
        course_id="ALL",
        target_subject=target_subject,
        reason="Deterministic fallback: request did not match a supported action.",
    )


def decide_action(message: str) -> AgentDecision:
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
        action = data.get("action", "").strip()
        course_id = data.get("course_id", "ALL") or "ALL"
        target_subject = data.get("target_subject", "self") or "self"
        reason = data.get("reason", "Intent classified by LLM")

        # An explicit unsupported_action is a valid security decision.
        # Do NOT throw it into the fallback classifier, otherwise a delete request
        # could be converted to a permitted read operation.
        if action == UNSUPPORTED_ACTION:
            return AgentDecision(
                action=UNSUPPORTED_ACTION,
                course_id="ALL",
                target_subject=target_subject,
                reason=reason or "Requested operation is not supported.",
            )

        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"LLM returned unknown action: {action}")

        if action in {"list_available_courses", "list_enrolled_courses"}:
            course_id = "ALL"

        if action == "enroll_course" and not course_id:
            course_id = "UNKNOWN"

        return AgentDecision(
            action=action,
            course_id=course_id,
            target_subject=target_subject,
            reason=reason,
        )

    except Exception as exc:
        # Fallback is only for an LLM/service/parsing failure.
        # The fallback itself is fail closed for unknown/unsupported requests.
        fallback = fallback_decide(message)
        fallback.reason = (
            f"LLM classification failed; deterministic fallback used. "
            f"Error: {str(exc)}. {fallback.reason}"
        )
        return fallback
