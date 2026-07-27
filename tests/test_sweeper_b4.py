from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lib import state
import sweeper


RCLONE = "/opt/homebrew/bin/rclone"
REVISION = "a" * 40
REPO_ID = "owner/model"
SAFE_NAME = "owner__model"


def _metadata(path: str, body: bytes, *, lfs: bool = True) -> dict:
    return {
        "path": path,
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest() if lfs else None,
        "lfs": lfs,
    }


@pytest.fixture
def remote_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    jobs_dir = tmp_path / "state" / "jobs"
    bucket = tmp_path / "local-r2" / "bucket"
    config = tmp_path / "rclone.conf"
    config.write_text("[r2]\ntype = local\n", encoding="utf-8")
    monkeypatch.setattr(state, "JOBS_DIR", jobs_dir)
    monkeypatch.setenv("RCLONE_CONFIG", str(config))
    return bucket


def _create_job(
    files: list[dict],
    *,
    uploaded: tuple[str, ...] = (),
    verified: tuple[str, ...] = (),
) -> state.Job:
    job = state.create(REPO_ID, REVISION, "base")
    state.set_files(job, files)
    for path in uploaded:
        declared = job["files"][path]["sha256"]
        state.mark_uploaded(job, path, declared or ("0" * 64))
    for path in verified:
        state.mark_verified(job, path)
    state.save(job)
    return job


def _put(bucket: Path, path: str, body: bytes) -> Path:
    destination = bucket / SAFE_NAME / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)
    return destination


def _hf_listing(files: list[dict], calls: list[tuple[str, str, str | None]]):
    def listing(repo_id: str, revision: str, token: str | None):
        calls.append((repo_id, revision, token))
        return files

    return listing


def _run(
    job: state.Job,
    bucket: Path,
    files: list[dict],
    dispatches: list[tuple[dict, tuple[str, ...]]],
    *,
    hf_calls: list[tuple[str, str, str | None]] | None = None,
):
    calls = [] if hf_calls is None else hf_calls

    def dispatch(_repository: str, _token: str, payload: dict):
        current = state.load(SAFE_NAME)
        assert current is not None
        dispatches.append((payload, tuple(state.pending_uploads(current))))

    return sweeper.sweep_jobs(
        [job],
        config={},
        bucket=str(bucket),
        rclone_bin=RCLONE,
        hf_token="hf-test",
        github_repository="owner/archive",
        dispatch_token="github-test",
        list_hf_files=_hf_listing(files, calls),
        dispatch=dispatch,
    )


def test_nothing_landed_resets_stale_uploaded_state_and_dispatches(
    remote_backend: Path,
):
    bodies = {"a.bin": b"alpha", "b.bin": b"bravo"}
    files = [_metadata(path, body) for path, body in bodies.items()]
    job = _create_job(files, uploaded=("a.bin", "b.bin"))
    dispatches: list[tuple[dict, tuple[str, ...]]] = []

    summary = _run(job, remote_backend, files, dispatches)

    current = state.load(SAFE_NAME)
    assert current is not None
    assert current["status"] == "uploading"
    assert state.pending_uploads(current) == ["a.bin", "b.bin"]
    assert dispatches == [
        (
            {"repo_id": REPO_ID, "revision": REVISION},
            ("a.bin", "b.bin"),
        )
    ]
    assert summary.dispatched_jobs == 1


def test_mid_shard_kill_recovers_landed_file_and_dispatches_exactly_missing(
    remote_backend: Path,
):
    bodies = {
        "model-00001.bin": b"first landed before runner death",
        "model-00002.bin": b"second never started",
        "model-00003.bin": b"third never started",
    }
    files = [_metadata(path, body) for path, body in bodies.items()]
    job = _create_job(files)
    _put(remote_backend, "model-00001.bin", bodies["model-00001.bin"])
    dispatches: list[tuple[dict, tuple[str, ...]]] = []

    _run(job, remote_backend, files, dispatches)

    current = state.load(SAFE_NAME)
    assert current is not None
    assert current["files"]["model-00001.bin"]["uploaded"] is True
    assert current["files"]["model-00001.bin"]["verified"] is False
    assert dispatches == [
        (
            {"repo_id": REPO_ID, "revision": REVISION},
            ("model-00002.bin", "model-00003.bin"),
        )
    ]


def test_partial_landing_preserves_valid_uploaded_object(
    remote_backend: Path,
):
    bodies = {"a.bin": b"landed", "b.bin": b"missing"}
    files = [_metadata(path, body) for path, body in bodies.items()]
    job = _create_job(files, uploaded=("a.bin",))
    landed = _put(remote_backend, "a.bin", bodies["a.bin"])
    dispatches: list[tuple[dict, tuple[str, ...]]] = []

    _run(job, remote_backend, files, dispatches)

    assert landed.read_bytes() == bodies["a.bin"]
    assert dispatches[0][1] == ("b.bin",)


def test_all_landed_unverified_are_stream_verified(
    remote_backend: Path,
):
    bodies = {"a.bin": b"alpha", "config.json": b'{"ok":true}'}
    files = [
        _metadata("a.bin", bodies["a.bin"]),
        _metadata("config.json", bodies["config.json"], lfs=False),
    ]
    job = _create_job(files, uploaded=("a.bin", "config.json"))
    for path, body in bodies.items():
        _put(remote_backend, path, body)

    summary = _run(job, remote_backend, files, [])

    current = state.load(SAFE_NAME)
    assert current is not None
    assert current["status"] == "verified"
    assert all(item["verified"] for item in current["files"].values())
    assert summary.verified_jobs == 1


def test_already_verified_job_is_total_noop(remote_backend: Path):
    body = b"already complete"
    files = [_metadata("model.bin", body)]
    job = _create_job(
        files, uploaded=("model.bin",), verified=("model.bin",)
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("terminal job performed external work")

    summary = sweeper.sweep_jobs(
        [job],
        config={},
        bucket="",
        rclone_bin=RCLONE,
        hf_token=None,
        github_repository="",
        list_hf_files=forbidden,
        list_remote=forbidden,
        delete_remote=forbidden,
        dispatch=forbidden,
        verifier=forbidden,
    )

    assert summary.active_jobs == 0
    assert summary.dispatched_jobs == 0


def test_size_mismatch_is_deleted_reset_and_dispatched(
    remote_backend: Path,
):
    body = b"correct-size"
    files = [_metadata("model.bin", body)]
    job = _create_job(files, uploaded=("model.bin",))
    bad_object = _put(remote_backend, "model.bin", b"short")
    dispatches: list[tuple[dict, tuple[str, ...]]] = []

    _run(job, remote_backend, files, dispatches)

    current = state.load(SAFE_NAME)
    assert current is not None
    assert not bad_object.exists()
    assert current["files"]["model.bin"]["uploaded"] is False
    assert dispatches[0][1] == ("model.bin",)


def test_orphan_is_reported_loudly_and_never_deleted(
    remote_backend: Path, capsys: pytest.CaptureFixture[str]
):
    body = b"planned"
    files = [_metadata("model.bin", body)]
    job = _create_job(files, uploaded=("model.bin",))
    _put(remote_backend, "model.bin", body)
    orphan = _put(remote_backend, "manual/keep-me.bin", b"operator copy")

    summary = _run(job, remote_backend, files, [])

    output = capsys.readouterr().out
    assert "WARNING ORPHAN OBJECT - NEVER AUTO-DELETING" in output
    assert "manual/keep-me.bin" in output
    assert orphan.read_bytes() == b"operator copy"
    assert summary.orphan_objects == 1


def test_poison_pill_fails_without_hf_r2_or_dispatch(
    remote_backend: Path, monkeypatch: pytest.MonkeyPatch
):
    files = [_metadata("model.bin", b"bad forever")]
    job = _create_job(files)
    job["files"]["model.bin"]["attempts"] = state.MAX_ATTEMPTS + 1
    state.save(job)
    notifications: list[str] = []
    monkeypatch.setattr(
        sweeper, "notify", lambda text, level="info": notifications.append(text)
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("poisoned job performed external work")

    summary = sweeper.sweep_jobs(
        [job],
        config={},
        bucket=str(remote_backend),
        rclone_bin=RCLONE,
        hf_token=None,
        github_repository="",
        list_hf_files=forbidden,
        list_remote=forbidden,
        dispatch=forbidden,
        verifier=forbidden,
    )

    current = state.load(SAFE_NAME)
    assert current is not None
    assert current["status"] == "failed"
    assert summary.failed_jobs == 1
    assert notifications


def test_dry_run_prints_actions_without_mutating_any_source(
    remote_backend: Path, capsys: pytest.CaptureFixture[str]
):
    body = b"expected bytes"
    files = [_metadata("model.bin", body)]
    job = _create_job(files, uploaded=("model.bin",))
    before = (state.JOBS_DIR / f"{SAFE_NAME}.json").read_bytes()
    bad_object = _put(remote_backend, "model.bin", b"wrong")
    dispatches: list[tuple[dict, tuple[str, ...]]] = []

    sweeper.sweep_jobs(
        [job],
        config={},
        bucket=str(remote_backend),
        rclone_bin=RCLONE,
        hf_token=None,
        github_repository="owner/archive",
        dispatch_token="token",
        dry_run=True,
        list_hf_files=lambda *_args: files,
        dispatch=lambda *args: dispatches.append(args),
    )

    output = capsys.readouterr().out
    assert "DRY RUN DIFF" in output
    assert "SIZE_MISMATCH" in output
    assert "would dispatch grab-model for exactly: model.bin" in output
    assert bad_object.read_bytes() == b"wrong"
    assert (state.JOBS_DIR / f"{SAFE_NAME}.json").read_bytes() == before
    assert dispatches == []
