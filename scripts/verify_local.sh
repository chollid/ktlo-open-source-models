#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/verify_local.sh <safe_name>

Required environment:
  ARCHIVE       Local archive directory
  R2_BUCKET     Bucket name, used only to print a targeted re-pull command

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

if (($# != 1)); then
  usage
  exit 2
fi

safe_name=$1
case "$safe_name" in
  "" | "." | ".." | */* | *\\* | *$'\n'* | *$'\r'*)
    echo "ERROR: safe_name must be a single safe filename component" >&2
    exit 2
    ;;
esac

: "${ARCHIVE:?ERROR: ARCHIVE must be set to the local archive directory}"
: "${R2_BUCKET:?ERROR: R2_BUCKET must be set to print the targeted re-pull command}"

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

cd -- "$repo_root"

"$python_bin" - "$safe_name" "${ARCHIVE}/${safe_name}" "$ARCHIVE" "$R2_BUCKET" <<'PY'
from __future__ import annotations

import hashlib
import os
import shlex
import stat
import sys
from pathlib import Path, PurePosixPath

from lib import state


safe_name, local_root_raw, archive_raw, bucket = sys.argv[1:]
job = state.load(safe_name)
if job is None:
    print(
        f"ERROR: authoritative manifest not found: state/jobs/{safe_name}.json",
        file=sys.stderr,
    )
    raise SystemExit(2)
if job["status"] not in {"verified", "pulled"}:
    print(
        f"ERROR: job status is {job['status']!r}; remote verification must finish before local verification",
        file=sys.stderr,
    )
    raise SystemExit(2)

local_root = Path(local_root_raw).resolve()
bad: list[tuple[str, str]] = []
passed = 0


def local_path(manifest_path: str) -> tuple[Path | None, str | None]:
    posix = PurePosixPath(manifest_path)
    if (
        posix.is_absolute()
        or ".." in posix.parts
        or "\n" in manifest_path
        or "\r" in manifest_path
    ):
        return None, "unsafe manifest path"
    candidate = local_root.joinpath(*posix.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(local_root)
    except (FileNotFoundError, OSError, ValueError):
        return None, "missing"
    try:
        mode = resolved.stat().st_mode
    except OSError:
        return None, "unreadable"
    if not stat.S_ISREG(mode):
        return None, "not a regular file"
    return resolved, None


for manifest_path, metadata in sorted(job["files"].items()):
    path, path_error = local_path(manifest_path)
    if path_error is not None or path is None:
        reason = path_error or "missing"
        print(f"FAIL  {manifest_path} ({reason})")
        bad.append((manifest_path, reason))
        continue

    try:
        observed_size = path.stat().st_size
    except OSError as exc:
        reason = f"cannot stat: {exc}"
        print(f"FAIL  {manifest_path} ({reason})")
        bad.append((manifest_path, reason))
        continue
    expected_size = metadata["size"]
    if observed_size != expected_size:
        reason = f"size {observed_size}, expected {expected_size}"
        print(f"FAIL  {manifest_path} ({reason})")
        bad.append((manifest_path, reason))
        continue

    expected_hash = metadata["sha256"]
    if expected_hash is None:
        print(f"PASS  {manifest_path} ({observed_size} bytes; manifest hash unavailable)")
        passed += 1
        continue

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        reason = f"cannot read: {exc}"
        print(f"FAIL  {manifest_path} ({reason})")
        bad.append((manifest_path, reason))
        continue

    observed_hash = digest.hexdigest()
    if observed_hash.lower() != expected_hash.lower():
        reason = f"sha256 {observed_hash}, expected {expected_hash.lower()}"
        print(f"FAIL  {manifest_path} ({reason})")
        bad.append((manifest_path, reason))
        continue

    print(f"PASS  {manifest_path} ({observed_size} bytes; sha256 {observed_hash})")
    passed += 1

if bad:
    source = f"r2:{bucket}/{safe_name}"
    destination = str(Path(archive_raw) / safe_name)
    bad_args = " ".join(shlex.quote(path) for path, _ in bad)
    command = (
        f"rclone copy {shlex.quote(source)} {shlex.quote(destination)} "
        f"--files-from-raw <(printf '%s\\n' {bad_args}) --ignore-size "
        "--cutoff-mode CAUTIOUS --transfers 8 --checkers 16 --fast-list -P"
    )
    print(
        f"VERIFICATION FAILED: {len(bad)} bad, {passed} passed",
        file=sys.stderr,
    )
    print("Re-pull only the bad files with:", file=sys.stderr)
    print(f"  {command}", file=sys.stderr)
    raise SystemExit(1)

if job["status"] == "verified":
    job["status"] = "pulled"
    state.save(job)

print(f"LOCAL VERIFICATION PASSED: {passed} files")
PY
