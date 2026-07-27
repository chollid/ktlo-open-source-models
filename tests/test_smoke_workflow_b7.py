"""Offline contract checks for the credentialed Batch 7 workflow."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


WORKFLOW = Path(".github/workflows/smoke.yml").read_text(encoding="utf-8")


def test_smoke_workflow_is_manual_only_and_least_privilege() -> None:
    trigger_block = WORKFLOW.split("permissions:", 1)[0]

    assert "workflow_dispatch:" in trigger_block
    assert "repository_dispatch:" not in trigger_block
    assert "schedule:" not in trigger_block
    assert "pull_request:" not in trigger_block
    assert "pull_request_target:" not in trigger_block
    assert "permissions:\n  contents: read" in WORKFLOW
    assert "contents: write" not in WORKFLOW
    assert "group: model-archive-smoke-prefix" in WORKFLOW


def test_smoke_workflow_fails_fast_with_every_exact_secret_name() -> None:
    required = (
        "HF_TOKEN",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_ACCOUNT_ID",
        "R2_BUCKET",
        "NOTIFY_WEBHOOK",
    )

    for name in required:
        assert f"{name}: ${{{{ secrets.{name} }}}}" in WORKFLOW
    assert (
        "ERROR: required GitHub Actions secret ${secret_name} is missing or empty"
        in WORKFLOW
    )
    assert "DISPATCH_PAT" not in WORKFLOW
    assert "set -x" not in WORKFLOW


def test_smoke_workflow_drives_every_frozen_component_and_real_prefix() -> None:
    assert "hf-internal-testing/tiny-random-gpt2" in WORKFLOW
    assert "printf 'R2_BUCKET=%s/smoke" in WORKFLOW
    assert "rclone v1.74.4" in WORKFLOW

    for command in (
        "hfmeta.resolve_revision",
        "hfmeta.list_repo_files",
        "state.create",
        "state.set_files",
        "state.save",
        "python plan_shards.py",
        "bash grab_shard.sh",
        "python collect_shards.py",
        "python sweeper.py --dry-run",
        "python verify_remote.py",
        "bash scripts/smoke_local.sh",
    ):
        assert command in WORKFLOW


def test_smoke_workflow_asserts_both_failures_and_their_recovery() -> None:
    assert "rclone deletefile" in WORKFLOW
    assert "summary.dispatched_jobs != 1" in WORKFLOW
    assert "pending != [target]" in WORKFLOW
    assert "L4 RECOVERY ASSERTION PASSED" in WORKFLOW

    assert "same-length wrong bytes" in WORKFLOW
    assert "MISMATCH ${SAFE_NAME}:${CORRUPT_TARGET} sha256 mismatch" in WORKFLOW
    assert 'item["attempts"] != 1' in WORKFLOW
    assert "mismatched object was not deleted" in WORKFLOW
    assert "L5 RECOVERY ASSERTION PASSED" in WORKFLOW


def test_smoke_workflow_measures_process_tree_rss_and_reports_rates() -> None:
    assert 'Path("/proc").glob("[0-9]*/status")' in WORKFLOW
    assert "aggregate_tree_rss_kib" in WORKFLOW
    assert "peak_kib > 4 * 1024 * 1024" in WORKFLOW
    assert "--s3-chunk-size 64M" in WORKFLOW
    assert "--s3-upload-concurrency 4" in WORKFLOW
    assert 'PARALLEL_TRANSFERS: "8"' in WORKFLOW
    assert "Observed upload throughput:" in WORKFLOW
    assert "Total measured workflow wall time:" in WORKFLOW


def test_smoke_workflow_always_cleans_and_asserts_notification_delivery() -> None:
    assert (
        "if: always() && steps.rclone.outcome == 'success'\n"
        "        run: |\n"
        "          if [[ \\"
    ) in WORKFLOW
    assert (
        "          set +e\n"
        '          rclone purge "r2:${SMOKE_BUCKET}"'
    ) in WORKFLOW
    assert WORKFLOW.count(
        "ERROR: refusing R2 smoke purge; target must end in /smoke "
        "and differ from the bare bucket"
    ) == 2
    assert WORKFLOW.count("$SMOKE_BUCKET != */smoke") == 2
    assert WORKFLOW.count('$SMOKE_BUCKET == "$R2_BUCKET_BASE"') == 2
    assert WORKFLOW.count('rclone purge "r2:${SMOKE_BUCKET}"') == 2
    assert 'rclone purge "r2:${R2_BUCKET}"' not in WORKFLOW
    assert (
        'rclone deletefile \\\n'
        '            "r2:${SMOKE_BUCKET}/${SAFE_NAME}/${MISSING_TARGET}"'
    ) in WORKFLOW
    assert (
        '"r2:${SMOKE_BUCKET}/${SAFE_NAME}/${CORRUPT_TARGET}"'
        in WORKFLOW
    )
    assert "cleanup left {len(objects)} object(s)" in WORKFLOW
    assert "from lib.notify import notify" in WORKFLOW
    assert "requests.post = recording_post" in WORKFLOW
    assert "responses[0].raise_for_status()" in WORKFLOW


def test_stale_cleanup_refuses_a_bare_bucket_without_calling_rclone(
    tmp_path: Path,
) -> None:
    marker = "- name: Remove stale smoke objects before starting\n"
    start = WORKFLOW.index(marker) + len(marker)
    run_marker = "        run: |\n"
    run_start = WORKFLOW.index(run_marker, start) + len(run_marker)
    next_step = WORKFLOW.index("\n      - name:", run_start)
    lines = WORKFLOW[run_start:next_step].splitlines()
    script = "\n".join(
        line[10:] if line.startswith("          ") else line for line in lines
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rclone_log = tmp_path / "rclone-called"
    fake_rclone = fake_bin / "rclone"
    fake_rclone.write_text(
        "#!/usr/bin/env bash\n"
        'printf "called\\n" >"$RCLONE_GUARD_LOG"\n',
        encoding="utf-8",
    )
    fake_rclone.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "RCLONE_GUARD_LOG": str(rclone_log),
            "SMOKE_BUCKET": "model-archive",
            "R2_BUCKET_BASE": "model-archive",
        }
    )

    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "ERROR: refusing R2 smoke purge" in result.stdout
    assert not rclone_log.exists()
