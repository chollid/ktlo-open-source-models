"""Network-free integration of the Batch 7 failure/recovery orchestration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from collect_shards import collect
from lib import state
from plan_shards import build_plan
from sweeper import sweep_jobs
from verify_remote import verify_job


RCLONE = "/opt/homebrew/bin/rclone"
REPO_ID = "smoke-fixture/tiny-model"
SAFE_NAME = "smoke-fixture__tiny-model"
REVISION = "a" * 40


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _metadata(files: dict[str, bytes]) -> list[dict[str, object]]:
    return [
        {
            "path": path,
            "size": len(content),
            "sha256": _sha256(content),
            "lfs": True,
        }
        for path, content in sorted(files.items())
    ]


def _reopen_as_uploaded() -> state.Job:
    original = state.load(SAFE_NAME)
    assert original is not None
    metadata = [
        {
            "path": path,
            "size": item["size"],
            "sha256": item["sha256"],
            "lfs": item["lfs"],
        }
        for path, item in original["files"].items()
    ]
    reopened = state.create(
        original["repo_id"], original["revision"], original["priority"]
    )
    state.set_files(reopened, metadata)
    for path, item in reopened["files"].items():
        state.mark_uploaded(reopened, path, item["sha256"])
    state.save(reopened)
    return reopened


def test_offline_smoke_converges_then_detects_missing_and_corrupt_objects(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    files = {
        "config.json": b'{"model_type":"gpt2"}\n',
        "model.safetensors": b"tiny-weight-bytes",
    }
    metadata = _metadata(files)
    jobs_dir = tmp_path / "state" / "jobs"
    shards_dir = tmp_path / "shards"
    results_dir = tmp_path / "results"
    bucket = tmp_path / "r2" / "archive" / "smoke"
    remote_model = bucket / SAFE_NAME
    config = tmp_path / "rclone.conf"
    config.write_text("[r2]\ntype = local\n", encoding="utf-8")
    remote_model.mkdir(parents=True)
    results_dir.mkdir()

    monkeypatch.setattr(state, "JOBS_DIR", jobs_dir)
    monkeypatch.setenv("RCLONE_CONFIG", str(config))

    job = state.create(REPO_ID, REVISION, "base")
    state.set_files(job, metadata)
    state.save(job)
    shards = build_plan(SAFE_NAME, shards_dir)
    assert shards == [["config.json", "model.safetensors"]]

    for path, content in files.items():
        (remote_model / path).write_bytes(content)
    result = {
        path: {
            "uploaded": True,
            "sha256_observed": _sha256(content),
            "error": None,
        }
        for path, content in files.items()
    }
    (results_dir / "shard-0-result.json").write_text(
        json.dumps(result), encoding="utf-8"
    )
    uploaded, failed, remaining = collect(
        SAFE_NAME, shards_dir / f"{SAFE_NAME}.json", results_dir
    )
    assert (uploaded, failed, remaining) == (2, 0, 0)

    listed_files = lambda *_args: metadata
    positive = state.load(SAFE_NAME)
    assert positive is not None
    summary = sweep_jobs(
        [positive],
        config={},
        bucket=str(bucket),
        rclone_bin=RCLONE,
        hf_token="offline-token",
        github_repository="owner/repository",
        dry_run=True,
        list_hf_files=listed_files,
    )
    assert summary.dispatched_jobs == 0
    assert "would dispatch" not in capsys.readouterr().out

    report = verify_job(
        positive,
        bucket=str(bucket),
        rclone_bin=RCLONE,
        save_progress=state.save,
    )
    assert (report.verified, report.failed) == (2, 0)
    assert state.load(SAFE_NAME)["status"] == "verified"

    missing_target = "config.json"
    (remote_model / missing_target).unlink()
    reopened = _reopen_as_uploaded()
    dispatches: list[dict[str, object]] = []
    missing_summary = sweep_jobs(
        [reopened],
        config={},
        bucket=str(bucket),
        rclone_bin=RCLONE,
        hf_token="offline-token",
        github_repository="owner/repository",
        dispatch_token="offline-capture",
        list_hf_files=listed_files,
        dispatch=lambda _repo, _token, payload: dispatches.append(dict(payload)),
    )
    after_missing = state.load(SAFE_NAME)
    assert after_missing is not None
    assert state.pending_uploads(after_missing) == [missing_target]
    assert missing_summary.dispatched_jobs == 1
    assert dispatches == [{"repo_id": REPO_ID, "revision": REVISION}]

    (remote_model / missing_target).write_bytes(files[missing_target])
    state.mark_uploaded(
        after_missing, missing_target, _sha256(files[missing_target])
    )
    state.save(after_missing)
    recovered = verify_job(
        after_missing,
        bucket=str(bucket),
        rclone_bin=RCLONE,
        save_progress=state.save,
    )
    assert (recovered.verified, recovered.failed) == (2, 0)
    assert state.load(SAFE_NAME)["status"] == "verified"

    corrupt_target = "model.safetensors"
    wrong = b"x" * len(files[corrupt_target])
    assert _sha256(wrong) != _sha256(files[corrupt_target])
    (remote_model / corrupt_target).write_bytes(wrong)
    reopened = _reopen_as_uploaded()
    corrupt_report = verify_job(
        reopened,
        bucket=str(bucket),
        rclone_bin=RCLONE,
        save_progress=state.save,
    )
    after_corrupt = state.load(SAFE_NAME)
    assert after_corrupt is not None
    assert corrupt_report.failed == 1
    assert not (remote_model / corrupt_target).exists()
    assert after_corrupt["files"][corrupt_target]["uploaded"] is False
    assert after_corrupt["files"][corrupt_target]["verified"] is False
    assert after_corrupt["files"][corrupt_target]["attempts"] == 1
