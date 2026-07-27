"""Offline behavior checks for the operator-side smoke wrapper."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_LOCAL = REPO_ROOT / "scripts" / "smoke_local.sh"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _fake_install(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    bin_dir = tmp_path / "bin"
    log = tmp_path / "calls.log"
    scripts.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(SMOKE_LOCAL, scripts / "smoke_local.sh")

    _write_executable(
        scripts / "pull.sh",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'pull|%s\\n' "$*" >>"$SMOKE_CALL_LOG"
if (($# == 2)); then
  exit "${FAKE_BUDGET_STATUS:-8}"
fi
exit 0
""",
    )
    _write_executable(
        scripts / "verify_local.sh",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'verify|%s\\n' "$*" >>"$SMOKE_CALL_LOG"
""",
    )
    _write_executable(
        scripts / "reclaim.sh",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'reclaim|%s\\n' "$*" >>"$SMOKE_CALL_LOG"
""",
    )
    _write_executable(
        bin_dir / "rclone",
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  listremotes)
    printf 'r2:\\n'
    ;;
  lsf)
    exit 3
    ;;
  *)
    echo "unexpected fake rclone command: ${1:-}" >&2
    exit 90
    ;;
esac
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "RCLONE_BIN": str(bin_dir / "rclone"),
            "R2_BUCKET": "real-archive-bucket",
            "SMOKE_CALL_LOG": str(log),
            "SMOKE_PULL_BUDGET": "1B",
        }
    )
    return scripts / "smoke_local.sh", env, log


def _run(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        cwd=script.parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_smoke_local_requires_exit_8_then_resumes_verifies_and_reclaims(
    tmp_path: Path,
) -> None:
    script, env, log = _fake_install(tmp_path)

    result = _run(script, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Budget assertion passed" in result.stdout
    assert "SMOKE LOCAL PASSED" in result.stdout
    assert log.read_text(encoding="utf-8").splitlines() == [
        "pull|hf-internal-testing__tiny-random-gpt2 1B",
        "pull|hf-internal-testing__tiny-random-gpt2",
        "verify|hf-internal-testing__tiny-random-gpt2",
        "reclaim|hf-internal-testing__tiny-random-gpt2 --yes",
    ]


def test_smoke_local_rejects_a_budget_run_that_does_not_exit_8(
    tmp_path: Path,
) -> None:
    script, env, log = _fake_install(tmp_path)
    env["FAKE_BUDGET_STATUS"] = "0"

    result = _run(script, env)

    assert result.returncode == 1
    assert "budgeted smoke pull must exit 8, observed 0" in result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "pull|hf-internal-testing__tiny-random-gpt2 1B"
    ]


def test_smoke_local_names_missing_bucket_before_running_any_tool(
    tmp_path: Path,
) -> None:
    script, env, log = _fake_install(tmp_path)
    env.pop("R2_BUCKET")

    result = _run(script, env)

    assert result.returncode != 0
    assert "R2_BUCKET must be set to the real R2 bucket name" in result.stderr
    assert not log.exists()


def test_smoke_local_guards_reclaim_with_a_derived_smoke_only_target() -> None:
    text = SMOKE_LOCAL.read_text(encoding="utf-8")

    assert "guard_smoke_target()" in text
    assert '$target != */smoke' in text
    assert '$target == "$bare_bucket"' in text
    assert 'smoke_bucket="${base_bucket}/smoke"' in text
    assert (
        'guard_smoke_target "$R2_BUCKET" "$base_bucket"\n'
        '"${script_dir}/reclaim.sh" "$safe_name" --yes'
    ) in text
