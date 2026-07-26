"""Security, trigger, and state-order invariants for the watcher workflow."""

from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/watch.yml"
).read_text(encoding="utf-8")


def test_watcher_workflow_has_only_safe_triggers_and_serialized_state_writes():
    trigger_block = WORKFLOW.split("permissions:", 1)[0]
    assert 'cron: "0 */3 * * *"' in trigger_block
    assert "workflow_dispatch:" in trigger_block
    assert "pull_request" not in trigger_block
    assert "repository_dispatch" not in trigger_block
    assert "contents: write" in WORKFLOW
    assert "group: state-write" in WORKFLOW
    assert "cancel-in-progress: false" in WORKFLOW


def test_watcher_uses_github_token_with_optional_dispatch_pat_override():
    assert "secrets.DISPATCH_PAT" in WORKFLOW
    assert "secrets.GITHUB_TOKEN" in WORKFLOW
    assert WORKFLOW.index("run: python watcher.py") < WORKFLOW.index("git add state/")
