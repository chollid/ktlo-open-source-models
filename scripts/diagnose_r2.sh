#!/usr/bin/env bash
# Diagnose R2 connectivity. Reads R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY from .env.
#
#   scripts/diagnose_r2.sh <R2_ACCOUNT_ID> <BUCKET_NAME>
#
# Prints real rclone errors (the smoke workflow currently discards them via 2>/dev/null).
# Never prints credential values.
set -uo pipefail

ARG_ACCOUNT_ID=${1:-}
ARG_BUCKET=${2:-}
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
env_file="${repo_root}/.env"

[[ -f $env_file ]] || { echo "ERROR: no .env at ${env_file}" >&2; exit 1; }
set -a
# shellcheck source=/dev/null
. "$env_file"
set +a
: "${R2_ACCESS_KEY_ID:?ERROR: R2_ACCESS_KEY_ID missing from .env}"
: "${R2_SECRET_ACCESS_KEY:?ERROR: R2_SECRET_ACCESS_KEY missing from .env}"

# Args win; otherwise fall back to .env.
ACCOUNT_ID=${ARG_ACCOUNT_ID:-${R2_ACCOUNT_ID:-}}
BUCKET=${ARG_BUCKET:-${R2_BUCKET:-model-archive}}

if [[ -z $ACCOUNT_ID ]]; then
  cat >&2 <<'MSG'
ERROR: R2 account ID not found.

Add these two lines to .env (the local pull scripts need them too):

    R2_ACCOUNT_ID=<32-char hex from https://dash.cloudflare.com/THIS_PART/r2/overview>
    R2_BUCKET=model-archive

Or pass them directly:  bash scripts/diagnose_r2.sh <account_id> <bucket>
MSG
  exit 2
fi

endpoint="https://${ACCOUNT_ID}.r2.cloudflarestorage.com"
echo "endpoint : ${endpoint}"
echo "bucket   : ${BUCKET}"
echo "key id   : ${R2_ACCESS_KEY_ID:0:4}…${R2_ACCESS_KEY_ID: -4} (${#R2_ACCESS_KEY_ID} chars)"
echo "secret   : ${#R2_SECRET_ACCESS_KEY} chars"
echo

echo "== 0. DNS/TLS reachability of the endpoint =="
curl -s -o /dev/null -w "  HTTP %{http_code} in %{time_total}s\n" --max-time 15 "$endpoint" \
  || echo "  FAILED to reach endpoint — account ID is probably wrong"
echo

mkconf() { # $1 = extra line
  local f; f=$(mktemp)
  {
    printf '[r2]\n'
    printf 'type = s3\n'
    printf 'provider = Cloudflare\n'
    printf 'access_key_id = %s\n' "$R2_ACCESS_KEY_ID"
    printf 'secret_access_key = %s\n' "$R2_SECRET_ACCESS_KEY"
    printf 'endpoint = %s\n' "$endpoint"
    printf 'acl = private\n'
    printf 'no_check_bucket = true\n'
    [[ -n ${1:-} ]] && printf '%s\n' "$1"
  } >"$f"
  chmod 600 "$f"
  printf '%s' "$f"
}

probe() {
  local label=$1 conf=$2; shift 2
  echo "== ${label} =="
  timeout 45 rclone --config "$conf" --low-level-retries 1 --retries 1 \
    --contimeout 10s --timeout 20s "$@" 2>&1 | sed 's/^/  /' | head -12
  echo "  -> exit ${PIPESTATUS[0]}"
  echo
}

conf_noregion=$(mkconf "")
conf_region=$(mkconf "region = auto")
trap 'rm -f "$conf_noregion" "$conf_region"' EXIT

probe "1. list buckets, current config (NO region)"  "$conf_noregion" lsd "r2:"
probe "2. list buckets, WITH region = auto"          "$conf_region"   lsd "r2:"
probe "3. list target bucket, WITH region = auto"    "$conf_region"   lsjson "r2:${BUCKET}" --max-depth 1
probe "4. list smoke prefix, WITH region = auto"     "$conf_region"   lsjson "r2:${BUCKET}/smoke" --max-depth 1

echo "== interpretation =="
echo "  1 fails, 2 works              -> missing 'region = auto' is the bug"
echo "  both fail w/ 401/403          -> token invalid or lacks Object Read & Write"
echo "  both fail, step 0 also failed -> R2_ACCOUNT_ID is wrong"
echo "  1&2 ok, 3 fails NotFound      -> bucket name mismatch vs the R2_BUCKET secret"
echo "  3 ok, 4 'directory not found' -> normal, smoke prefix just does not exist yet"
