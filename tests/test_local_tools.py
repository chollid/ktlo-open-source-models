from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import state


REPO_ROOT = Path(__file__).resolve().parents[1]
PULL = REPO_ROOT / "scripts" / "pull.sh"
VERIFY = REPO_ROOT / "scripts" / "verify_local.sh"
RECLAIM = REPO_ROOT / "scripts" / "reclaim.sh"
SAFE_NAME = "test-org__test-model"


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.fixture
def local_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    archive = tmp_path / "archive"
    bucket = tmp_path / "local-r2" / "bucket"
    config = tmp_path / "rclone.conf"
    workspace.mkdir()
    archive.mkdir()
    bucket.mkdir(parents=True)
    config.write_text("[r2]\ntype = local\n", encoding="utf-8")

    monkeypatch.setattr(state, "JOBS_DIR", workspace / "state" / "jobs")

    env = os.environ.copy()
    env.update(
        {
            "ARCHIVE": str(archive),
            "R2_BUCKET": str(bucket),
            "RCLONE_CONFIG": str(config),
            "MODEL_ARCHIVE_PYTHON": sys.executable,
            "MODEL_ARCHIVE_REPO_ROOT": str(workspace),
        }
    )
    env["PATH"] = f"/opt/homebrew/bin:{env.get('PATH', '')}"

    def make_job(files: dict[str, bytes]) -> tuple[Path, Path]:
        remote_model = bucket / SAFE_NAME
        local_model = archive / SAFE_NAME
        remote_model.mkdir(parents=True)

        metadata = []
        for relative_path, content in files.items():
            target = remote_model / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            metadata.append(
                {
                    "path": relative_path,
                    "size": len(content),
                    "sha256": sha256(content),
                    "lfs": True,
                }
            )

        job = state.create("test-org/test-model", "a" * 40, "base")
        state.set_files(job, metadata)
        for item in metadata:
            state.mark_uploaded(job, item["path"], item["sha256"])
            state.mark_verified(job, item["path"])
        state.save(job)
        return remote_model, local_model

    return env, make_job


def run_script(
    script: Path, *args: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def combined(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_pull_budget_stop_exit_8_then_resume_exit_0(local_backend):
    env, make_job = local_backend
    files = {
        f"shards/model-{index:02d}.safetensors": bytes([index]) * (2 * 1024 * 1024)
        for index in range(1, 13)
    }
    remote_model, local_model = make_job(files)

    stopped = run_script(PULL, SAFE_NAME, "3M", env=env)
    assert stopped.returncode == 8, combined(stopped)
    assert "budget reached" in combined(stopped)
    assert "Resume:" in combined(stopped)
    assert "Effective transfers: 1" in combined(stopped)
    assert (
        "BUDGET REPORT: requested 3M (3145728 bytes); "
        "actual 2097152 bytes in 1 completed file(s)"
    ) in combined(stopped)
    assert 0 < len(list(local_model.rglob("*.safetensors"))) < len(files)
    assert not list(local_model.rglob("*.partial"))

    resumed = run_script(PULL, SAFE_NAME, env=env)
    assert resumed.returncode == 0, combined(resumed)
    assert "PULL COMPLETE" in combined(resumed)

    checked = subprocess.run(
        [
            "/opt/homebrew/bin/rclone",
            "check",
            str(remote_model),
            str(local_model),
            "--size-only",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, combined(checked)
    assert {
        path.relative_to(local_model): sha256(path.read_bytes())
        for path in local_model.rglob("*")
        if path.is_file()
    } == {
        Path(name): sha256(content)
        for name, content in files.items()
    }


def test_pull_five_files_caps_eight_slots_and_honors_budget(local_backend):
    env, make_job = local_backend
    files = {
        f"part-{index}.bin": bytes([index]) * (2 * 1024 * 1024)
        for index in range(5)
    }
    _, local_model = make_job(files)

    result = run_script(PULL, SAFE_NAME, "5M", env=env)

    assert result.returncode == 8, combined(result)
    assert "Effective transfers: 2" in combined(result)
    assert (
        "BUDGET REPORT: requested 5M (5242880 bytes); "
        "actual 4194304 bytes in 2 completed file(s)"
    ) in combined(result)
    assert len(list(local_model.glob("*.bin"))) == 2


def test_pull_four_huge_files_with_sub_file_budget_stops_before_transfer(
    local_backend,
):
    env, make_job = local_backend
    files = {
        f"huge-{index}.bin": bytes([index]) * (2 * 1024 * 1024)
        for index in range(4)
    }
    _, local_model = make_job(files)

    result = run_script(PULL, SAFE_NAME, "1M", env=env)

    assert result.returncode == 8, combined(result)
    assert "Effective transfers: 1" in combined(result)
    assert "WARNING: BUDGET IS SMALLER THAN THE LARGEST PENDING FILE" in combined(
        result
    )
    assert (
        "BUDGET REPORT: requested 1M (1048576 bytes); "
        "actual 0 bytes in 0 completed file(s)"
    ) in combined(result)
    assert "BUDGET BOUND: 1 transfer(s) x 2097152 largest-pending bytes" in combined(
        result
    )
    assert len(list(local_model.glob("*.bin"))) == 0


def test_pull_warns_when_remaining_files_fit_all_effective_slots(local_backend):
    env, make_job = local_backend
    files = {
        "a.bin": b"a" * 1024,
        "b.bin": b"b" * 1024,
    }
    make_job(files)

    result = run_script(PULL, SAFE_NAME, "10M", env=env)

    assert result.returncode == 0, combined(result)
    assert "WARNING: BUDGET MAY NOT BE ENFORCED" in combined(result)
    assert "2 remaining file(s) <= 8 transfer slot(s)" in combined(result)
    assert (
        "BUDGET REPORT: requested 10M (10485760 bytes); "
        "actual 2048 bytes in 2 completed file(s)"
    ) in combined(result)


def test_pull_interrupted_then_rerun_converges(local_backend):
    env, make_job = local_backend
    content = b"interruption-safe" * (512 * 1024)
    remote_model, local_model = make_job({"large/model.safetensors": content})

    process = subprocess.Popen(
        [str(PULL), SAFE_NAME, "--bwlimit", "512K"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    time.sleep(0.8)
    os.killpg(process.pid, signal.SIGINT)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode != 0, stdout + stderr
    assert "Resume:" in stdout + stderr
    assert not list(local_model.rglob("*.partial"))

    resumed = run_script(PULL, SAFE_NAME, env=env)
    assert resumed.returncode == 0, combined(resumed)
    assert (local_model / "large" / "model.safetensors").read_bytes() == content

    checked = subprocess.run(
        [
            "/opt/homebrew/bin/rclone",
            "check",
            str(remote_model),
            str(local_model),
            "--size-only",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, combined(checked)


def test_verify_reads_state_and_repull_hint_lists_only_bad_files(local_backend):
    env, make_job = local_backend
    files = {
        "good.safetensors": b"good",
        "bad-hash.safetensors": b"hash",
        "missing.safetensors": b"missing",
    }
    _, local_model = make_job(files)
    local_model.mkdir(parents=True)
    (local_model / "good.safetensors").write_bytes(files["good.safetensors"])
    (local_model / "bad-hash.safetensors").write_bytes(b"xxxx")

    result = run_script(VERIFY, SAFE_NAME, env=env)
    assert result.returncode == 1, combined(result)
    assert "FAIL  bad-hash.safetensors" in result.stdout
    assert "FAIL  missing.safetensors" in result.stdout
    assert "PASS  good.safetensors" in result.stdout
    repull_hint = result.stderr.split("Re-pull only the bad files with:", 1)[1]
    assert "bad-hash.safetensors" in repull_hint
    assert "missing.safetensors" in repull_hint
    assert "good.safetensors" not in repull_hint
    assert "--ignore-size" in repull_hint


def test_verify_success_marks_job_pulled(local_backend):
    env, make_job = local_backend
    env.pop("MODEL_ARCHIVE_PYTHON")
    files = {"model.safetensors": b"verified-local-copy"}
    remote_model, local_model = make_job(files)
    shutil.copytree(remote_model, local_model)

    result = run_script(VERIFY, SAFE_NAME, env=env)
    assert result.returncode == 0, combined(result)
    assert "LOCAL VERIFICATION PASSED: 1 files" in result.stdout
    assert state.load(SAFE_NAME)["status"] == "pulled"


def test_reclaim_refuses_missing_local_file(local_backend):
    env, make_job = local_backend
    files = {"a.safetensors": b"alpha", "b.safetensors": b"bravo"}
    remote_model, local_model = make_job(files)
    shutil.copytree(remote_model, local_model)
    (local_model / "a.safetensors").unlink()

    result = run_script(RECLAIM, SAFE_NAME, "--yes", env=env)
    assert result.returncode == 1, combined(result)
    assert "a.safetensors (missing)" in result.stderr
    assert "REFUSED" in result.stderr
    assert (remote_model / "a.safetensors").exists()
    assert (remote_model / "b.safetensors").exists()


def test_reclaim_refuses_size_mismatch(local_backend):
    env, make_job = local_backend
    files = {"model.safetensors": b"correct-size"}
    remote_model, local_model = make_job(files)
    shutil.copytree(remote_model, local_model)
    (local_model / "model.safetensors").write_bytes(b"short")

    result = run_script(RECLAIM, SAFE_NAME, "--yes", env=env)
    assert result.returncode == 1, combined(result)
    assert "size 5, expected 12" in result.stderr
    assert "REFUSED" in result.stderr
    assert (remote_model / "model.safetensors").exists()


def test_reclaim_refuses_hash_mismatch(local_backend):
    env, make_job = local_backend
    files = {"model.safetensors": b"right"}
    remote_model, local_model = make_job(files)
    shutil.copytree(remote_model, local_model)
    (local_model / "model.safetensors").write_bytes(b"wrong")

    result = run_script(RECLAIM, SAFE_NAME, "--yes", env=env)
    assert result.returncode == 1, combined(result)
    assert "sha256" in result.stderr
    assert "REFUSED" in result.stderr
    assert (remote_model / "model.safetensors").exists()


def test_reclaim_refuses_absent_yes_after_printing_exact_plan(local_backend):
    env, make_job = local_backend
    files = {"model.safetensors": b"keep-until-confirmed"}
    remote_model, local_model = make_job(files)
    shutil.copytree(remote_model, local_model)

    result = run_script(RECLAIM, SAFE_NAME, env=env)
    assert result.returncode == 2, combined(result)
    assert "R2 DELETION PLAN:" in result.stdout
    assert (
        f"r2:{env['R2_BUCKET']}/{SAFE_NAME}/model.safetensors "
        f"({len(files['model.safetensors'])} bytes)"
    ) in result.stdout
    assert f"TOTAL: 1 object(s), {len(files['model.safetensors'])} bytes" in result.stdout
    assert "DRY RUN ONLY" in result.stderr
    assert (remote_model / "model.safetensors").exists()


def test_reclaim_verified_only_deletes_only_freshly_verified_files(local_backend):
    env, make_job = local_backend
    files = {"present.safetensors": b"present", "pending.safetensors": b"pending"}
    remote_model, local_model = make_job(files)
    local_model.mkdir(parents=True)
    (local_model / "present.safetensors").write_bytes(files["present.safetensors"])

    result = run_script(
        RECLAIM, SAFE_NAME, "--verified-only", "--yes", env=env
    )
    assert result.returncode == 0, combined(result)
    assert "SKIPPED: 1 object(s)" in result.stdout
    assert not (remote_model / "present.safetensors").exists()
    assert (remote_model / "pending.safetensors").exists()
