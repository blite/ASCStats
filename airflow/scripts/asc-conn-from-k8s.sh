#!/usr/bin/env bash
# Build the asc_default Airflow connection from the asc-api-key Kubernetes Secret, so the
# key has exactly one source of truth. Writes airflow/.env (gitignored).
# On Cloud Composer, store the same JSON in Secret Manager as airflow-connections-asc_default.
set -euo pipefail
cd "$(dirname "$0")/.."
get() { kubectl -n ascstats get secret "${2:-asc-api-key}" -o "jsonpath={.data.$1}" | base64 --decode; }
ASC_VENDOR_NUMBER="${ASC_VENDOR_NUMBER:-$(get ASC_VENDOR_NUMBER asc-settings)}"
: "${ASC_VENDOR_NUMBER:?set ASC_VENDOR_NUMBER or create the asc-settings Secret}"
python3 - "$(get ASC_KEY_ID)" "$(get ASC_ISSUER_ID)" "$(get 'AuthKey\.p8')" "$ASC_VENDOR_NUMBER" > .env <<'PY'
import json, sys
key_id, issuer_id, p8, vendor = sys.argv[1:]
conn = {"conn_type": "generic", "login": key_id, "password": p8,
        "extra": {"issuer_id": issuer_id, "vendor_number": vendor}}
print(f"AIRFLOW_CONN_ASC_DEFAULT='{json.dumps(conn)}'")
PY
chmod 600 .env
echo "wrote airflow/.env; restart the stack to pick it up"
