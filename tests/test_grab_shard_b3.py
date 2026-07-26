"""Offline integration tests for the real streaming shell pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from lib import state


PYTHON = Path(os.environ["HOME"]) / ".pyenv/versions/3.12.13/bin/python"
RCLONE = Path("/opt/homebrew/bin/rclone")
REPO_ROOT = Path(__file__).resolve().parents[1]
GRABBER = REPO_ROOT / "grab_shard.sh"
REVISION = "c" * 40
REPO_ID = "owner/model"
SAFE_NAME = "owner__model"


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _start_http_server(
    root: Path,
) -> tuple[subprocess.Popen[bytes] | None, str]:
    try:
        port = _free_port()
    except PermissionError:
        # The managed Codex sandbox blocks all socket binds. The same bytes still
        # traverse real curl and rclone through file:// in that environment.
        return None, root.as_uri()
    else:
        process = subprocess.Popen(
            [
                str(PYTHON),
                "-m",
                "http.server",
                str(port),
                "--bind",
                "127.0.0.1",
                "--directory",
                str(root),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.2)
        base_url = f"http://127.0.0.1:{port}"
    if process.poll() is not None:
        raise RuntimeError("local HTTP server failed to start")
    return process, base_url


def _write_job(tmp_path: Path, expected_body: bytes, *, uploaded: bool = False) -> None:
    job = state.create(REPO_ID, REVISION, "base")
    state.set_files(
        job,
        [
            {
                "path": "weights.bin",
                "size": len(expected_body),
                "sha256": hashlib.sha256(expected_body).hexdigest(),
                "lfs": True,
            }
        ],
    )
    if uploaded:
        state.mark_uploaded(
            job, "weights.bin", hashlib.sha256(expected_body).hexdigest()
        )
    state.save(job)
    shards = tmp_path / "shards"
    shards.mkdir()
    (shards / "shard-0.json").write_text(
        '["weights.bin"]\n', encoding="utf-8"
    )


def _environment(
    tmp_path: Path,
    base_url: str,
    *,
    limit_rate: str | None = None,
    report_curl_pid: bool = False,
) -> dict[str, str]:
    config = tmp_path / "rclone.conf"
    config.write_text("[r2]\ntype = local\n", encoding="utf-8")
    destination = tmp_path / "remote"
    destination.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "CURL_BIN": "/usr/bin/curl",
            "DURABLE_STATE_FILE": f"state/jobs/{SAFE_NAME}.json",
            "GRAB_BASE_URL": base_url,
            "GRAB_CURL_RETRIES": "0",
            "GRAB_USE_FIFO": "1",
            "HF_TOKEN": "offline-test-token",
            "PYTHON_BIN": str(PYTHON),
            "R2_BUCKET": str(destination),
            "RCLONE_BIN": str(RCLONE),
            "RCLONE_CONFIG": str(config),
            "RESULT_DIR": str(tmp_path),
            "SHARD_PLAN_FILE": f"state/jobs/{SAFE_NAME}.json",
        }
    )
    if limit_rate is not None:
        env["GRAB_CURL_LIMIT_RATE"] = limit_rate
    if report_curl_pid:
        wrapper = tmp_path / "curl-with-pid"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "/usr/bin/curl \"$@\" &\n"
            "curl_pid=$!\n"
            "printf '%s\\n' \"$curl_pid\" >\"$GRAB_CURL_PID_FILE\"\n"
            "wait \"$curl_pid\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        env["CURL_BIN"] = str(wrapper)
        env["GRAB_CURL_PID_FILE"] = str(tmp_path / "curl.pid")
    return env


def _command() -> list[str]:
    return [
        "/opt/homebrew/bin/bash",
        str(GRABBER),
        "shards/shard-0.json",
        REPO_ID,
        REVISION,
        SAFE_NAME,
    ]


def _result(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "shard-0-result.json").read_text(encoding="utf-8")
    )


def test_corrupted_equal_length_body_fails_sha256(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    expected = b"expected archive bytes"
    corrupted = b"corrupt! archive bytes"
    assert len(corrupted) == len(expected)
    _write_job(tmp_path, expected)
    served = tmp_path / f"http/{REPO_ID}/resolve/{REVISION}"
    served.mkdir(parents=True)
    (served / "weights.bin").write_bytes(corrupted)
    server, base_url = _start_http_server(tmp_path / "http")
    try:
        completed = subprocess.run(
            _command(),
            cwd=tmp_path,
            env=_environment(tmp_path, base_url),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=5)

    assert completed.returncode == 0, completed.stderr
    result = _result(tmp_path)["weights.bin"]
    assert result["uploaded"] is False
    assert result["sha256_observed"] == hashlib.sha256(corrupted).hexdigest()
    assert "SHA256 mismatch" in result["error"]
    assert state.load(SAFE_NAME)["files"]["weights.bin"]["uploaded"] is False


def test_mid_file_server_kill_is_not_marked_uploaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    expected = b"x" * (32 * 1024 * 1024)
    _write_job(tmp_path, expected)
    served = tmp_path / f"http/{REPO_ID}/resolve/{REVISION}"
    served.mkdir(parents=True)
    (served / "weights.bin").write_bytes(expected)
    server, base_url = _start_http_server(tmp_path / "http")
    use_pid_wrapper = server is None
    process = subprocess.Popen(
        _command(),
        cwd=tmp_path,
        env=_environment(
            tmp_path,
            base_url,
            limit_rate="256k",
            report_curl_pid=use_pid_wrapper,
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if server is None:
        pid_file = tmp_path / "curl.pid"
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        os.kill(int(pid_file.read_text(encoding="utf-8")), 9)
    else:
        time.sleep(0.8)
        server.terminate()
        server.wait(timeout=5)
    stdout, stderr = process.communicate(timeout=20)

    assert process.returncode == 0, f"{stdout}\n{stderr}"
    result = _result(tmp_path)["weights.bin"]
    assert result["uploaded"] is False
    assert result["error"]
    assert (
        "curl exited" in result["error"]
        or "rclone rcat exited" in result["error"]
        or "byte count mismatch" in result["error"]
    )
    assert state.load(SAFE_NAME)["files"]["weights.bin"]["uploaded"] is False


def test_already_uploaded_shard_is_fast_noop_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_job(tmp_path, b"already uploaded", uploaded=True)
    started = time.monotonic()

    completed = subprocess.run(
        _command(),
        cwd=tmp_path,
        env=_environment(tmp_path, "http://127.0.0.1:1"),
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert time.monotonic() - started < 3
    assert _result(tmp_path) == {}
