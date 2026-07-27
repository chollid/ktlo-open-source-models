#!/usr/bin/env python3
"""Converge non-terminal archive jobs from pinned HF metadata and actual R2."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from lib import hfmeta, state
from lib.notify import notify
from verify_remote import (
    RemoteVerificationError,
    VerificationReport,
    verify_job,
)
from watcher import (
    WatcherError,
    _git_commit_state,
    _load_config,
    _post_dispatch,
    _repo_matches,
    _select_dispatch_token,
    filter_repo_files,
)


TERMINAL_STATES = frozenset({"verified", "pulled", "failed"})
_RECOVERED_NON_LFS_SHA256 = "0" * 64


class SweeperError(RuntimeError):
    """A sweep operation failed without exposing credentials or rclone config."""


@dataclass(frozen=True)
class RemoteObject:
    path: str
    size: int


@dataclass(frozen=True)
class JobDiff:
    missing: tuple[str, ...]
    size_mismatches: tuple[tuple[str, int, int], ...]
    recovered: tuple[str, ...]
    present: tuple[str, ...]
    orphans: tuple[str, ...]

    @property
    def needs_upload(self) -> tuple[str, ...]:
        mismatched = (item[0] for item in self.size_mismatches)
        return tuple(sorted((*self.missing, *mismatched)))


@dataclass(frozen=True)
class SweepSummary:
    active_jobs: int
    dispatched_jobs: int
    verified_jobs: int
    failed_jobs: int
    orphan_objects: int


@dataclass
class _JobWork:
    job: state.Job
    needs_upload: tuple[str, ...]
    should_verify: bool


def _safe_object_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if (
        not path
        or candidate.is_absolute()
        or str(candidate) == "."
        or ".." in candidate.parts
        or "." in candidate.parts
    ):
        raise SweeperError(f"unsafe repository object path: {path!r}")
    return path


def _remote_prefix(bucket: str, safe_name: str) -> str:
    if not bucket:
        raise SweeperError("R2_BUCKET must be set")
    return f"r2:{bucket.rstrip('/')}/{safe_name}"


def _remote_object(bucket: str, safe_name: str, path: str) -> str:
    return f"{_remote_prefix(bucket, safe_name)}/{_safe_object_path(path)}"


def list_remote_objects(
    bucket: str, safe_name: str, *, rclone_bin: str
) -> dict[str, RemoteObject]:
    """Return the actual object inventory below one job's R2 prefix."""

    try:
        result = subprocess.run(
            [
                rclone_bin,
                "lsjson",
                _remote_prefix(bucket, safe_name),
                "--recursive",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        raise SweeperError("could not start rclone lsjson") from None

    # rclone's local backend uses exit 3 for a prefix that has never existed.
    # S3-compatible backends normally return an empty listing for the same case.
    if result.returncode == 3:
        return {}
    if result.returncode != 0:
        raise SweeperError(
            f"rclone lsjson failed with exit code {result.returncode}"
        )
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SweeperError("rclone lsjson returned invalid JSON") from None
    if not isinstance(raw, list):
        raise SweeperError("rclone lsjson result must be a list")

    objects: dict[str, RemoteObject] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise SweeperError("rclone lsjson contained a non-object entry")
        if item.get("IsDir", False):
            continue
        path = item.get("Path")
        size = item.get("Size")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise SweeperError("rclone lsjson contained invalid file metadata")
        _safe_object_path(path)
        if path in objects:
            raise SweeperError(f"rclone lsjson returned duplicate path: {path}")
        objects[path] = RemoteObject(path=path, size=size)
    return objects


def delete_remote_object(
    bucket: str, safe_name: str, path: str, *, rclone_bin: str
) -> None:
    try:
        result = subprocess.run(
            [
                rclone_bin,
                "deletefile",
                _remote_object(bucket, safe_name, path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        raise SweeperError("could not start rclone deletefile") from None
    if result.returncode != 0:
        raise SweeperError(
            f"rclone deletefile failed with exit code {result.returncode}"
        )


def _target_for_discovered_job(
    config: Mapping[str, Any], job: state.Job
) -> Mapping[str, Any]:
    for target in config["targets"]:
        if (
            target["priority"] == job["priority"]
            and target["author"] == job["repo_id"].split("/", 1)[0]
            and _repo_matches(job["repo_id"], target["include"])
        ):
            return target
    raise SweeperError(
        f"no watchlist target matches discovered job {job['repo_id']}"
    )


def _authoritative_files(
    job: state.Job,
    *,
    config: Mapping[str, Any],
    hf_token: str | None,
    list_hf_files: Callable[[str, str, str | None], list[hfmeta.FileMeta]],
    dry_run: bool,
) -> tuple[dict[str, hfmeta.FileMeta], dict[str, hfmeta.FileMeta]]:
    listed = list_hf_files(job["repo_id"], job["revision"], hf_token)
    raw_by_path: dict[str, hfmeta.FileMeta] = {}
    for metadata in listed:
        path = metadata["path"]
        _safe_object_path(path)
        if path in raw_by_path:
            raise SweeperError(f"Hugging Face returned duplicate path: {path}")
        raw_by_path[path] = metadata

    if job["status"] == "discovered":
        target = _target_for_discovered_job(config, job)
        filtered = filter_repo_files(listed, target["file_rules"])
        expected = {metadata["path"]: metadata for metadata in filtered}
        if not dry_run:
            state.set_files(job, filtered)
        return raw_by_path, expected

    expected: dict[str, hfmeta.FileMeta] = {}
    for path, file_state in job["files"].items():
        metadata = raw_by_path.get(path)
        if metadata is None:
            raise SweeperError(
                f"planned file is absent at pinned HF revision: {path}"
            )
        if (
            metadata["size"] != file_state["size"]
            or metadata["sha256"] != file_state["sha256"]
            or metadata["lfs"] != file_state["lfs"]
        ):
            raise SweeperError(
                f"state metadata disagrees with pinned HF metadata for {path}"
            )
        expected[path] = metadata
    return raw_by_path, expected


def _diff(
    job: state.Job,
    expected: Mapping[str, hfmeta.FileMeta],
    raw_hf_paths: set[str],
    remote: Mapping[str, RemoteObject],
) -> JobDiff:
    missing: list[str] = []
    mismatched: list[tuple[str, int, int]] = []
    recovered: list[str] = []
    present: list[str] = []

    for path, metadata in expected.items():
        remote_object = remote.get(path)
        if remote_object is None:
            missing.append(path)
        elif remote_object.size != metadata["size"]:
            mismatched.append(
                (path, metadata["size"], remote_object.size)
            )
        else:
            present.append(path)
            if not job["files"].get(path, {}).get("uploaded", False):
                recovered.append(path)

    return JobDiff(
        missing=tuple(sorted(missing)),
        size_mismatches=tuple(sorted(mismatched)),
        recovered=tuple(sorted(recovered)),
        present=tuple(sorted(present)),
        orphans=tuple(sorted(set(remote).difference(raw_hf_paths))),
    )


def _print_diff(job: state.Job, diff: JobDiff, *, dry_run: bool) -> None:
    prefix = "DRY RUN " if dry_run else ""
    print(f"{prefix}DIFF {job['repo_id']}@{job['revision']}")
    for path in diff.present:
        if dry_run:
            print(f"  PRESENT {path}")
    for path in diff.missing:
        print(f"  MISSING {path}")
    for path, expected, observed in diff.size_mismatches:
        print(
            f"  SIZE_MISMATCH {path} expected={expected} observed={observed}"
        )
    for path in diff.recovered:
        print(f"  RECOVER_FROM_R2 {path}")
    for path in diff.orphans:
        print(
            "  WARNING ORPHAN OBJECT - NEVER AUTO-DELETING: "
            f"{job['safe_name']}/{path}"
        )
    if (
        not diff.present
        and not diff.missing
        and not diff.size_mismatches
        and not diff.orphans
    ):
        print("  EMPTY")


def _trip_stale_poison_pill(job: state.Job, *, dry_run: bool) -> bool:
    poisoned = [
        path
        for path, file_state in job["files"].items()
        if file_state["attempts"] > state.MAX_ATTEMPTS
    ]
    if not poisoned:
        return False

    paths = ", ".join(sorted(poisoned))
    if dry_run:
        print(
            f"DRY RUN: would fail {job['repo_id']}; "
            f"retry limit exceeded for {paths}"
        )
        return True

    job["status"] = "failed"
    state.save(job)
    notify(
        f"Model archive job failed for {job['repo_id']}: "
        f"retry limit already exceeded for {paths}",
        level="error",
    )
    print(f"FAILED {job['repo_id']}: retry limit exceeded for {paths}")
    return True


def _dispatch_payload(job: state.Job) -> dict[str, object]:
    return {
        "repo_id": job["repo_id"],
        "revision": job["revision"],
    }


def _load_jobs() -> list[state.Job]:
    jobs: list[state.Job] = []
    if not state.JOBS_DIR.exists():
        return jobs
    for path in sorted(state.JOBS_DIR.glob("*.json")):
        job = state.load(path.stem)
        if job is None:
            raise SweeperError(f"job disappeared during sweep: {path.name}")
        jobs.append(job)
    return jobs


def sweep_jobs(
    jobs: Sequence[state.Job],
    *,
    config: Mapping[str, Any],
    bucket: str,
    rclone_bin: str,
    hf_token: str | None,
    github_repository: str,
    dispatch_token: str | None = None,
    dispatch_token_factory: Callable[[], str] = _select_dispatch_token,
    dry_run: bool = False,
    list_hf_files: Callable[
        [str, str, str | None], list[hfmeta.FileMeta]
    ] = hfmeta.list_repo_files,
    list_remote: Callable[..., dict[str, RemoteObject]] = list_remote_objects,
    delete_remote: Callable[..., None] = delete_remote_object,
    dispatch: Callable[
        [str, str, Mapping[str, object]], None
    ] = _post_dispatch,
    verifier: Callable[..., VerificationReport] = verify_job,
    persist_before_dispatch: Callable[[Sequence[Path]], bool] | None = None,
) -> SweepSummary:
    """Reconcile all supplied jobs, dispatch recovery, then verify complete jobs."""

    work: list[_JobWork] = []
    changed_paths: list[Path] = []
    failed_jobs = 0
    orphan_objects = 0
    active_jobs = 0

    for job in jobs:
        if job["status"] in TERMINAL_STATES:
            continue
        active_jobs += 1
        if _trip_stale_poison_pill(job, dry_run=dry_run):
            if not dry_run:
                failed_jobs += 1
            continue

        before = json.dumps(job, sort_keys=True)
        raw_hf, expected = _authoritative_files(
            job,
            config=config,
            hf_token=hf_token,
            list_hf_files=list_hf_files,
            dry_run=dry_run,
        )
        remote = list_remote(
            bucket, job["safe_name"], rclone_bin=rclone_bin
        )
        diff = _diff(job, expected, set(raw_hf), remote)
        _print_diff(job, diff, dry_run=dry_run)
        orphan_objects += len(diff.orphans)
        if diff.orphans and not dry_run:
            notify(
                f"ORPHAN OBJECTS detected for {job['repo_id']} and deliberately "
                f"left untouched: {', '.join(diff.orphans)}",
                level="error",
            )

        if dry_run:
            if diff.needs_upload:
                print(
                    "DRY RUN: would dispatch grab-model for exactly: "
                    + ", ".join(diff.needs_upload)
                )
            else:
                pending = [
                    path
                    for path in expected
                    if not job["files"].get(path, {}).get("verified", False)
                ]
                if pending:
                    print(
                        "DRY RUN: would byte-verify: "
                        + ", ".join(sorted(pending))
                    )
                else:
                    print("DRY RUN: would mark job verified")
            work.append(
                _JobWork(
                    job=job,
                    needs_upload=diff.needs_upload,
                    should_verify=not diff.needs_upload,
                )
            )
            continue

        for path, _expected_size, _observed_size in diff.size_mismatches:
            delete_remote(
                bucket,
                job["safe_name"],
                path,
                rclone_bin=rclone_bin,
            )
            if job["files"][path]["uploaded"]:
                state.reset_file(job, path)

        for path in diff.missing:
            if job["files"][path]["uploaded"]:
                state.reset_file(job, path)

        for path in diff.recovered:
            declared = job["files"][path]["sha256"]
            state.mark_uploaded(
                job, path, declared or _RECOVERED_NON_LFS_SHA256
            )

        state.recompute_status(job)
        after = json.dumps(job, sort_keys=True)
        if after != before:
            state.save(job)
            changed_paths.append(
                state.JOBS_DIR / f"{job['safe_name']}.json"
            )

        needs_upload = tuple(sorted(state.pending_uploads(job)))
        work.append(
            _JobWork(
                job=job,
                needs_upload=needs_upload,
                should_verify=not needs_upload
                and bool(state.pending_verifications(job)),
            )
        )

    dispatch_work = [item for item in work if item.needs_upload]
    if (
        not dry_run
        and dispatch_work
        and changed_paths
        and persist_before_dispatch is not None
    ):
        persist_before_dispatch(changed_paths)

    dispatched_jobs = 0
    for item in dispatch_work:
        if dry_run:
            continue
        token = dispatch_token
        if token is None:
            token = dispatch_token_factory()
            dispatch_token = token
        dispatch(
            github_repository,
            token,
            _dispatch_payload(item.job),
        )
        dispatched_jobs += 1
        print(
            f"DISPATCHED {item.job['repo_id']} for missing files: "
            + ", ".join(item.needs_upload)
        )
        notify(
            f"Re-dispatched model archive job for {item.job['repo_id']}: "
            f"{len(item.needs_upload)} files still need upload"
        )

    verified_jobs = 0
    for item in work:
        if dry_run or not item.should_verify:
            continue
        verifier(
            item.job,
            bucket=bucket,
            rclone_bin=rclone_bin,
            save_progress=state.save,
        )
        if item.job["status"] == "verified":
            verified_jobs += 1
            notify(
                f"Model archive ready to pull: {item.job['repo_id']} at "
                f"{item.job['revision']} ({item.job['total_bytes']} bytes)"
            )
            print(f"READY TO PULL {item.job['repo_id']}")
        elif item.job["status"] == "failed":
            failed_jobs += 1

    if active_jobs == 0:
        print("nothing to do")

    return SweepSummary(
        active_jobs=active_jobs,
        dispatched_jobs=dispatched_jobs,
        verified_jobs=verified_jobs,
        failed_jobs=failed_jobs,
        orphan_objects=orphan_objects,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Converge incomplete model archive jobs from HF, R2, and state."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the full diff and intended actions without mutations",
    )
    args = parser.parse_args(argv)

    try:
        jobs = _load_jobs()
        if not any(job["status"] not in TERMINAL_STATES for job in jobs):
            print("nothing to do")
            return 0

        config = _load_config()
        persist = (
            _git_commit_state
            if os.environ.get("SWEEPER_PUSH_BEFORE_DISPATCH") == "1"
            else None
        )
        sweep_jobs(
            jobs,
            config=config,
            bucket=os.environ.get("R2_BUCKET", ""),
            rclone_bin=os.environ.get("RCLONE_BIN", "rclone"),
            hf_token=os.environ.get("HF_TOKEN"),
            github_repository=os.environ.get("GITHUB_REPOSITORY", ""),
            dry_run=args.dry_run,
            persist_before_dispatch=persist,
        )
    except (
        SweeperError,
        RemoteVerificationError,
        WatcherError,
        hfmeta.HFMetadataError,
        subprocess.CalledProcessError,
        ValueError,
    ) as exc:
        if not args.dry_run:
            notify(f"Sweeper failed: {exc}", level="error")
        raise SystemExit(str(exc)) from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
