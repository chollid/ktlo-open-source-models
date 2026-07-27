#!/usr/bin/env python3
"""Stream R2 objects through SHA256 and reconcile remote verification state."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO, Callable, Sequence

from lib import state


HASH_CHUNK_BYTES = 1024 * 1024


class RemoteVerificationError(RuntimeError):
    """Remote verification failed without exposing rclone credentials or config."""


@dataclass(frozen=True)
class VerificationReport:
    """Summary of one bounded verification pass."""

    examined: int
    verified: int
    failed: int
    bytes_verified: int
    bytes_read: int
    wall_seconds: float
    size_only: tuple[str, ...]


def _safe_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if (
        not path
        or candidate.is_absolute()
        or str(candidate) == "."
        or ".." in candidate.parts
        or "." in candidate.parts
    ):
        raise RemoteVerificationError(f"unsafe repository object path: {path!r}")
    return path


def _remote(bucket: str, safe_name: str, path: str) -> str:
    if not bucket:
        raise RemoteVerificationError("R2_BUCKET must be set")
    return f"r2:{bucket.rstrip('/')}/{safe_name}/{_safe_path(path)}"


def streaming_sha256(
    stream: BinaryIO, *, chunk_bytes: int = HASH_CHUNK_BYTES
) -> tuple[str, int]:
    """Hash a binary stream with one reusable buffer and constant memory."""

    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")

    digest = hashlib.sha256()
    total_bytes = 0
    buffer = bytearray(chunk_bytes)
    view = memoryview(buffer)

    while True:
        count = stream.readinto(buffer)
        if count is None:
            raise RemoteVerificationError("stream read returned no byte count")
        if count == 0:
            break
        if count < 0 or count > len(buffer):
            raise RemoteVerificationError("stream returned an invalid byte count")
        digest.update(view[:count])
        total_bytes += count

    return digest.hexdigest(), total_bytes


def _cat_and_hash(
    remote: str, *, rclone_bin: str
) -> tuple[str, int]:
    with tempfile.TemporaryFile() as error_output:
        try:
            process = subprocess.Popen(
                [rclone_bin, "cat", remote],
                stdout=subprocess.PIPE,
                stderr=error_output,
            )
        except OSError:
            raise RemoteVerificationError("could not start rclone cat") from None

        assert process.stdout is not None
        try:
            observed_sha256, observed_bytes = streaming_sha256(process.stdout)
        except BaseException:
            process.terminate()
            process.wait()
            raise
        finally:
            process.stdout.close()

        return_code = process.wait()
        if return_code != 0:
            raise RemoteVerificationError(
                f"rclone cat failed with exit code {return_code}"
            )
    return observed_sha256, observed_bytes


def _delete_object(remote: str, *, rclone_bin: str) -> None:
    try:
        result = subprocess.run(
            [rclone_bin, "deletefile", remote],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        raise RemoteVerificationError("could not start rclone deletefile") from None
    if result.returncode != 0:
        raise RemoteVerificationError(
            f"rclone deletefile failed with exit code {result.returncode}"
        )


def _selected_paths(
    job: state.Job, *, limit: int | None, max_bytes: int | None
) -> list[str]:
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")

    selected: list[str] = []
    selected_bytes = 0
    for path in sorted(state.pending_verifications(job)):
        if limit is not None and len(selected) >= limit:
            break
        size = job["files"][path]["size"]
        if max_bytes is not None and selected_bytes + size > max_bytes:
            continue
        selected.append(path)
        selected_bytes += size
    return selected


def verify_job(
    job: state.Job,
    *,
    bucket: str,
    rclone_bin: str,
    limit: int | None = None,
    max_bytes: int | None = None,
    dry_run: bool = False,
    save_progress: Callable[[state.Job], None] | None = state.save,
    cat_and_hash: Callable[..., tuple[str, int]] = _cat_and_hash,
    delete_object: Callable[..., None] = _delete_object,
) -> VerificationReport:
    """Verify one job, saving after every completed file mutation."""

    started = time.monotonic()
    selected = _selected_paths(job, limit=limit, max_bytes=max_bytes)
    verified = 0
    failed = 0
    bytes_verified = 0
    bytes_read = 0
    size_only_paths: list[str] = []

    for path in selected:
        file_state = job["files"][path]
        remote = _remote(bucket, job["safe_name"], path)
        observed_sha256, observed_bytes = cat_and_hash(
            remote, rclone_bin=rclone_bin
        )
        bytes_read += observed_bytes

        declared_sha256 = file_state["sha256"]
        size_matches = observed_bytes == file_state["size"]
        hash_matches = (
            declared_sha256 is None
            or observed_sha256.lower() == declared_sha256.lower()
        )
        matches = size_matches and hash_matches
        note = " size_only=true" if declared_sha256 is None else ""

        if matches:
            print(
                f"VERIFIED {job['safe_name']}:{path} "
                f"bytes={observed_bytes}{note}"
            )
            if not dry_run:
                state.mark_verified(job, path)
                if save_progress is not None:
                    save_progress(job)
            verified += 1
            bytes_verified += observed_bytes
            if declared_sha256 is None:
                size_only_paths.append(path)
            continue

        reasons: list[str] = []
        if not size_matches:
            reasons.append(
                f"size expected={file_state['size']} observed={observed_bytes}"
            )
        if not hash_matches:
            reasons.append("sha256 mismatch")
        reason = "; ".join(reasons)
        print(f"MISMATCH {job['safe_name']}:{path} {reason}")

        if not dry_run:
            delete_object(remote, rclone_bin=rclone_bin)
            state.reset_file(job, path)
            state.record_failure(job, path, f"remote verification failed: {reason}")
            if save_progress is not None:
                save_progress(job)
        failed += 1
        if not dry_run and job["status"] == "failed":
            break

    elapsed = time.monotonic() - started
    report = VerificationReport(
        examined=verified + failed,
        verified=verified,
        failed=failed,
        bytes_verified=bytes_verified,
        bytes_read=bytes_read,
        wall_seconds=elapsed,
        size_only=tuple(size_only_paths),
    )
    print(
        "Verification pass: "
        f"{report.bytes_verified} bytes verified, "
        f"{report.bytes_read} bytes read in {report.wall_seconds:.3f}s "
        f"({report.verified} verified, {report.failed} failed)"
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stream and verify uploaded R2 model objects."
    )
    parser.add_argument("--safe-name", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-bytes", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    job = state.load(args.safe_name)
    if job is None:
        raise SystemExit(f"job state not found for {args.safe_name}")
    bucket = os.environ.get("R2_BUCKET", "")
    rclone_bin = os.environ.get("RCLONE_BIN", "rclone")

    try:
        verify_job(
            job,
            bucket=bucket,
            rclone_bin=rclone_bin,
            limit=args.limit,
            max_bytes=args.max_bytes,
            dry_run=args.dry_run,
        )
    except (RemoteVerificationError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
