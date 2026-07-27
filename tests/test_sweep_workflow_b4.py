from pathlib import Path


WORKFLOW = Path(".github/workflows/sweep.yml")


def test_sweep_workflow_has_only_safe_triggers_and_locked_serialization():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert 'cron: "0 */2 * * *"' in text
    assert "workflow_dispatch:" in text
    assert "pull_request:" not in text
    assert "pull_request_target:" not in text
    assert "repository_dispatch:" not in text
    assert "permissions:\n  contents: write" in text
    assert "group: state-write" in text
    assert "cancel-in-progress: false" in text
    assert "timeout-minutes: 350" in text


def test_sweep_pushes_reconciled_state_before_recovery_dispatch():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert 'SWEEPER_PUSH_BEFORE_DISPATCH: "1"' in text
    assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in text
    assert "DISPATCH_PAT: ${{ secrets.DISPATCH_PAT }}" in text
    assert "python sweeper.py" in text
    assert "- name: Commit final verification state\n        if: always()" in text
    assert "git add state/jobs/" in text
