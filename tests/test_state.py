from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import state


SHA = "a" * 40
HASH_A = "1" * 64
HASH_B = "2" * 64


def metadata():
    return [
        {"path": "a.safetensors", "size": 100, "sha256": HASH_A, "lfs": True},
        {"path": "config.json", "size": 20, "sha256": None, "lfs": False},
    ]


def planned_job():
    job = state.create("org/model", SHA, "base")
    state.set_files(job, metadata())
    return job


@pytest.mark.parametrize(
    ("repo_id", "expected"),
    [
        ("moonshotai/Kimi-K3", "moonshotai__Kimi-K3"),
        ("Org/Model.Name-v1", "Org__Model.Name-v1"),
        ("a/b/c", "a__b__c"),
        ("single", "single"),
    ],
)
def test_safe_name_edge_cases(repo_id, expected):
    assert state.safe_name(repo_id) == expected


@pytest.mark.parametrize("repo_id", ["", None])
def test_safe_name_rejects_empty_or_non_string(repo_id):
    with pytest.raises(ValueError):
        state.safe_name(repo_id)


def test_save_is_atomic_and_load_round_trips(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "state" / "jobs"
    monkeypatch.setattr(state, "JOBS_DIR", jobs_dir)
    replacements = []
    real_replace = state.os.replace

    def recording_replace(source, target):
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(state.os, "replace", recording_replace)
    job = planned_job()
    original_updated = job["updated_utc"]
    state.save(job)

    target = jobs_dir / "org__model.json"
    assert target.exists()
    assert len(replacements) == 1
    assert replacements[0][0].parent == target.parent
    assert replacements[0][1] == target
    assert not list(target.parent.glob("*.tmp"))
    assert json.loads(target.read_text())["status"] == "planned"
    assert job["updated_utc"].endswith("Z")
    assert job["updated_utc"] >= original_updated
    assert state.load("org__model") == job
    assert state.load("missing") is None


def test_full_forward_state_progression_and_pending_lists():
    job = state.create("org/model", SHA, "base")
    assert job["status"] == "discovered"
    state.set_files(job, metadata())
    assert job["status"] == "planned"
    assert state.pending_uploads(job) == ["a.safetensors", "config.json"]

    state.mark_uploaded(job, "a.safetensors", HASH_A)
    assert job["status"] == "uploading"
    state.mark_uploaded(job, "config.json", HASH_B)
    assert job["status"] == "verifying"
    assert state.pending_verifications(job) == ["a.safetensors", "config.json"]

    state.mark_verified(job, "a.safetensors")
    assert job["status"] == "verifying"
    state.mark_verified(job, "config.json")
    assert job["status"] == "verified"
    assert state.pending_uploads(job) == []
    assert state.pending_verifications(job) == []

    job["status"] = "pulled"
    state._set_status(job, "pulled")
    assert job["status"] == "pulled"


def test_illegal_transitions_are_rejected_on_operation_and_save(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(state, "JOBS_DIR", tmp_path / "jobs")
    discovered = state.create("org/model", SHA, "base")
    discovered["status"] = "verifying"
    with pytest.raises(state.IllegalTransitionError):
        state.save(discovered)

    job = planned_job()
    with pytest.raises(state.IllegalTransitionError):
        state.set_files(job, metadata())
    with pytest.raises(state.IllegalTransitionError):
        state.mark_verified(job, "a.safetensors")

    state.mark_uploaded(job, "a.safetensors", HASH_A)
    state.mark_uploaded(job, "config.json", HASH_B)
    state.mark_verified(job, "a.safetensors")
    state.mark_verified(job, "config.json")
    state.save(job)
    job["status"] = "uploading"
    with pytest.raises(state.IllegalTransitionError):
        state.save(job)


def test_reset_file_is_the_legal_verified_to_uploading_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "JOBS_DIR", tmp_path / "jobs")
    job = planned_job()
    state.mark_uploaded(job, "a.safetensors", HASH_A)
    state.mark_uploaded(job, "config.json", HASH_B)
    state.mark_verified(job, "a.safetensors")
    state.mark_verified(job, "config.json")
    assert job["status"] == "verified"

    state.reset_file(job, "a.safetensors")
    assert job["status"] == "uploading"
    assert job["files"]["a.safetensors"]["uploaded"] is False
    assert job["files"]["a.safetensors"]["verified"] is False
    state.save(job)


def test_failure_trips_failed_only_past_max_attempts(monkeypatch):
    job = planned_job()
    notifications = []
    monkeypatch.setattr(
        state,
        "notify",
        lambda text, level="info": notifications.append((text, level)),
    )

    for attempt in range(1, state.MAX_ATTEMPTS + 1):
        state.record_failure(job, "a.safetensors", f"failure {attempt}")
        assert job["status"] == "planned"
    state.record_failure(job, "a.safetensors", "failure past the limit")

    assert job["files"]["a.safetensors"]["attempts"] == state.MAX_ATTEMPTS + 1
    assert job["status"] == "failed"
    assert len(notifications) == 1
    assert state.pending_uploads(job) == []
    assert state.pending_verifications(job) == []


def test_record_failure_redacts_secret_values(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_sensitive-value")
    job = planned_job()
    state.record_failure(job, "a.safetensors", "error: hf_sensitive-value")
    assert "hf_sensitive-value" not in job["files"]["a.safetensors"]["last_error"]


@pytest.mark.parametrize(
    ("uploaded", "verified", "expected"),
    [
        ((False, False), (False, False), "uploading"),
        ((True, False), (False, False), "uploading"),
        ((True, False), (True, False), "verifying"),
        ((True, True), (True, False), "verifying"),
        ((True, True), (True, True), "verified"),
    ],
)
def test_recompute_status_partial_progress(uploaded, verified, expected):
    job = planned_job()
    first, second = job["files"].values()
    first["uploaded"], first["verified"] = uploaded
    second["uploaded"], second["verified"] = verified
    assert state.recompute_status(job) == expected


def test_sha256_mismatch_reopens_file_after_job_reaches_verifying():
    job = state.create("acme/m", "a" * 40, "base")
    state.set_files(
        job,
        [
            {
                "path": "f1",
                "size": 10,
                "sha256": "a" * 64,
                "lfs": True,
            },
            {
                "path": "f2",
                "size": 10,
                "sha256": "b" * 64,
                "lfs": True,
            },
        ],
    )
    state.mark_uploaded(job, "f1", "a" * 64)
    state.mark_uploaded(job, "f2", "b" * 64)
    assert job["status"] == "verifying"

    with pytest.raises(state.Sha256MismatchError, match="SHA256 mismatch"):
        state.mark_uploaded(job, "f2", "c" * 64)

    assert job["status"] == "uploading"
    assert job["files"]["f2"]["uploaded"] is False
    assert job["files"]["f2"]["attempts"] == 1


def test_recompute_still_rejects_caller_hand_set_status_regression():
    job = planned_job()
    state.mark_uploaded(job, "a.safetensors", HASH_A)
    state.mark_uploaded(job, "config.json", HASH_B)
    assert job["status"] == "verifying"

    job["status"] = "uploading"
    with pytest.raises(state.IllegalTransitionError):
        state.recompute_status(job)


def test_empty_enumerated_repo_recomputes_to_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "JOBS_DIR", tmp_path / "jobs")
    job = state.create("acme/empty", SHA, "base")
    state.set_files(job, [])
    assert job["status"] == "planned"

    assert state.recompute_status(job) == "verified"
    assert job["status"] == "verified"
    state.save(job)
    assert state.load("acme__empty") == job


def test_sha256_mismatch_increments_attempts_and_raises():
    job = planned_job()
    with pytest.raises(state.Sha256MismatchError, match="SHA256 mismatch"):
        state.mark_uploaded(job, "a.safetensors", HASH_B)
    file_state = job["files"]["a.safetensors"]
    assert file_state["attempts"] == 1
    assert file_state["uploaded"] is False
    assert file_state["last_error"] == "streamed SHA256 did not match HF LFS metadata"
    assert job["status"] == "uploading"


def test_merge_shard_results_records_failures_and_other_successes():
    job = planned_job()
    state.merge_shard_results(
        job,
        {
            "a.safetensors": {
                "uploaded": True,
                "sha256_observed": HASH_A,
                "error": None,
            },
            "config.json": {
                "uploaded": False,
                "sha256_observed": None,
                "error": "curl failed",
            },
        },
    )
    assert job["files"]["a.safetensors"]["uploaded"] is True
    assert job["files"]["config.json"]["attempts"] == 1
    assert job["status"] == "uploading"
