#!/usr/bin/env python3
"""Build deterministic upload shards without mutating durable job state."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Iterable

from lib import hfmeta, state


TARGET_SHARD_BYTES = 300_000_000_000
MAX_SHARDS = 20


def pack_shards(
    files: Iterable[tuple[str, int]],
    *,
    target_bytes: int = TARGET_SHARD_BYTES,
    max_shards: int = MAX_SHARDS,
) -> list[list[str]]:
    """Pack files largest-first into the least-loaded eligible shard."""

    if target_bytes <= 0:
        raise ValueError("target_bytes must be positive")
    if max_shards <= 0:
        raise ValueError("max_shards must be positive")

    ordered = sorted(files, key=lambda item: (-item[1], item[0]))
    for path, size in ordered:
        if not path:
            raise ValueError("file paths must be non-empty")
        if size < 0:
            raise ValueError(f"file size must be non-negative: {path}")
    if not ordered:
        return []

    total_bytes = sum(size for _, size in ordered)
    shard_count = max(1, min(max_shards, math.ceil(total_bytes / target_bytes)))
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    shard_sizes = [0] * shard_count

    for path, size in ordered:
        index = min(
            range(shard_count),
            key=lambda candidate: (shard_sizes[candidate], candidate),
        )
        shards[index].append(path)
        shard_sizes[index] += size

    return shards


def _emit_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def _save_plan_snapshot(job: state.Job, shards_dir: Path) -> Path:
    """Save a validated state snapshot outside the durable state directory."""

    original_jobs_dir = state.JOBS_DIR
    try:
        state.JOBS_DIR = shards_dir
        state.save(job)
    finally:
        state.JOBS_DIR = original_jobs_dir
    return shards_dir / f"{job['safe_name']}.json"


def build_plan(safe_name: str, shards_dir: Path) -> list[list[str]]:
    job = state.load(safe_name)
    if job is None:
        raise SystemExit(f"job state not found for {safe_name}")

    if job["status"] == "discovered":
        files = hfmeta.list_repo_files(
            job["repo_id"],
            job["revision"],
            os.environ.get("HF_TOKEN"),
        )
        state.set_files(job, files)

    pending = state.pending_uploads(job)
    sized_files = [(path, job["files"][path]["size"]) for path in pending]
    shards = pack_shards(sized_files)

    shards_dir.mkdir(parents=True, exist_ok=True)
    for old_manifest in shards_dir.glob("shard-*.json"):
        old_manifest.unlink()
    for index, paths in enumerate(shards):
        manifest = shards_dir / f"shard-{index}.json"
        manifest.write_text(
            json.dumps(paths, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    _save_plan_snapshot(job, shards_dir)
    return shards


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--safe-name", required=True)
    parser.add_argument("--shards-dir", type=Path, default=Path("shards"))
    args = parser.parse_args()

    shards = build_plan(args.safe_name, args.shards_dir)
    matrix = {"shard": list(range(len(shards)))}
    _emit_output("matrix", json.dumps(matrix, separators=(",", ":")))
    _emit_output("skip", "true" if not shards else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
