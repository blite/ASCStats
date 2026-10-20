#!/usr/bin/env bash
# Create (or update) the asc-api-key Secret from this machine's shared App Store Connect setup
# (~/.appstoreconnect/config.json + private_keys/), resolved exactly as ascstats resolves it
# locally, so the cluster uses ASCStats's own key and nothing is copied into the repo. With a
# vendor number it also switches the cluster to real data (asc-settings).
#
#   k8s/scripts/asc-secret-from-local.sh            # key only
#   k8s/scripts/asc-secret-from-local.sh 8xxxxxxx   # key + ASC_SOURCE=asc
set -euo pipefail
cd "$(dirname "$0")/../.."
{ read -r key_id; read -r issuer_id; read -r key; } < <(uv run --quiet python -m ascstats.asc.credentials)

kubectl -n ascstats create secret generic asc-api-key \
  --from-file=AuthKey.p8="$key" \
  --from-literal=ASC_KEY_ID="$key_id" \
  --from-literal=ASC_ISSUER_ID="$issuer_id" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "asc-api-key: key ${key_id:0:2}…"

if [[ $# -ge 1 ]]; then
  kubectl -n ascstats create secret generic asc-settings \
    --from-literal=ASC_SOURCE=asc --from-literal=ASC_VENDOR_NUMBER="$1" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n ascstats rollout restart deploy/web >/dev/null
  echo "asc-settings: ASC_SOURCE=asc; web restarted"
fi
