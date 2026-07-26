#!/usr/bin/env bash
set -euo pipefail

if (( $# != 4 )); then
  echo "usage: grab_shard.sh SHARD_FILE REPO_ID REVISION SAFE_NAME" >&2
  exit 2
fi

SHARD_FILE=$1
REPO_ID=$2
REVISION=$3
SAFE_NAME=$4

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
PYTHON_BIN=${PYTHON_BIN:-python3}
RCLONE_BIN=${RCLONE_BIN:-rclone}
CURL_BIN=${CURL_BIN:-curl}
PARALLEL_TRANSFERS=${PARALLEL_TRANSFERS:-8}
CURL_RETRIES=${GRAB_CURL_RETRIES:-8}
CURL_RETRY_DELAY=${GRAB_CURL_RETRY_DELAY:-5}
RESULT_DIR=${RESULT_DIR:-.}
SHARD_PLAN_FILE=${SHARD_PLAN_FILE:-state/jobs/${SAFE_NAME}.json}
DURABLE_STATE_FILE=${DURABLE_STATE_FILE:-state/jobs/${SAFE_NAME}.json}
GRAB_BASE_URL=${GRAB_BASE_URL:-https://huggingface.co}

: "${HF_TOKEN:?HF_TOKEN must be set in the environment}"
: "${R2_BUCKET:?R2_BUCKET must be set in the environment}"

if [[ ! $PARALLEL_TRANSFERS =~ ^[1-9][0-9]*$ ]]; then
  echo "PARALLEL_TRANSFERS must be a positive integer" >&2
  exit 2
fi
if [[ ! $(basename -- "$SHARD_FILE") =~ ^shard-([0-9]+)\.json$ ]]; then
  echo "shard filename must be shard-<n>.json" >&2
  exit 2
fi
SHARD_INDEX=${BASH_REMATCH[1]}

if [[ -z ${RCLONE_CONFIG:-} ]]; then
  : "${R2_ACCESS_KEY_ID:?R2_ACCESS_KEY_ID must be set in the environment}"
  : "${R2_SECRET_ACCESS_KEY:?R2_SECRET_ACCESS_KEY must be set in the environment}"
  : "${R2_ACCOUNT_ID:?R2_ACCOUNT_ID must be set in the environment}"
  export RCLONE_CONFIG="${HOME}/.config/rclone/rclone.conf"
  umask 077
  mkdir -p -- "$(dirname -- "$RCLONE_CONFIG")"
  {
    printf '%s\n' '[r2]'
    printf '%s\n' 'type = s3'
    printf '%s\n' 'provider = Cloudflare'
    printf 'access_key_id = %s\n' "$R2_ACCESS_KEY_ID"
    printf 'secret_access_key = %s\n' "$R2_SECRET_ACCESS_KEY"
    printf 'endpoint = https://%s.r2.cloudflarestorage.com\n' "$R2_ACCOUNT_ID"
    printf '%s\n' 'acl = private'
    printf '%s\n' 'no_check_bucket = true'
  } >"$RCLONE_CONFIG"
  chmod 600 "$RCLONE_CONFIG"
fi

mkdir -p -- "$RESULT_DIR"
RUN_TMP=$(mktemp -d "${TMPDIR:-/tmp}/grab-shard.${SHARD_INDEX}.XXXXXX")
trap 'rm -rf -- "$RUN_TMP"' EXIT
RESULT_PARTS="${RUN_TMP}/results"
PENDING_PATHS="${RUN_TMP}/pending.json"
PENDING_NUL="${RUN_TMP}/pending.nul"
mkdir -p -- "$RESULT_PARTS"

export PYTHON_BIN RCLONE_BIN CURL_BIN
export CURL_RETRIES CURL_RETRY_DELAY RESULT_PARTS RUN_TMP R2_BUCKET SAFE_NAME

"$PYTHON_BIN" - \
  "$SHARD_FILE" "$SHARD_PLAN_FILE" "$DURABLE_STATE_FILE" \
  "$SAFE_NAME" "$REPO_ID" "$REVISION" "$GRAB_BASE_URL" "$PENDING_PATHS" \
  >"$PENDING_NUL" <<'PY'
import json
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from lib import state
from lib.hfmeta import download_url

(
    shard_path,
    plan_path,
    durable_path,
    safe_name,
    repo_id,
    revision,
    base_url,
    pending_path,
) = sys.argv[1:]


def load_at(path: str):
    candidate = Path(path)
    if candidate.name != f"{safe_name}.json" or not candidate.exists():
        return None
    original = state.JOBS_DIR
    try:
        state.JOBS_DIR = candidate.parent
        return state.load(safe_name)
    finally:
        state.JOBS_DIR = original


plan = load_at(plan_path)
durable = load_at(durable_path)
if plan is None:
    raise SystemExit(f"plan state not found: {plan_path}")
for job in (plan, durable):
    if job is not None and (
        job["safe_name"] != safe_name
        or job["repo_id"] != repo_id
        or job["revision"] != revision
    ):
        raise SystemExit("shard arguments do not match job state")

raw_shard = json.loads(Path(shard_path).read_text(encoding="utf-8"))
if not isinstance(raw_shard, list) or not all(
    isinstance(path, str) and path for path in raw_shard
):
    raise SystemExit("shard manifest must be a list of non-empty paths")
if len(set(raw_shard)) != len(raw_shard):
    raise SystemExit("shard manifest contains duplicate paths")

pending = []
for path in raw_shard:
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        raise SystemExit(f"unsafe repository path: {path}")
    try:
        metadata = plan["files"][path]
    except KeyError:
        raise SystemExit(f"shard file is absent from plan state: {path}") from None
    if metadata["uploaded"]:
        continue
    if (
        durable is not None
        and path in durable["files"]
        and durable["files"][path]["uploaded"]
    ):
        continue

    if base_url == "https://huggingface.co":
        url = download_url(repo_id, revision, path)
    else:
        url = (
            f"{base_url.rstrip('/')}/{quote(repo_id, safe='/')}/resolve/"
            f"{quote(revision, safe='')}/{quote(path.lstrip('/'), safe='/')}"
        )
    declared = (metadata["sha256"] or "").lower()
    pending.append(path)
    for field in (path, declared, str(metadata["size"]), url):
        sys.stdout.buffer.write(field.encode("utf-8") + b"\0")

Path(pending_path).write_text(
    json.dumps(pending, ensure_ascii=False), encoding="utf-8"
)
PY

# shellcheck disable=SC2329  # Invoked through exported process_file.
hash_stream() {
  local hash_file=$1
  local status_file=$2
  local hash_status cut_status
  local -a stages

  set +e
  sha256sum | cut -d' ' -f1 >"$hash_file"
  stages=("${PIPESTATUS[@]}")
  hash_status=${stages[0]}
  cut_status=${stages[1]}
  printf '%s %s\n' "$hash_status" "$cut_status" >"$status_file"
  if (( hash_status != 0 || cut_status != 0 )); then
    return 1
  fi
  return 0
}

# shellcheck disable=SC2329  # Invoked through exported process_file.
write_result_part() {
  local path=$1
  local uploaded=$2
  local observed=$3
  local error=$4
  local part_file

  part_file=$(mktemp "${RESULT_PARTS}/part.XXXXXX")
  "$PYTHON_BIN" - "$path" "$uploaded" "$observed" "$error" "$part_file" <<'PY'
import json
import sys
from pathlib import Path

path, uploaded, observed, error, destination = sys.argv[1:]
payload = {
    "path": path,
    "result": {
        "uploaded": uploaded == "true",
        "sha256_observed": observed or None,
        "error": error or None,
    },
}
Path(destination).write_text(json.dumps(payload), encoding="utf-8")
PY
  mv -- "$part_file" "${part_file}.json"
}

# shellcheck disable=SC2329  # Invoked by xargs in a child Bash process.
process_file() {
  local path=$1
  local declared_sha=$2
  local expected_size=$3
  local url=$4
  local file_tmp hash_file hash_status_file curl_log rclone_log
  local hash_pid hash_wait_status auth_pid auth_wait_status
  local hash_target auth_target branch_mode
  local -a pipeline_status curl_args errors
  local observed_sha observed_size destination

  file_tmp=$(mktemp -d "${RUN_TMP}/file.XXXXXX")
  hash_file="${file_tmp}/observed.sha"
  hash_status_file="${file_tmp}/hash.status"
  curl_log="${file_tmp}/curl.stderr"
  rclone_log="${file_tmp}/rclone.stderr"
  curl_args=(
    --silent --show-error --location --fail
    --retry "$CURL_RETRIES"
    --retry-all-errors
    --retry-delay "$CURL_RETRY_DELAY"
    --write-out '%{stderr}GRAB_SIZE:%{size_download}\n'
  )
  if [[ -n ${GRAB_CURL_LIMIT_RATE:-} ]]; then
    curl_args+=(--limit-rate "$GRAB_CURL_LIMIT_RATE")
  fi

  branch_mode=process-substitution
  if [[ ${GRAB_USE_FIFO:-0} == 1 ]]; then
    branch_mode=fifo
    hash_target="${file_tmp}/hash.fifo"
    auth_target="${file_tmp}/auth.fifo"
    mkfifo -- "$hash_target" "$auth_target"
    hash_stream "$hash_file" "$hash_status_file" <"$hash_target" &
    hash_pid=$!
    printf 'Authorization: Bearer %s\n' "$HF_TOKEN" >"$auth_target" &
    auth_pid=$!
  else
    exec 4< <(printf 'Authorization: Bearer %s\n' "$HF_TOKEN")
    auth_pid=$!
    exec 3> >(hash_stream "$hash_file" "$hash_status_file")
    hash_pid=$!
    hash_target=/dev/fd/3
    auth_target=/dev/fd/4
  fi
  destination="r2:${R2_BUCKET%/}/${SAFE_NAME}/${path}"

  # Capture the three foreground statuses immediately, then close and wait for
  # both process-substitution branches. No success decision uses pipefail alone.
  set +e
  "$CURL_BIN" "${curl_args[@]}" \
    --header "@${auth_target}" \
    "$url" \
    2>"$curl_log" \
    | tee "$hash_target" \
    | "$RCLONE_BIN" rcat "$destination" \
        --size "$expected_size" \
        --s3-chunk-size 64M \
        --s3-upload-concurrency 4 \
        --retries 5 --low-level-retries 20 \
        2>"$rclone_log"
  pipeline_status=("${PIPESTATUS[@]}")
  if [[ $branch_mode == process-substitution ]]; then
    exec 4<&-
    exec 3>&-
  fi
  wait "$hash_pid"
  hash_wait_status=$?
  wait "$auth_pid"
  auth_wait_status=$?
  set -e

  observed_sha=
  if [[ -s $hash_file ]]; then
    IFS= read -r observed_sha <"$hash_file"
  fi
  observed_size=$(
    sed -n 's/^GRAB_SIZE://p' "$curl_log" | tail -n 1
  )

  errors=()
  (( pipeline_status[0] == 0 )) || errors+=("curl exited ${pipeline_status[0]}")
  (( pipeline_status[1] == 0 )) || errors+=("tee exited ${pipeline_status[1]}")
  (( pipeline_status[2] == 0 )) || errors+=("rclone rcat exited ${pipeline_status[2]}")
  (( hash_wait_status == 0 )) || errors+=("hash branch exited ${hash_wait_status}")
  (( auth_wait_status == 0 )) || errors+=("authorization header branch exited ${auth_wait_status}")
  if [[ ! -s $hash_status_file ]]; then
    errors+=("hash branch did not report stage statuses")
  else
    local sha_status cut_status
    read -r sha_status cut_status <"$hash_status_file"
    (( sha_status == 0 )) || errors+=("sha256sum exited ${sha_status}")
    (( cut_status == 0 )) || errors+=("hash cut exited ${cut_status}")
  fi
  [[ $observed_sha =~ ^[0-9a-fA-F]{64}$ ]] \
    || errors+=("hash branch did not report a valid SHA256")
  [[ $observed_size =~ ^[0-9]+$ ]] \
    || errors+=("curl did not report a valid byte count")
  if [[ $observed_size =~ ^[0-9]+$ ]] && (( observed_size != expected_size )); then
    errors+=("byte count mismatch: expected ${expected_size}, observed ${observed_size}")
  fi
  if [[ -n $declared_sha && $observed_sha != "$declared_sha" ]]; then
    errors+=("SHA256 mismatch: streamed bytes differ from HF metadata")
  fi

  if (( ${#errors[@]} == 0 )); then
    write_result_part "$path" true "$observed_sha" ""
  else
    local joined_error
    joined_error=$(IFS='; '; echo "${errors[*]}")
    write_result_part "$path" false "${observed_sha:-}" "$joined_error"
  fi
  rm -rf -- "$file_tmp"
  return 0
}

export -f hash_stream write_result_part process_file
export HF_TOKEN GRAB_CURL_LIMIT_RATE GRAB_USE_FIFO

if [[ -s $PENDING_NUL ]]; then
  set +e
  # shellcheck disable=SC2016  # Expansion belongs to the xargs child shell.
  xargs -0 -n 4 -P "$PARALLEL_TRANSFERS" \
    bash -c 'process_file "$1" "$2" "$3" "$4"' _ \
    <"$PENDING_NUL"
  XARGS_STATUS=$?
  set -e
else
  XARGS_STATUS=0
fi

RESULT_FILE="${RESULT_DIR%/}/shard-${SHARD_INDEX}-result.json"
"$PYTHON_BIN" - "$PENDING_PATHS" "$RESULT_PARTS" "$XARGS_STATUS" "$RESULT_FILE" <<'PY'
import json
import sys
from pathlib import Path

pending_path, parts_dir, xargs_status, result_path = sys.argv[1:]
pending = json.loads(Path(pending_path).read_text(encoding="utf-8"))
results = {}
for part in sorted(Path(parts_dir).glob("part.*.json")):
    payload = json.loads(part.read_text(encoding="utf-8"))
    path = payload["path"]
    if path in results:
        raise SystemExit(f"duplicate worker result for {path}")
    results[path] = payload["result"]

for path in pending:
    if path not in results:
        results[path] = {
            "uploaded": False,
            "sha256_observed": None,
            "error": f"worker produced no result (xargs status {xargs_status})",
        }

destination = Path(result_path)
temporary = destination.with_suffix(destination.suffix + ".tmp")
temporary.write_text(
    json.dumps(results, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
temporary.replace(destination)
PY

exit 0
