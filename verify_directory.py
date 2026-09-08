# Copyright IBM Corp. All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import httpx

from config import settings


def _tenant_base() -> str:
    """Return the IBM Verify tenant base URL without /oauth2."""
    issuer = settings.verify_issuer.rstrip("/")
    if issuer.endswith("/oauth2"):
        return issuer[: -len("/oauth2")]
    if "/oauth2/" in issuer:
        return issuer.split("/oauth2/")[0]
    return issuer.replace("/oauth2", "")


def _token_endpoint() -> str:
    """Return the configured Verify token endpoint."""
    if settings.token_endpoint:
        return settings.token_endpoint
    return f"{settings.verify_issuer.rstrip('/')}/token"


def _escape_scim_value(value: str) -> str:
    """Escape a value before placing it in a SCIM filter string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def get_verify_management_token() -> str:
    """
    Obtain an IBM Verify API/management access token by client_credentials.

    The configured API client must have permission to read users, for example
    the readUsers entitlement used by this sample's directory lookup.
    """
    if not settings.verify_management_client_id or not settings.verify_management_client_secret:
        raise RuntimeError(
            "VERIFY_MANAGEMENT_CLIENT_ID and VERIFY_MANAGEMENT_CLIENT_SECRET are required "
            "to resolve non-self users from IBM Verify Directory."
        )

    data = {
        "grant_type": "client_credentials",
        "client_id": settings.verify_management_client_id,
        "client_secret": settings.verify_management_client_secret,
    }

    if settings.verify_management_scopes:
        data["scope"] = settings.verify_management_scopes

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            _token_endpoint(),
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Verify management token failed: {response.status_code} {response.text}"
        )

    body = response.json()
    access_token = body.get("access_token")
    if not access_token:
        raise RuntimeError("Verify management token response did not contain access_token")

    return access_token


async def _search_verify_users(token: str, filter_value: str, count: int = 3) -> list[dict]:
    """Run one IBM Verify /v2.0/Users SCIM-filtered search."""
    url = f"{_tenant_base().rstrip('/')}/v2.0/Users"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/scim+json",
            },
            params={
                "filter": filter_value,
                "count": count,
            },
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Verify user lookup failed: {response.status_code} {response.text}"
        )

    body = response.json()
    return body.get("Resources", []) or []


def _user_summary(user: dict) -> dict:
    """Return only the user fields useful to the sample diagnostics."""
    return {
        "id": user.get("id"),
        "userName": user.get("userName"),
        "displayName": user.get("displayName"),
        "name": user.get("name"),
        "emails": user.get("emails", []),
    }


async def find_verify_user(user_hint: str) -> dict | None:
    """
    Resolve a natural-language user hint to exactly one IBM Verify user.

    Resolution order:
      1. exact userName match
      2. exact email match when the hint contains '@'
      3. broader prefix search over userName, displayName, given name,
         family name, and email

    Returns:
      - a user dictionary when exactly one user is resolved
      - None when no user matches

    Raises:
      - ValueError when a broad lookup matches multiple users, so the caller
        can ask for an exact IBM Verify username or email address
      - RuntimeError for IBM Verify token/API failures
    """
    hint = (user_hint or "").strip()
    if not hint:
        return None

    token = await get_verify_management_token()
    safe_hint = _escape_scim_value(hint)

    # 1. Prefer an exact IBM Verify username.
    resources = await _search_verify_users(
        token,
        f'userName eq "{safe_hint}"',
        count=2,
    )

    if len(resources) == 1:
        return _user_summary(resources[0])
    if len(resources) > 1:
        raise ValueError(
            f"Multiple IBM Verify users matched exact userName '{hint}'."
        )

    # 2. If the user supplied an email address, prefer an exact email match.
    if "@" in hint:
        resources = await _search_verify_users(
            token,
            f'emails.value eq "{safe_hint}"',
            count=2,
        )

        if len(resources) == 1:
            return _user_summary(resources[0])
        if len(resources) > 1:
            raise ValueError(
                f"Multiple IBM Verify users matched email '{hint}'."
            )

    # 3. Natural-language convenience lookup, e.g. John -> john.smith.
    filter_value = (
        f'userName sw "{safe_hint}" '
        f'or displayName sw "{safe_hint}" '
        f'or name.givenName sw "{safe_hint}" '
        f'or name.familyName sw "{safe_hint}" '
        f'or emails.value sw "{safe_hint}"'
    )

    resources = await _search_verify_users(
        token,
        filter_value,
        count=3,
    )

    if len(resources) == 0:
        return None

    if len(resources) > 1:
        raise ValueError(
            f"Multiple IBM Verify users matched '{hint}'. "
            "Use the exact IBM Verify userName or email address."
        )

    return _user_summary(resources[0])