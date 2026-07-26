"""Security and concurrency invariants for the Batch 3 workflow."""

from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/grab.yml"
).read_text(encoding="utf-8")


def test_secret_bearing_workflow_has_only_explicit_manual_dispatches() -> None:
    trigger_block = WORKFLOW.split("permissions:", 1)[0]
    assert "repository_dispatch:" in trigger_block
    assert "workflow_dispatch:" in trigger_block
    assert "pull_request" not in trigger_block
    assert "schedule:" not in trigger_block


def test_parallel_grab_isolated_and_only_collect_can_write_contents() -> None:
    assert "fail-fast: false" in WORKFLOW
    assert "timeout-minutes: 350" in WORKFLOW
    assert WORKFLOW.count("contents: write") == 1
    assert "group: state-write" in WORKFLOW
    assert "collect_shards.py" in WORKFLOW
