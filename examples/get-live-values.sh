#!/bin/bash
# Hoval Connect API - Get Live Values Example
# Usage: ./get-live-values.sh <email> <password> <plantId> <circuitPath>
#
# This script exists to answer one question directly against the live API:
# "does the live-values endpoint still work for my account, and what does
# it actually return?"
#
# That matters because the integration itself NO LONGER CALLS THIS ENDPOINT.
# v1.0.0 removed scheduled telemetry polling entirely (see
# docs/audit-v1.0.0.md); api.py's get_live_values() was deleted with it.
# If you are wondering whether it would be worth reintroducing, run this
# first and look at what comes back, rather than assuming.

set -e

EMAIL="${1:?Usage: $0 <email> <password> <plantId> <circuitPath>}"
PASSWORD="${2:?}"
PLANT_ID="${3:?}"
CIRCUIT_PATH="${4:?}"

BASE="https://azure-iot-prod.hoval.com/core"
CLIENT_ID="991b54b2-7e67-47ef-81fe-572e21c59899"
IDP="https://akwc5scsc.accounts.ondemand.com/oauth2/token"

# MUST stay byte-for-byte identical to const.py's USER_AGENT.
#
# Hoval's gateway returned a blanket HTTP 403 to the integration until the
# v0.24.0 investigation (docs/audit-v0.24.0.md) traced it to the client's
# User-Agent. This exact string is the ONLY one ever confirmed live. That
# audit is explicit that it is plausible-but-untested whether any
# non-default string clears the rule — so do not "tidy" this into
# something nicer-looking without live re-validation.
#
# Note curl's own default UA ("curl/X.Y.Z") is not the one that was
# observed blocked ("python-requests/X.Y.Z"), so earlier revisions of this
# script may well have worked without any UA at all. Sending the proven
# string costs nothing and removes the question entirely — which is the
# point, for a script whose whole job is diagnosing this class of failure.
UA="hoval-connect-forensic-crawler/1.0 (+https://github.com/; diagnostic tool)"

# Step 1: Get ID token
echo "Authenticating..."
TOKEN_RESP=$(curl -s -X POST "$IDP" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "User-Agent: $UA" \
  -d "grant_type=password&client_id=$CLIENT_ID&username=$EMAIL&password=$PASSWORD&scope=openid")

ID_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['id_token'])")

# Step 2: Get Plant Access Token
echo "Getting plant access token..."
PAT=$(curl -s "$BASE/v1/plants/$PLANT_ID/settings" \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "User-Agent: $UA" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Step 3: Get live values
#
# Only circuitPath is sent. docs/openapi-v3.json declares circuitPath as the
# sole required query parameter for this operation and does not define
# circuitType at all. Earlier revisions of this script (and the pre-v1.0.0
# api.py) also sent circuitType and worked in production, so the parameter
# appears to be accepted-and-ignored rather than wrong — but the contract is
# the contract, and an example should demonstrate the documented call.
echo "Fetching live values for circuit $CIRCUIT_PATH..."
curl -s "$BASE/v3/api/statistics/live-values/$PLANT_ID?circuitPath=$CIRCUIT_PATH" \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "X-Plant-Access-Token: $PAT" \
  -H "User-Agent: $UA" | python3 -m json.tool
