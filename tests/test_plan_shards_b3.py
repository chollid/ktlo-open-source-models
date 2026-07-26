"""Batch 3 shard-planning tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from lib import state
from plan_shards import build_plan, pack_shards


PYTHON = Path(os.environ["HOME"]) / ".pyenv/versions/3.12.13/bin/python"
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_largest_first_balances_uneven_sizes() -> None:
    shards = pack_shards(
        [("a", 80), ("b", 70), ("c", 30), ("d", 20)],
        target_bytes=100,
    )

    assert shards == [["a", "d"], ["b", "c"]]


def test_oversized_file_gets_own_shard_and_is_never_dropped() -> None:
    files = [("huge", 250), ("medium", 60), ("small", 40)]

    shards = pack_shards(files, target_bytes=100)

    assert shards[0] == ["huge"]
    assert sorted(path for shard in shards for path in shard) == [
        "huge",
        "medium",
        "small",
    ]
    assert len(shards) == 4


def test_zero_files_has_no_shards() -> None:
    assert pack_shards([], target_bytes=100) == []


def test_exact_boundary_uses_one_shard() -> None:
    assert pack_shards([("a", 60), ("b", 40)], target_bytes=100) == [
        ["a", "b"]
    ]


def test_cli_zero_pending_emits_skip_and_does_not_mutate_durable_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    job = state.create("owner/model", "a" * 40, "base")
    state.set_files(job, [])
    state.save(job)
    durable_path = tmp_path / "state/jobs/owner__model.json"
    before = durable_path.read_bytes()
    output_path = tmp_path / "github-output"
    env = os.environ.copy()
    env.update(
        {
            "GITHUB_OUTPUT": str(output_path),
            "PYTHONPATH": str(REPO_ROOT),
        }
    )

    completed = subprocess.run(
        [
            str(PYTHON),
            str(REPO_ROOT / "plan_shards.py"),
            "--safe-name",
            "owner__model",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert output_path.read_text(encoding="utf-8").splitlines() == [
        'matrix={"shard":[]}',
        "skip=true",
    ]
    assert durable_path.read_bytes() == before
    assert json.loads(
        (tmp_path / "shards/owner__model.json").read_text(encoding="utf-8")
    )["status"] == "planned"
    assert list((tmp_path / "shards").glob("shard-*.json")) == []


def test_build_plan_writes_all_manifests_without_saving_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    job = state.create("owner/model", "b" * 40, "base")
    state.set_files(
        job,
        [
            {"path": "a", "size": 200_000_000_000, "sha256": None, "lfs": False},
            {"path": "b", "size": 100_000_000_000, "sha256": None, "lfs": False},
        ],
    )
    state.save(job)
    durable = (tmp_path / "state/jobs/owner__model.json").read_bytes()

    shards = build_plan("owner__model", tmp_path / "shards")

    assert shards == [["a", "b"]]
    assert json.loads(
        (tmp_path / "shards/shard-0.json").read_text(encoding="utf-8")
    ) == ["a", "b"]
    assert (tmp_path / "state/jobs/owner__model.json").read_bytes() == durable
