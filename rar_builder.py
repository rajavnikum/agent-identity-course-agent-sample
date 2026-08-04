# Copyright IBM Corp. All Rights Reserved.
# #
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Optional, Dict, Any

from config import settings


def build_agent_authorization_details(
    creator: str,
    affected_person: str,
    action: str,
    target_system: str,
    resource: str,
    course_id: str = "ALL",
    logged_in_subject: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "type": settings.agent_adt_type,
        "courseId": course_id,
        "operationDetails": {
            "creator": creator,
            "affectedPerson": affected_person,
            "loggedInSubject": logged_in_subject or affected_person,
            "action": action,
            "targetSystem": target_system,
            "resource": resource,
        },
    }
