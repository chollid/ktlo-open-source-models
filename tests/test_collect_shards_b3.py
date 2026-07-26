"""Tests for the single state-writing convergence step."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from collect_shards import collect
from lib import state


def test_collect_populates_discovered_job_and_merges_result_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    observed = hashlib.sha256(b"body").hexdigest()
    durable = state.create("owner/model", "d" * 40, "base")
    state.save(durable)

    planned = state.create("owner/model", "d" * 40, "base")
    state.set_files(
        planned,
        [
            {
                "path": "weights.bin",
                "size": 4,
                "sha256": observed,
                "lfs": True,
            }
        ],
    )
    shards = tmp_path / "shards"
    original_jobs_dir = state.JOBS_DIR
    try:
        state.JOBS_DIR = shards
        state.save(planned)
    finally:
        state.JOBS_DIR = original_jobs_dir
    (shards / "shard-0.json").write_text(
        '["weights.bin"]\n', encoding="utf-8"
    )
    results = tmp_path / "results"
    results.mkdir()
    (results / "shard-0-result.json").write_text(
        json.dumps(
            {
                "weights.bin": {
                    "uploaded": True,
                    "sha256_observed": observed,
                    "error": None,
                }
            }
        ),
        encoding="utf-8",
    )

    counts = collect(
        "owner__model",
        shards / "owner__model.json",
        results,
    )

    assert counts == (1, 0, 0)
    saved = state.load("owner__model")
    assert saved["files"]["weights.bin"]["uploaded"] is True
    assert saved["status"] == "verifying"


def test_collect_records_missing_worker_artifact_as_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    job = state.create("owner/model", "e" * 40, "base")
    state.set_files(
        job,
        [{"path": "config.json", "size": 2, "sha256": None, "lfs": False}],
    )
    state.save(job)
    shards = tmp_path / "shards"
    original_jobs_dir = state.JOBS_DIR
    try:
        state.JOBS_DIR = shards
        state.save(job)
    finally:
        state.JOBS_DIR = original_jobs_dir
    (shards / "shard-0.json").write_text(
        '["config.json"]\n', encoding="utf-8"
    )
    results = tmp_path / "results"
    results.mkdir()

    counts = collect(
        "owner__model",
        shards / "owner__model.json",
        results,
    )

    assert counts == (0, 1, 1)
    saved = state.load("owner__model")
    assert saved["files"]["config.json"]["attempts"] == 1
    assert "no result artifact" in saved["files"]["config.json"]["last_error"]
