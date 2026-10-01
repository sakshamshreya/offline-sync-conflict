#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"

printf '\n== Create a document ==\n'
CREATE_RESPONSE="$(curl -sS -X POST "$BASE_URL/documents" \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"laptop","data":{"title":"Trip plan","body":"Draft","status":"open"}}')"
printf '%s\n' "$CREATE_RESPONSE"
DOC_ID="$(printf '%s' "$CREATE_RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["document_id"])')"
printf '\nDocument ID for this demo: %s\n' "$DOC_ID"

printf '\n== Laptop updates title from version 1 ==\n'
curl -sS -i -X POST "$BASE_URL/documents/$DOC_ID/sync" \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"laptop","operation_id":"laptop-edit-001","base_version":1,"changes":{"title":"Launch plan"}}'

printf '\n== Phone changes a different field from version 1 (safe merge) ==\n'
curl -sS -i -X POST "$BASE_URL/documents/$DOC_ID/sync" \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"phone","operation_id":"phone-edit-001","base_version":1,"changes":{"status":"ready"}}'

printf '\n== Tablet changes the same field from version 1 (conflict) ==\n'
curl -sS -i -X POST "$BASE_URL/documents/$DOC_ID/sync" \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"tablet","operation_id":"tablet-edit-001","base_version":1,"changes":{"title":"Different plan"}}'

printf '\n== Latest document ==\n'
curl -sS -i "$BASE_URL/documents/$DOC_ID"

printf '\n== Change history ==\n'
curl -sS -i "$BASE_URL/documents/$DOC_ID/history?after_version=0&limit=100"
printf '\nDemo complete.\n'
