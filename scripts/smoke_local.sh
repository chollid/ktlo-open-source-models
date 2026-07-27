#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/smoke_local.sh

Runs the operator-side half of the Batch 7 smoke test for
hf-internal-testing/tiny-random-gpt2. The remote must already contain the verified
smoke archive and this checkout must contain its verified state manifest.

Required environment:
  R2_BUCKET             Real R2 bucket name; /smoke is appended automatically

Optional environment:
  RCLONE_BIN            rclone executable (default: rclone)
  RCLONE_CONFIG         config containing a remote named r2
  SMOKE_PULL_BUDGET     deliberately undersized first-pass budget (default: 1B)
  MODEL_ARCHIVE_PYTHON  explicit Python >=3.12 for the frozen local tools
EOF
}

guard_smoke_target() {
  local target=$1
  local bare_bucket=$2

  if [[ \
    -z $target || \
    -z $bare_bucket || \
    $target != */smoke || \
    $target == "$bare_bucket" \
  ]]; then
    echo \
      "ERROR: refusing smoke reclaim; target must end in /smoke and differ from the bare R2 bucket" \
      >&2
    return 1
  fi
}

if (($# != 0)); then
  usage
  exit 2
fi

: "${R2_BUCKET:?ERROR: R2_BUCKET must be set to the real R2 bucket name}"

case "$R2_BUCKET" in
  *$'\n'* | *$'\r'*)
    echo "ERROR: R2_BUCKET must not contain a newline" >&2
    exit 2
    ;;
esac

rclone_bin=${RCLONE_BIN:-rclone}
if ! command -v "$rclone_bin" >/dev/null 2>&1; then
  echo "ERROR: rclone is required but was not found in PATH" >&2
  exit 127
fi
if ! "$rclone_bin" listremotes 2>/dev/null | grep -Fxq 'r2:'; then
  echo "ERROR: rclone remote 'r2:' is not configured" >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
safe_name=hf-internal-testing__tiny-random-gpt2
budget=${SMOKE_PULL_BUDGET:-1B}
base_bucket=${R2_BUCKET%/}
smoke_bucket="${base_bucket}/smoke"
guard_smoke_target "$smoke_bucket" "$base_bucket"
export R2_BUCKET=$smoke_bucket

smoke_tmp=$(mktemp -d "${TMPDIR:-/tmp}/model-archive-smoke-local.XXXXXXXX")
trap 'rm -rf -- "$smoke_tmp"' EXIT
export ARCHIVE="${smoke_tmp}/archive"
mkdir -p -- "$ARCHIVE"

set +e
"${script_dir}/pull.sh" "$safe_name" "$budget"
budget_status=$?
set -e
if ((budget_status != 8)); then
  echo \
    "ERROR: budgeted smoke pull must exit 8, observed ${budget_status}; ${budget} did not prove the pause path" \
    >&2
  exit 1
fi
echo "Budget assertion passed: pull.sh exited 8 and is safe to resume."

"${script_dir}/pull.sh" "$safe_name"
"${script_dir}/verify_local.sh" "$safe_name"
guard_smoke_target "$R2_BUCKET" "$base_bucket"
"${script_dir}/reclaim.sh" "$safe_name" --yes

set +e
remaining=$(
  "$rclone_bin" lsf \
    "r2:${R2_BUCKET}/${safe_name}" \
    --recursive --files-only 2>/dev/null
)
inventory_status=$?
set -e
if ((inventory_status != 0 && inventory_status != 3)); then
  echo \
    "ERROR: could not confirm reclaim completion (rclone exit ${inventory_status})" \
    >&2
  exit "$inventory_status"
fi
if [[ -n $remaining ]]; then
  echo "ERROR: reclaim left one or more smoke model objects in R2" >&2
  exit 1
fi

echo "SMOKE LOCAL PASSED: budget exit 8, resume, local verification, and reclaim."
