#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/pull.sh <safe_name> [budget] [--max-duration DURATION] [--bwlimit LIMIT]

Required environment:
  ARCHIVE       Local archive directory
  R2_BUCKET     Bucket name (the configured rclone remote must be named "r2")

Budget behavior:
  A budget is approximate because CAUTIOUS never cancels an in-flight file.
  Budgeted parallelism is reduced from 8 based on the budget and largest pending
  file. Overshoot is bounded by roughly effective-transfers x largest-file-size;
  a budget smaller than one file can therefore overshoot by that one whole file.

Examples:
  scripts/pull.sh org__model 500G
  scripts/pull.sh org__model 500G --max-duration 6h
  scripts/pull.sh org__model --bwlimit "08:00,20M 23:00,off"
EOF
}

if (($# < 1)); then
  usage
  exit 2
fi

original_args=("$@")
safe_name=$1
shift

case "$safe_name" in
  "" | "." | ".." | */* | *\\* | *$'\n'* | *$'\r'*)
    echo "ERROR: safe_name must be a single safe filename component" >&2
    exit 2
    ;;
esac

: "${ARCHIVE:?ERROR: ARCHIVE must be set to the local archive directory}"
: "${R2_BUCKET:?ERROR: R2_BUCKET must be set to the R2 bucket name}"

case "$R2_BUCKET" in
  *$'\n'* | *$'\r'*)
    echo "ERROR: R2_BUCKET must not contain a newline" >&2
    exit 2
    ;;
esac

budget=off
if (($# > 0)) && [[ $1 != --* ]]; then
  budget=$1
  shift
fi

max_duration=
bwlimit=
while (($# > 0)); do
  case "$1" in
    --max-duration)
      if (($# < 2)); then
        echo "ERROR: --max-duration requires a value" >&2
        exit 2
      fi
      max_duration=$2
      shift 2
      ;;
    --max-duration=*)
      max_duration=${1#*=}
      shift
      ;;
    --bwlimit)
      if (($# < 2)); then
        echo "ERROR: --bwlimit requires a value" >&2
        exit 2
      fi
      bwlimit=$2
      shift 2
      ;;
    --bwlimit=*)
      bwlimit=${1#*=}
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

rclone_bin=${RCLONE_BIN:-rclone}
if ! command -v "$rclone_bin" >/dev/null 2>&1; then
  echo "ERROR: rclone is required but was not found in PATH" >&2
  exit 127
fi

caller_dir=$PWD
if [[ $ARCHIVE != /* ]]; then
  ARCHIVE="${caller_dir}/${ARCHIVE}"
fi
mkdir -p -- "$ARCHIVE"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
script_path="${script_dir}/$(basename -- "${BASH_SOURCE[0]}")"
source_path="r2:${R2_BUCKET}/${safe_name}"
destination_path="${ARCHIVE}/${safe_name}"
log_path="${ARCHIVE}/${safe_name}.pull.log"

file_size() {
  local path=$1
  if stat -f '%z' "$path" >/dev/null 2>&1; then
    stat -f '%z' "$path"
  else
    stat -c '%s' "$path"
  fi
}

parse_size_bytes() {
  local value normalized number suffix exponent
  value=$1
  normalized=$(printf '%s' "$value" | tr '[:lower:]' '[:upper:]')
  if [[ ! $normalized =~ ^([0-9]+([.][0-9]+)?)([BKMGTPE]?I?B?)$ ]]; then
    return 1
  fi
  number=${BASH_REMATCH[1]}
  suffix=${BASH_REMATCH[3]}
  suffix=${suffix%B}
  suffix=${suffix%I}
  case "$suffix" in
    "") exponent=0 ;;
    K) exponent=1 ;;
    M) exponent=2 ;;
    G) exponent=3 ;;
    T) exponent=4 ;;
    P) exponent=5 ;;
    E) exponent=6 ;;
    *) return 1 ;;
  esac
  awk -v number="$number" -v exponent="$exponent" \
    'BEGIN { printf "%.0f\n", number * (2 ^ (10 * exponent)) }'
}

transfers=8
pending_count=0
largest_pending_size=0
requested_budget_bytes=
pull_tmp=
pending_manifest=
if [[ $budget != off ]]; then
  pull_tmp=$(mktemp -d "${TMPDIR:-/tmp}/model-pull.XXXXXX")
  trap 'rm -rf -- "$pull_tmp"' EXIT
  source_listing="${pull_tmp}/source.tsv"
  pending_manifest="${pull_tmp}/pending.tsv"

  if ! "$rclone_bin" lsf "$source_path" \
    --recursive --files-only --format sp --separator $'\t' \
    >"$source_listing"; then
    echo "ERROR: could not inventory pending files before budgeted pull" >&2
    exit 1
  fi

  while IFS=$'\t' read -r size relative_path; do
    [[ $size =~ ^[0-9]+$ ]] || {
      echo "ERROR: invalid size in rclone source listing" >&2
      exit 1
    }
    [[ -n $relative_path ]] || continue
    local_path="${destination_path}/${relative_path}"
    local_size=-1
    if [[ -f $local_path ]]; then
      local_size=$(file_size "$local_path")
    fi
    if ((local_size != size)); then
      printf '%s\t%s\n' "$size" "$relative_path" >>"$pending_manifest"
      ((pending_count += 1))
      if ((size > largest_pending_size)); then
        largest_pending_size=$size
      fi
    fi
  done <"$source_listing"

  if requested_budget_bytes=$(parse_size_bytes "$budget"); then
    if ((largest_pending_size > 0)); then
      # CAUTIOUS accounts only after transfers start. Limit simultaneous starts to
      # the number of largest files that fit in the budget, with one slot as the
      # minimum concurrency and eight as the unbudgeted ceiling. If even one file
      # cannot fit, a single slot lets CAUTIOUS refuse it before any bytes move.
      transfers=$(
        awk \
          -v budget_bytes="$requested_budget_bytes" \
          -v largest="$largest_pending_size" \
          'BEGIN {
             slots = int(budget_bytes / largest)
             if (slots < 1) slots = 1
             if (slots > 8) slots = 8
             print slots
           }'
      )
      if ((requested_budget_bytes < largest_pending_size)); then
        echo "WARNING: BUDGET IS SMALLER THAN THE LARGEST PENDING FILE" >&2
        echo "WARNING: requested ${requested_budget_bytes} bytes; largest pending file is ${largest_pending_size} bytes." >&2
        echo "WARNING: CAUTIOUS may make no progress; raise the budget to at least one whole file." >&2
      fi
    else
      transfers=1
    fi
  else
    transfers=1
    echo "WARNING: could not normalize budget '${budget}' for parallelism; using one transfer slot" >&2
  fi

  if ((pending_count > 0 && pending_count <= transfers)); then
    echo "WARNING: BUDGET MAY NOT BE ENFORCED" >&2
    echo "WARNING: ${pending_count} remaining file(s) <= ${transfers} transfer slot(s)." >&2
    echo "WARNING: CAUTIOUS may start every remaining file before budget accounting; the budget is advisory for this run." >&2
  fi
fi

rclone_args=(
  copy "$source_path" "$destination_path"
  --max-transfer "$budget"
  --cutoff-mode CAUTIOUS
  --size-only
  --transfers "$transfers"
  --checkers 16
  --fast-list
  # R2-to-local cannot server-side copy. Disabling Copy also makes directory-backend
  # acceptance tests exercise real byte transfer and therefore max-transfer.
  --disable Copy
  --log-file "$log_path"
  --log-level INFO
  --stats 30s
  -P
)
if [[ -n $max_duration ]]; then
  rclone_args+=(--max-duration "$max_duration")
fi
if [[ -n $bwlimit ]]; then
  rclone_args+=(--bwlimit "$bwlimit")
fi

echo "Pulling ${source_path}"
echo "Destination: ${destination_path}"
echo "Transfer budget: ${budget}"
echo "Effective transfers: ${transfers}"

if "$rclone_bin" "${rclone_args[@]}"; then
  rclone_status=0
else
  rclone_status=$?
fi

if [[ $budget != off ]]; then
  transferred_bytes=0
  transferred_files=0
  if [[ -s $pending_manifest ]]; then
    while IFS=$'\t' read -r size relative_path; do
      local_path="${destination_path}/${relative_path}"
      if [[ -f $local_path ]] && (( $(file_size "$local_path") == size )); then
        ((transferred_bytes += size))
        ((transferred_files += 1))
      fi
    done <"$pending_manifest"
  fi

  if [[ -n $requested_budget_bytes ]]; then
    echo "BUDGET REPORT: requested ${budget} (${requested_budget_bytes} bytes); actual ${transferred_bytes} bytes in ${transferred_files} completed file(s)"
  else
    echo "BUDGET REPORT: requested ${budget}; actual ${transferred_bytes} bytes in ${transferred_files} completed file(s)"
  fi
  echo "BUDGET BOUND: ${transfers} transfer(s) x ${largest_pending_size} largest-pending bytes"
fi

if ((rclone_status == 0)); then
  echo "PULL COMPLETE — run verify_local.sh ${safe_name}"
  exit 0
fi

case "$rclone_status" in
  8)
    echo "PULL PAUSED: budget reached — rerun to continue" >&2
    ;;
  10)
    echo "PULL PAUSED: time window closed — rerun to continue" >&2
    ;;
  *)
    echo "PULL ERROR: error ${rclone_status} — rerun is safe (idempotent)" >&2
    ;;
esac

printf 'Resume: ARCHIVE=%q R2_BUCKET=%q' "$ARCHIVE" "$R2_BUCKET" >&2
if [[ -n ${RCLONE_CONFIG:-} ]]; then
  printf ' RCLONE_CONFIG=%q' "$RCLONE_CONFIG" >&2
fi
if [[ -n ${RCLONE_BIN:-} ]]; then
  printf ' RCLONE_BIN=%q' "$RCLONE_BIN" >&2
fi
printf ' %q' "$script_path" >&2
printf ' %q' "${original_args[@]}" >&2
printf '\n' >&2

exit "$rclone_status"
