#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/reclaim.sh <safe_name> [--verified-only] [--yes]

Without --yes, this prints the exact deletion plan and exits without deleting.
With --verified-only, missing or invalid local files are skipped and only files that
pass a fresh local verification are eligible for deletion.

Required environment:
  ARCHIVE       Local archive directory
  R2_BUCKET     Bucket name (the configured rclone remote must be named "r2")

Optional environment:
  MODEL_ARCHIVE_PYTHON   Explicit Python >=3.12 interpreter
  MODEL_ARCHIVE_REPO_ROOT
                         Alternate checkout root (primarily for isolated tests)
EOF
}

find_python() {
  local candidate
  local -a candidates=()

  if [[ -n ${MODEL_ARCHIVE_PYTHON:-} ]]; then
    candidates=("$MODEL_ARCHIVE_PYTHON")
  else
    if command -v pyenv >/dev/null 2>&1; then
      candidate=$(pyenv which python 2>/dev/null || true)
      if [[ -n $candidate ]]; then
        candidates+=("$candidate")
      fi
      while IFS= read -r candidate; do
        if [[ -n $candidate ]]; then
          candidates+=("$(pyenv root)/versions/${candidate}/bin/python")
        fi
      done < <(pyenv versions --bare 2>/dev/null || true)
    fi
    candidates+=(python3.12 python3 python)
  fi

  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' \
        >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "ERROR: Python >=3.12 is required. Install Python 3.12 or set MODEL_ARCHIVE_PYTHON." >&2
  return 1
}

if (($# < 1)); then
  usage
  exit 2
fi

safe_name=$1
shift
case "$safe_name" in
  "" | "." | ".." | */* | *\\* | *$'\n'* | *$'\r'*)
    echo "ERROR: safe_name must be a single safe filename component" >&2
    exit 2
    ;;
esac

confirmed=false
verified_only=false
while (($# > 0)); do
  case "$1" in
    --yes)
      confirmed=true
      ;;
    --verified-only)
      verified_only=true
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
  shift
done

: "${ARCHIVE:?ERROR: ARCHIVE must be set to the local archive directory}"
: "${R2_BUCKET:?ERROR: R2_BUCKET must be set to the R2 bucket name}"

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

caller_dir=$PWD
if [[ $ARCHIVE != /* ]]; then
  ARCHIVE="${caller_dir}/${ARCHIVE}"
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
code_root=$(cd -- "${script_dir}/.." && pwd -P)
repo_root=${MODEL_ARCHIVE_REPO_ROOT:-$code_root}
if [[ $repo_root != /* ]]; then
  repo_root="${caller_dir}/${repo_root}"
fi

python_bin=$(find_python)
export PYTHONPATH="${code_root}${PYTHONPATH:+:${PYTHONPATH}}"

temporary_dir=$(mktemp -d "${TMPDIR:-/tmp}/model-archive-reclaim.XXXXXXXX")
trap 'rm -rf -- "$temporary_dir"' EXIT
files_from="${temporary_dir}/verified-files.txt"
summary_file="${temporary_dir}/summary"

cd -- "$repo_root"

verification_mode=all
if [[ $verified_only == true ]]; then
  verification_mode=verified-only
fi

if "$python_bin" - "$safe_name" "${ARCHIVE}/${safe_name}" "$R2_BUCKET" \
  "$verification_mode" "$files_from" "$summary_file" <<'PY'
from __future__ import annotations

import hashlib
import stat
import sys
from pathlib import Path, PurePosixPath

from lib import state


safe_name, local_root_raw, bucket, mode, files_from_raw, summary_raw = sys.argv[1:]
job = state.load(safe_name)
if job is None:
    print(
        f"REFUSED: authoritative manifest not found: state/jobs/{safe_name}.json",
        file=sys.stderr,
    )
    raise SystemExit(2)
if job["status"] not in {"verified", "pulled"}:
    print(
        f"REFUSED: job status is {job['status']!r}; remote verification is incomplete",
        file=sys.stderr,
    )
    raise SystemExit(2)

local_root = Path(local_root_raw).resolve()
verified: list[tuple[str, int]] = []
invalid: list[tuple[str, str]] = []


def verify(manifest_path: str, metadata: dict[str, object]) -> str | None:
    posix = PurePosixPath(manifest_path)
    if (
        posix.is_absolute()
        or ".." in posix.parts
        or "\n" in manifest_path
        or "\r" in manifest_path
    ):
        return "unsafe manifest path"
    candidate = local_root.joinpath(*posix.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(local_root)
        file_stat = resolved.stat()
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError):
        return "unreadable or outside archive root"
    if not stat.S_ISREG(file_stat.st_mode):
        return "not a regular file"

    expected_size = metadata["size"]
    if file_stat.st_size != expected_size:
        return f"size {file_stat.st_size}, expected {expected_size}"

    expected_hash = metadata["sha256"]
    if expected_hash is None:
        return None

    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        return f"cannot read: {exc}"
    observed_hash = digest.hexdigest()
    if observed_hash.lower() != str(expected_hash).lower():
        return f"sha256 {observed_hash}, expected {expected_hash}"
    return None


for manifest_path, metadata in sorted(job["files"].items()):
    error = verify(manifest_path, metadata)
    if error is None:
        verified.append((manifest_path, metadata["size"]))
        print(f"VERIFIED  {manifest_path} ({metadata['size']} bytes)")
    else:
        invalid.append((manifest_path, error))
        print(f"NOT VERIFIED  {manifest_path} ({error})", file=sys.stderr)

if invalid and mode == "all":
    print(
        f"REFUSED: {len(invalid)} local file(s) failed fresh verification; no R2 objects will be deleted",
        file=sys.stderr,
    )
    raise SystemExit(1)
if not verified:
    print("REFUSED: no local files passed fresh verification", file=sys.stderr)
    raise SystemExit(1)

files_from = Path(files_from_raw)
with files_from.open("w", encoding="utf-8", newline="\n") as stream:
    for manifest_path, _ in verified:
        stream.write(manifest_path)
        stream.write("\n")

total_bytes = sum(size for _, size in verified)
Path(summary_raw).write_text(
    f"{len(verified)} {total_bytes}\n",
    encoding="utf-8",
)

print("R2 DELETION PLAN:")
for manifest_path, size in verified:
    print(f"  r2:{bucket}/{safe_name}/{manifest_path} ({size} bytes)")
print(f"TOTAL: {len(verified)} object(s), {total_bytes} bytes")
if invalid:
    print(f"SKIPPED: {len(invalid)} object(s) without a verified local copy")
PY
then
  verification_status=0
else
  verification_status=$?
fi

if ((verification_status != 0)); then
  exit "$verification_status"
fi

if [[ $confirmed != true ]]; then
  echo "DRY RUN ONLY — nothing was deleted. Rerun with --yes to execute this exact plan." >&2
  exit 2
fi

read -r planned_count planned_bytes <"$summary_file"
remote_root="r2:${R2_BUCKET}/${safe_name}"
"$rclone_bin" delete "$remote_root" --files-from-raw "$files_from"

echo "R2 RECLAIM COMPLETE:"
while IFS= read -r manifest_path; do
  echo "  deleted ${remote_root}/${manifest_path}"
done <"$files_from"
echo "DELETED TOTAL: ${planned_count} object(s), ${planned_bytes} bytes"
