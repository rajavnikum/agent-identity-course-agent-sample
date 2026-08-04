#!/usr/bin/env bash

# Copyright IBM Corp. All Rights Reserved.
# #
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
: "${TENANT_URL:?Set TENANT_URL}"
: "${ADMIN_CLIENT_ID:?Set ADMIN_CLIENT_ID}"
: "${ADMIN_CLIENT_SECRET:?Set ADMIN_CLIENT_SECRET}"

echo "1/4 Obtain admin token"
ADMIN_ACCESS_TOKEN=$(curl -fsS "$TENANT_URL/oauth2/token" -H 'Content-Type: application/x-www-form-urlencoded' --data-urlencode 'grant_type=client_credentials' --data-urlencode "client_id=$ADMIN_CLIENT_ID" --data-urlencode "client_secret=$ADMIN_CLIENT_SECRET" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

echo "2/4 Create actor client with DCR"
DCR_RESPONSE=$(curl -fsS "$TENANT_URL/oauth2/register" -H "Authorization: Bearer $ADMIN_ACCESS_TOKEN" -H 'Content-Type: application/json' --data @curl/payloads/actor-client-dcr.json)
ACTOR_CLIENT_ID=$(printf '%s' "$DCR_RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["client_id"])')
ACTOR_CLIENT_SECRET=$(printf '%s' "$DCR_RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["client_secret"])')
printf 'Actor client ID: %s\n' "$ACTOR_CLIENT_ID"
printf 'Actor client secret returned by DCR. Store it securely; it is not printed by this script.\n'

echo "3/4 Create Agent Registry record and associate actor client"
: "${ACTOR_CLIENT_REFERENCE:?Set ACTOR_CLIENT_REFERENCE to the client reference used by your IBM Verify environment}"
export ACTOR_CLIENT_ID ACTOR_CLIENT_REFERENCE
AGENT_RESPONSE=$(curl -fsS "$TENANT_URL/v1.0/Agents" -H "Authorization: Bearer $ADMIN_ACCESS_TOKEN" -H 'Accept: application/scim+json' -H 'Content-Type: application/scim+json' --data "$(envsubst < curl/payloads/course-agent.json)")
printf '%s\n' "$AGENT_RESPONSE" | python3 -m json.tool

echo "4/4 Obtain actor token"
curl -fsS "$TENANT_URL/oauth2/token" -H 'Content-Type: application/x-www-form-urlencoded' --data-urlencode 'grant_type=client_credentials' --data-urlencode "client_id=$ACTOR_CLIENT_ID" --data-urlencode "client_secret=$ACTOR_CLIENT_SECRET" --data-urlencode 'scope=agent.run' | python3 -m json.tool
