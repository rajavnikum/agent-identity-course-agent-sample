# Copyright IBM Corp. All Rights Reserved.
# #
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

# Central least-privilege mapping shared by the Course Agent Application and
# the protected Course API. Each action is allowed to request exactly one
# course scope during OAuth 2.0 Token Exchange.
ACTION_SCOPE_MAP = {
    "list_available_courses": "course.read",
    "list_enrolled_courses": "course.read",
    "enroll_course": "course.enroll",
}

ALLOWED_ACTIONS = frozenset(ACTION_SCOPE_MAP)
COURSE_SCOPES = frozenset(ACTION_SCOPE_MAP.values())


def resolve_scope(action: str) -> str:
    """Return the single least-privileged OAuth scope required by an action."""

    scope = ACTION_SCOPE_MAP.get(action)
    if scope is None:
        raise ValueError(f"Unsupported action: {action}")
    return scope
