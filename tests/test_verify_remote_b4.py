from __future__ import annotations

import hashlib
import tracemalloc
from pathlib import Path

import pytest

from lib import state
import verify_remote


RCLONE = "/opt/homebrew/bin/rclone"
REVISION = "b" * 40
SAFE_NAME = "owner__model"


class SyntheticZeroStream:
    def __init__(self, total_bytes: int):
        self.remaining = total_bytes

    def readinto(self, buffer: bytearray) -> int:
        count = min(len(buffer), self.remaining)
        self.remaining -= count
        return count


def _measure_peak(total_bytes: int) -> tuple[int, int]:
    tracemalloc.start()
    try:
        _digest, observed = verify_remote.streaming_sha256(
            SyntheticZeroStream(total_bytes)
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return observed, peak


def test_streaming_sha256_memory_is_flat_for_large_synthetic_stream():
    small_bytes, small_peak = _measure_peak(4 * 1024 * 1024)
    large_bytes, large_peak = _measure_peak(256 * 1024 * 1024)

    assert small_bytes == 4 * 1024 * 1024
    assert large_bytes == 256 * 1024 * 1024
    assert large_peak < 2 * 1024 * 1024
    assert abs(large_peak - small_peak) < 128 * 1024


@pytest.fixture
def remote_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    jobs_dir = tmp_path / "state" / "jobs"
    bucket = tmp_path / "remote" / "bucket"
    config = tmp_path / "rclone.conf"
    config.write_text("[r2]\ntype = local\n", encoding="utf-8")
    monkeypatch.setattr(state, "JOBS_DIR", jobs_dir)
    monkeypatch.setenv("RCLONE_CONFIG", str(config))

    bodies = {
        "weights.bin": b"declared model bytes",
        "config.json": b'{"architecture":"test"}',
    }
    files = [
        {
            "path": "weights.bin",
            "size": len(bodies["weights.bin"]),
            "sha256": hashlib.sha256(bodies["weights.bin"]).hexdigest(),
            "lfs": True,
        },
        {
            "path": "config.json",
            "size": len(bodies["config.json"]),
            "sha256": None,
            "lfs": False,
        },
    ]
    job = state.create("owner/model", REVISION, "base")
    state.set_files(job, files)
    for metadata in files:
        state.mark_uploaded(
            job,
            metadata["path"],
            metadata["sha256"] or ("0" * 64),
        )
        destination = bucket / SAFE_NAME / metadata["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(bodies[metadata["path"]])
    state.save(job)
    return job, bucket, bodies


def test_real_rclone_verifies_lfs_and_size_only_file(
    remote_job, capsys: pytest.CaptureFixture[str]
):
    job, bucket, bodies = remote_job

    report = verify_remote.verify_job(
        job, bucket=str(bucket), rclone_bin=RCLONE
    )

    current = state.load(SAFE_NAME)
    assert current is not None
    assert current["status"] == "verified"
    assert report.bytes_verified == sum(map(len, bodies.values()))
    assert report.size_only == ("config.json",)
    assert "size_only=true" in capsys.readouterr().out


def test_sha_mismatch_deletes_resets_and_records_failure(remote_job):
    job, bucket, bodies = remote_job
    bad = bucket / SAFE_NAME / "weights.bin"
    bad.write_bytes(b"x" * len(bodies["weights.bin"]))

    report = verify_remote.verify_job(
        job, bucket=str(bucket), rclone_bin=RCLONE
    )

    current = state.load(SAFE_NAME)
    assert current is not None
    assert report.failed == 1
    assert not bad.exists()
    assert current["status"] == "uploading"
    assert current["files"]["weights.bin"]["uploaded"] is False
    assert current["files"]["weights.bin"]["verified"] is False
    assert current["files"]["weights.bin"]["attempts"] == 1
    assert "remote verification failed" in (
        current["files"]["weights.bin"]["last_error"] or ""
    )


def test_limit_and_max_bytes_bound_the_pass(remote_job):
    job, bucket, bodies = remote_job
    config_size = len(bodies["config.json"])

    report = verify_remote.verify_job(
        job,
        bucket=str(bucket),
        rclone_bin=RCLONE,
        limit=1,
        max_bytes=config_size,
    )

    current = state.load(SAFE_NAME)
    assert current is not None
    assert report.examined == 1
    assert report.bytes_read <= config_size
    assert current["files"]["config.json"]["verified"] is True
    assert current["files"]["weights.bin"]["verified"] is False


def test_verifier_dry_run_reads_but_mutates_nothing(remote_job):
    job, bucket, _bodies = remote_job
    before = (state.JOBS_DIR / f"{SAFE_NAME}.json").read_bytes()

    report = verify_remote.verify_job(
        job,
        bucket=str(bucket),
        rclone_bin=RCLONE,
        dry_run=True,
    )

    assert report.verified == 2
    assert (state.JOBS_DIR / f"{SAFE_NAME}.json").read_bytes() == before
    current = state.load(SAFE_NAME)
    assert current is not None
    assert not any(item["verified"] for item in current["files"].values())
