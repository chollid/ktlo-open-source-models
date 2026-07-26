"""Typed, validated, atomic state management for archival jobs."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from lib.hfmeta import FileMeta
from lib.notify import _redact_secret_values, notify


MAX_ATTEMPTS = 5
JOBS_DIR = Path("state/jobs")

Status = Literal[
    "discovered",
    "planned",
    "uploading",
    "verifying",
    "verified",
    "pulled",
    "failed",
]
VALID_STATUSES: frozenset[str] = frozenset(
    {
        "discovered",
        "planned",
        "uploading",
        "verifying",
        "verified",
        "pulled",
        "failed",
    }
)

_FORWARD_TRANSITIONS: dict[str, frozenset[str]] = {
    "discovered": frozenset({"planned", "failed"}),
    "planned": frozenset({"uploading", "verifying", "verified", "failed"}),
    "uploading": frozenset({"verifying", "verified", "failed"}),
    "verifying": frozenset({"verified", "failed"}),
    "verified": frozenset({"pulled", "failed"}),
    "pulled": frozenset(),
    "failed": frozenset(),
}
_JOB_FIELDS = frozenset(
    {
        "repo_id",
        "safe_name",
        "revision",
        "priority",
        "status",
        "discovered_utc",
        "updated_utc",
        "total_bytes",
        "total_files",
        "files",
    }
)
_FILE_FIELDS = frozenset(
    {
        "size",
        "sha256",
        "lfs",
        "uploaded",
        "verified",
        "attempts",
        "last_error",
    }
)
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class FileState(TypedDict):
    size: int
    sha256: str | None
    lfs: bool
    uploaded: bool
    verified: bool
    attempts: int
    last_error: str | None


class Job(dict[str, Any]):
    """Dictionary-compatible job with non-serialized transition bookkeeping."""

    __slots__ = ("_trusted_status",)

    def __init__(self, *args: Any, trusted_status: str | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._trusted_status = trusted_status


class StateError(RuntimeError):
    """Base state-machine error."""


class StateValidationError(StateError):
    """State data does not conform to the binding schema."""


class IllegalTransitionError(StateError):
    """A caller attempted a forbidden status transition."""


class Sha256MismatchError(StateError):
    """Observed upload bytes do not match Hugging Face's LFS SHA256."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def safe_name(repo_id: str) -> str:
    """Map a Hugging Face repository ID to its state/R2-safe name."""

    if not isinstance(repo_id, str) or not repo_id:
        raise ValueError("repo_id must be a non-empty string")
    return repo_id.replace("/", "__")


def _job_path(name: str) -> Path:
    if (
        not isinstance(name, str)
        or not name
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
        or "\x00" in name
    ):
        raise ValueError("safe_name must be a non-empty filename component")
    return JOBS_DIR / f"{name}.json"


def _parse_utc(field_name: str, value: object) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise StateValidationError(f"{field_name} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise StateValidationError(f"{field_name} must be an ISO-8601 timestamp") from None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise StateValidationError(f"{field_name} must be UTC")


def _validate_file(path: str, file_state: object) -> FileState:
    if not isinstance(path, str) or not path:
        raise StateValidationError("file paths must be non-empty strings")
    if not isinstance(file_state, dict) or set(file_state) != _FILE_FIELDS:
        raise StateValidationError(f"invalid state fields for file {path}")

    size = file_state["size"]
    sha256 = file_state["sha256"]
    lfs = file_state["lfs"]
    uploaded = file_state["uploaded"]
    verified = file_state["verified"]
    attempts = file_state["attempts"]
    last_error = file_state["last_error"]

    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise StateValidationError(f"invalid size for file {path}")
    if not isinstance(lfs, bool):
        raise StateValidationError(f"invalid lfs flag for file {path}")
    if lfs:
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise StateValidationError(f"invalid LFS SHA256 for file {path}")
    elif sha256 is not None:
        raise StateValidationError(f"non-LFS file {path} must have sha256 null")
    if not isinstance(uploaded, bool) or not isinstance(verified, bool):
        raise StateValidationError(f"invalid progress flags for file {path}")
    if verified and not uploaded:
        raise StateValidationError(f"verified file {path} must also be uploaded")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise StateValidationError(f"invalid attempts for file {path}")
    if last_error is not None and not isinstance(last_error, str):
        raise StateValidationError(f"invalid last_error for file {path}")
    return cast(FileState, file_state)


def _validate_job(job: dict[str, Any]) -> None:
    if set(job) != _JOB_FIELDS:
        raise StateValidationError("job does not match the binding state schema")

    for field_name in ("repo_id", "safe_name", "revision", "priority"):
        if not isinstance(job[field_name], str) or not job[field_name]:
            raise StateValidationError(f"{field_name} must be a non-empty string")
    if job["safe_name"] != safe_name(job["repo_id"]):
        raise StateValidationError("safe_name does not match repo_id")

    status = job["status"]
    if status not in VALID_STATUSES:
        raise StateValidationError(f"invalid job status: {status!r}")
    _parse_utc("discovered_utc", job["discovered_utc"])
    _parse_utc("updated_utc", job["updated_utc"])

    files = job["files"]
    if not isinstance(files, dict):
        raise StateValidationError("files must be an object")
    validated_files = {
        path: _validate_file(path, file_state)
        for path, file_state in files.items()
    }

    total_files = job["total_files"]
    total_bytes = job["total_bytes"]
    if (
        not isinstance(total_files, int)
        or isinstance(total_files, bool)
        or total_files < 0
        or total_files != len(validated_files)
    ):
        raise StateValidationError("total_files does not match files")
    expected_bytes = sum(file_state["size"] for file_state in validated_files.values())
    if (
        not isinstance(total_bytes, int)
        or isinstance(total_bytes, bool)
        or total_bytes < 0
        or total_bytes != expected_bytes
    ):
        raise StateValidationError("total_bytes does not match files")

    if status == "discovered" and validated_files:
        raise StateValidationError("discovered jobs cannot already contain files")
    if status == "planned" and any(
        file_state["uploaded"] or file_state["verified"]
        for file_state in validated_files.values()
    ):
        raise StateValidationError("planned jobs cannot contain completed files")
    if status == "uploading" and validated_files and all(
        file_state["uploaded"] for file_state in validated_files.values()
    ):
        raise StateValidationError("uploading job has no pending uploads")
    if status == "verifying":
        if not validated_files or not all(
            file_state["uploaded"] for file_state in validated_files.values()
        ):
            raise StateValidationError("verifying job must have every file uploaded")
        if all(file_state["verified"] for file_state in validated_files.values()):
            raise StateValidationError("verifying job has no pending verifications")
    if status in {"verified", "pulled"} and not all(
        file_state["uploaded"] and file_state["verified"]
        for file_state in validated_files.values()
    ):
        raise StateValidationError(
            f"{status} job must have every file uploaded and verified"
        )


def _transition_allowed(old: str, new: str, *, retry: bool = False) -> bool:
    if old == new:
        return True
    if new in _FORWARD_TRANSITIONS.get(old, frozenset()):
        return True
    return retry and new == "uploading" and old in {"verifying", "verified"}


def _synchronize_caller_status(job: Job) -> str:
    """Validate any status a caller assigned directly since the last operation."""

    current = job.get("status")
    if current not in VALID_STATUSES:
        raise StateValidationError(f"invalid job status: {current!r}")

    trusted = job._trusted_status
    if trusted is None:
        job._trusted_status = current
    elif current != trusted:
        if not isinstance(current, str) or not _transition_allowed(trusted, current):
            raise IllegalTransitionError(
                f"illegal job status transition: {trusted} -> {current}"
            )
        job._trusted_status = current
    return cast(str, current)


def _set_status(job: Job, new_status: Status, *, retry: bool = False) -> None:
    if new_status not in VALID_STATUSES:
        raise StateValidationError(f"invalid job status: {new_status!r}")

    current = _synchronize_caller_status(job)
    if not _transition_allowed(
        current, new_status, retry=retry
    ):
        raise IllegalTransitionError(
            f"illegal job status transition: {current} -> {new_status}"
        )
    job["status"] = new_status
    job._trusted_status = new_status


def _set_derived_status(job: Job, derived_status: Status) -> None:
    """Apply status derived from file flags without treating it as caller intent."""

    _synchronize_caller_status(job)
    job["status"] = derived_status
    job._trusted_status = derived_status


def load(safe_name: str) -> Job | None:
    """Load and validate a job by safe name, returning None when absent."""

    path = _job_path(safe_name)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise StateValidationError(f"cannot read valid job state for {safe_name}") from None
    if not isinstance(raw, dict):
        raise StateValidationError(f"job state for {safe_name} must be an object")
    _validate_job(raw)
    return Job(raw, trusted_status=raw["status"])


def save(job: Job) -> None:
    """Validate and atomically persist a job using a same-directory temp file."""

    if not isinstance(job, Job):
        job = Job(job, trusted_status=job.get("status"))

    current = job.get("status")
    if current not in VALID_STATUSES:
        raise StateValidationError(f"invalid job status: {current!r}")
    trusted = job._trusted_status
    if trusted is not None and current != trusted:
        if not isinstance(current, str) or not _transition_allowed(trusted, current):
            raise IllegalTransitionError(
                f"illegal job status transition: {trusted} -> {current}"
            )
        job._trusted_status = current

    job["updated_utc"] = _utc_now()
    _validate_job(job)
    path = _job_path(job["safe_name"])
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            json.dump(job, temp_file, indent=2, sort_keys=True)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, path)
        temp_name = None
        job._trusted_status = cast(str, job["status"])
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink()
            except FileNotFoundError:
                pass


def create(repo_id: str, revision: str, priority: str) -> Job:
    """Create a new in-memory job in discovered state."""

    if not revision:
        raise ValueError("revision must be non-empty")
    if not priority:
        raise ValueError("priority must be non-empty")
    now = _utc_now()
    job = Job(
        {
            "repo_id": repo_id,
            "safe_name": safe_name(repo_id),
            "revision": revision,
            "priority": priority,
            "status": "discovered",
            "discovered_utc": now,
            "updated_utc": now,
            "total_bytes": 0,
            "total_files": 0,
            "files": {},
        },
        trusted_status="discovered",
    )
    _validate_job(job)
    return job


def set_files(job: Job, files: list[FileMeta]) -> None:
    """Populate the immutable file plan and advance discovered -> planned."""

    if job["status"] != "discovered":
        raise IllegalTransitionError("files can only be set on a discovered job")

    states: dict[str, FileState] = {}
    for metadata in files:
        path = metadata["path"]
        if path in states:
            raise StateValidationError(f"duplicate file metadata path: {path}")
        states[path] = {
            "size": metadata["size"],
            "sha256": metadata["sha256"],
            "lfs": metadata["lfs"],
            "uploaded": False,
            "verified": False,
            "attempts": 0,
            "last_error": None,
        }

    job["files"] = states
    job["total_bytes"] = sum(item["size"] for item in states.values())
    job["total_files"] = len(states)
    _set_status(job, "planned")


def _file(job: Job, path: str) -> FileState:
    try:
        return cast(FileState, job["files"][path])
    except KeyError:
        raise KeyError(f"file is not in job state: {path}") from None


def record_failure(job: Job, path: str, error: object) -> None:
    """Record one failed operation and trip the poison-pill guard past five."""

    if job["status"] == "failed":
        return
    if job["status"] == "pulled":
        raise IllegalTransitionError("cannot record a failure on a pulled job")
    file_state = _file(job, path)
    file_state["attempts"] += 1
    file_state["last_error"] = _redact_secret_values(error)
    if file_state["attempts"] > MAX_ATTEMPTS:
        _set_status(job, "failed")
        notify(
            f"Model archive job failed for {job['repo_id']}: "
            f"retry limit exceeded for {path}",
            level="error",
        )


def mark_uploaded(job: Job, path: str, sha256_observed: str) -> None:
    """Mark an upload complete after comparing its streamed SHA256 when LFS."""

    file_state = _file(job, path)
    if not isinstance(sha256_observed, str) or not _SHA256.fullmatch(
        sha256_observed
    ):
        record_failure(job, path, "upload did not report a valid SHA256")
        if job["status"] != "failed":
            recompute_status(job)
        raise Sha256MismatchError(f"invalid observed SHA256 for file {path}")

    declared = file_state["sha256"]
    if declared is not None and declared.lower() != sha256_observed.lower():
        file_state["uploaded"] = False
        file_state["verified"] = False
        record_failure(job, path, "streamed SHA256 did not match HF LFS metadata")
        if job["status"] != "failed":
            recompute_status(job)
        raise Sha256MismatchError(f"SHA256 mismatch for file {path}")

    file_state["uploaded"] = True
    file_state["last_error"] = None
    recompute_status(job)


def mark_verified(job: Job, path: str) -> None:
    """Mark an uploaded file remotely verified and recompute the job status."""

    file_state = _file(job, path)
    if not file_state["uploaded"]:
        raise IllegalTransitionError(f"cannot verify file before upload: {path}")
    file_state["verified"] = True
    file_state["last_error"] = None
    recompute_status(job)


def merge_shard_results(job: Job, results: dict[str, dict[str, Any]]) -> None:
    """Merge one collect job's shard result map into durable file state."""

    for path, result in results.items():
        _file(job, path)
        if job["status"] == "failed":
            break
        if not isinstance(result, dict):
            record_failure(job, path, "shard result was not an object")
            continue

        error = result.get("error")
        if error or not result.get("uploaded", False):
            record_failure(job, path, error or "shard did not upload file")
            continue

        observed = result.get("sha256_observed")
        try:
            mark_uploaded(job, path, observed)
        except Sha256MismatchError:
            # mark_uploaded has already persisted the failed attempt in-memory;
            # continue so one bad shard result does not hide other successes.
            continue

    if job["status"] != "failed":
        recompute_status(job)


def recompute_status(job: Job) -> Status:
    """Derive uploading/verifying/verified from per-file progress flags."""

    current = _synchronize_caller_status(job)
    if current in {"failed", "pulled"}:
        return cast(Status, current)

    files: dict[str, FileState] = job["files"]
    if not files:
        if current == "discovered":
            return "discovered"
        _set_derived_status(job, "verified")
        return "verified"
    for path, file_state in files.items():
        if file_state["verified"] and not file_state["uploaded"]:
            raise StateValidationError(f"verified file {path} is not uploaded")

    if any(not file_state["uploaded"] for file_state in files.values()):
        derived: Status = "uploading"
    elif any(not file_state["verified"] for file_state in files.values()):
        derived = "verifying"
    else:
        derived = "verified"
    _set_derived_status(job, derived)
    return derived


def pending_uploads(job: Job) -> list[str]:
    """Return paths still needing upload, or none for a poison-pill job."""

    if job["status"] == "failed":
        return []
    return [
        path
        for path, file_state in job["files"].items()
        if not file_state["uploaded"]
    ]


def pending_verifications(job: Job) -> list[str]:
    """Return uploaded paths still needing byte verification."""

    if job["status"] == "failed":
        return []
    return [
        path
        for path, file_state in job["files"].items()
        if file_state["uploaded"] and not file_state["verified"]
    ]


def reset_file(job: Job, path: str) -> None:
    """Legally reopen a file for sweeper retry without resetting its attempts."""

    if job["status"] in {"failed", "pulled"}:
        raise IllegalTransitionError(
            f"cannot reset a file while job status is {job['status']}"
        )
    file_state = _file(job, path)
    file_state["uploaded"] = False
    file_state["verified"] = False
    _set_status(job, "uploading", retry=True)
