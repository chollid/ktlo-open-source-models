#!/usr/bin/env python3
"""Merge immutable shard artifacts into durable state exactly once."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, cast

from lib import state
from lib.hfmeta import FileMeta
from lib.notify import notify


def _load_plan_snapshot(safe_name: str, plan_file: Path) -> state.Job:
    if plan_file.name != f"{safe_name}.json":
        raise ValueError("plan snapshot filename must match safe_name")
    original_jobs_dir = state.JOBS_DIR
    try:
        state.JOBS_DIR = plan_file.parent
        plan = state.load(safe_name)
    finally:
        state.JOBS_DIR = original_jobs_dir
    if plan is None:
        raise FileNotFoundError(f"plan snapshot not found: {plan_file}")
    return plan


def _read_result(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid shard result artifact: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"shard result must be an object: {path}")
    return cast(dict[str, dict[str, Any]], raw)


def _expected_paths(plan_dir: Path) -> set[str]:
    expected: set[str] = set()
    for manifest in sorted(plan_dir.glob("shard-*.json")):
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(
            isinstance(path, str) and path for path in raw
        ):
            raise ValueError(f"invalid shard manifest: {manifest}")
        overlap = expected.intersection(raw)
        if overlap:
            duplicate = min(overlap)
            raise ValueError(f"file appears in multiple shards: {duplicate}")
        expected.update(raw)
    return expected


def _write_output(name: str, value: int | str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def collect(
    safe_name: str,
    plan_file: Path,
    results_dir: Path,
) -> tuple[int, int, int]:
    job = state.load(safe_name)
    if job is None:
        raise FileNotFoundError(f"durable job state not found for {safe_name}")
    before = json.dumps(job, sort_keys=True)
    plan = _load_plan_snapshot(safe_name, plan_file)

    identity_fields = ("repo_id", "safe_name", "revision")
    if any(job[field] != plan[field] for field in identity_fields):
        raise ValueError("plan snapshot does not match durable job identity")

    if job["status"] == "discovered":
        files = [
            cast(
                FileMeta,
                {
                    "path": path,
                    "size": file_state["size"],
                    "sha256": file_state["sha256"],
                    "lfs": file_state["lfs"],
                },
            )
            for path, file_state in plan["files"].items()
        ]
        state.set_files(job, files)
    elif job["files"] != plan["files"]:
        raise ValueError("plan snapshot file metadata does not match durable state")

    merged: dict[str, dict[str, Any]] = {}
    for result_file in sorted(results_dir.glob("shard-*-result.json")):
        for path, result in _read_result(result_file).items():
            if path in merged:
                raise ValueError(f"duplicate shard result for file: {path}")
            merged[path] = result

    expected = _expected_paths(plan_file.parent)
    unexpected = set(merged).difference(expected)
    if unexpected:
        raise ValueError(f"result contains unplanned file: {min(unexpected)}")
    for path in expected.difference(merged):
        merged[path] = {
            "uploaded": False,
            "sha256_observed": None,
            "error": "shard worker produced no result artifact",
        }

    state.merge_shard_results(job, merged)
    if json.dumps(job, sort_keys=True) != before:
        state.save(job)

    uploaded = sum(item["uploaded"] for item in job["files"].values())
    failed = sum(
        not item["uploaded"] and item["last_error"] is not None
        for item in job["files"].values()
    )
    remaining = sum(not item["uploaded"] for item in job["files"].values())
    notify(
        f"Grab complete for {job['repo_id']}: "
        f"{uploaded} uploaded, {failed} failed, {remaining} remaining",
        level="error" if failed else "info",
    )
    return uploaded, failed, remaining


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--safe-name", required=True)
    parser.add_argument("--plan-file", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    args = parser.parse_args()

    uploaded, failed, remaining = collect(
        args.safe_name,
        args.plan_file,
        args.results_dir,
    )
    _write_output("uploaded", uploaded)
    _write_output("failed", failed)
    _write_output("remaining", remaining)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
