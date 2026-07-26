from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import watcher
from lib import state


SHA_OLD = "a" * 40
SHA_NEW = "b" * 40


def metadata(path: str, size: int = 100):
    return {"path": path, "size": size, "sha256": None, "lfs": False}


def config(*, maximum: int = 10_000, priority: str = "base"):
    return {
        "targets": [
            {
                "author": "org",
                "include": ["Kimi-K3"],
                "priority": priority,
                "file_rules": {
                    "always_keep": [
                        "*.json",
                        "*.txt",
                        "*.md",
                        "*.model",
                        "tokenizer*",
                        "*.py",
                    ],
                    "include": ["*Q4_K_M*"] if priority == "abliterated" else [],
                    "exclude": (
                        [] if priority == "abliterated" else ["*bf16*", "*BF16*"]
                    ),
                },
                "max_total_bytes": maximum,
            }
        ],
        "quality_gates": {
            "min_downloads": 50,
            "require_method": ["heretic", "abliterat"],
        },
    }


class FakeApi:
    def __init__(self, infos):
        self.infos = infos
        self.calls = []

    def list_models(self, **kwargs):
        self.calls.append(kwargs)
        return self.infos


def info(
    *,
    repo_id: str = "org/Kimi-K3-Abliterated",
    sha: str = SHA_NEW,
    downloads: int = 100,
    tags: list[str] | None = None,
    dtype: str = "float8_e4m3fn",
):
    return SimpleNamespace(
        id=repo_id,
        sha=sha,
        downloads=downloads,
        tags=tags or ["abliterated"],
        config={"torch_dtype": dtype},
        card_data=None,
    )


def install_hf_mocks(monkeypatch, files):
    resolve_calls = []
    list_calls = []

    def resolve(repo_id, revision, token):
        resolve_calls.append((repo_id, revision, token))
        return revision.lower()

    def list_files(repo_id, revision, token):
        list_calls.append((repo_id, revision, token))
        return files

    monkeypatch.setattr(watcher.hfmeta, "resolve_revision", resolve)
    monkeypatch.setattr(watcher.hfmeta, "list_repo_files", list_files)
    return resolve_calls, list_calls


def test_base_bypasses_quality_gates():
    candidate = info(downloads=0, tags=["plain-model"])
    gates = {"min_downloads": 50, "require_method": ["abliterat"]}
    assert watcher.passes_gates(candidate, "base", gates) is True


@pytest.mark.parametrize(
    ("downloads", "tags", "expected"),
    [
        (49, ["abliterated"], False),
        (50, ["plain-model"], False),
        (50, ["method:heretic"], True),
        (80, ["abliterated"], True),
    ],
)
def test_abliterated_respects_download_and_method_gates(
    downloads, tags, expected
):
    candidate = info(
        repo_id="org/Kimi-K3-Plain",
        downloads=downloads,
        tags=tags,
    )
    gates = {
        "min_downloads": 50,
        "require_method": ["heretic", "abliterat"],
    }
    assert (
        watcher.passes_gates(candidate, "abliterated", gates) is expected
    )


def test_sha_change_prepares_regrab_but_unchanged_sha_is_noop(monkeypatch):
    files = [metadata("model.safetensors")]
    resolve_calls, list_calls = install_hf_mocks(monkeypatch, files)

    changed, next_seen = watcher.prepare_jobs(
        config(), FakeApi([info()]), {"org/Kimi-K3-Abliterated": SHA_OLD}, "hf"
    )
    assert len(changed) == 1
    assert changed[0].job["revision"] == SHA_NEW
    assert next_seen["org/Kimi-K3-Abliterated"] == SHA_NEW
    assert resolve_calls == [
        ("org/Kimi-K3-Abliterated", SHA_NEW, "hf")
    ]
    assert len(list_calls) == 1

    resolve_calls.clear()
    list_calls.clear()
    unchanged, next_seen = watcher.prepare_jobs(
        config(), FakeApi([info()]), {"org/Kimi-K3-Abliterated": SHA_NEW}, "hf"
    )
    assert unchanged == []
    assert next_seen == {"org/Kimi-K3-Abliterated": SHA_NEW}
    assert resolve_calls == []
    assert list_calls == []


def test_file_filter_keeps_exact_quant_and_always_keep():
    q4 = [
        metadata(f"Model-Q4_K_M-{part:05d}-of-00024.gguf")
        for part in range(1, 25)
    ]
    q5 = [
        metadata(f"Model-Q5_K_M-{part:05d}-of-00030.gguf")
        for part in range(1, 31)
    ]
    files = [
        *q4,
        *q5,
        metadata("Model-Q8_0.gguf"),
        metadata("config.json", 5),
        metadata("tokenizer.bin", 6),
        metadata("assets/notes.pdf", 7),
    ]
    rules = {
        "always_keep": ["*.json", "tokenizer*"],
        "include": ["*Q4_K_M*"],
        "exclude": ["*00013-of-99999*"],
    }

    kept = watcher.filter_repo_files(files, rules)
    kept_paths = {item["path"] for item in kept}

    assert kept_paths == {
        *(item["path"] for item in q4),
        "config.json",
        "tokenizer.bin",
        "assets/notes.pdf",
    }
    assert "Model-Q5_K_M-00003-of-00030.gguf" not in kept_paths
    assert "Model-Q8_0.gguf" not in kept_paths


def test_always_keep_bypasses_include_and_exclude():
    rules = {
        "always_keep": ["tokenizer*"],
        "include": ["*Q4_K_M*"],
        "exclude": ["*tokenizer*"],
    }
    kept = watcher.filter_repo_files([metadata("tokenizer.bin")], rules)
    assert [item["path"] for item in kept] == ["tokenizer.bin"]


def test_partial_split_set_is_rejected():
    files = [
        metadata("Model-Q4_K_M-00007-of-00024.gguf"),
        metadata("Model-Q4_K_M-00008-of-00024.gguf"),
        metadata("Model-Q5_K_M-00003-of-00030.gguf"),
        metadata("Model-Q5_K_M-00004-of-00030.gguf"),
    ]
    rules = {
        "always_keep": [],
        "include": ["*Q4_K_M-00007*", "*Q5_K_M*"],
        "exclude": [],
    }
    with pytest.raises(
        watcher.SplitSetIntegrityError,
        match=r"Q4_K_M-NNNNN-of-00024\.gguf: kept 1 of 2",
    ):
        watcher.filter_repo_files(files, rules)


def test_size_gate_saves_failed_job_notifies_top_ten_and_never_dispatches(
    tmp_path, monkeypatch
):
    files = [metadata(f"weight-{number:02d}.gguf", number) for number in range(1, 13)]
    install_hf_mocks(monkeypatch, files)
    monkeypatch.setattr(state, "JOBS_DIR", tmp_path / "state" / "jobs")
    monkeypatch.setattr(watcher, "SEEN_PATH", tmp_path / "state" / "seen.json")
    events = []
    notifications = []

    def commit(paths):
        events.append(("commit", list(paths)))
        return True

    monkeypatch.setattr(watcher, "_git_commit_state", commit)
    monkeypatch.setattr(
        watcher,
        "_post_dispatch",
        lambda *args: events.append(("dispatch", args)),
    )
    monkeypatch.setattr(
        watcher,
        "notify",
        lambda text, level="info": notifications.append((text, level)),
    )

    prepared = watcher.execute(
        config(maximum=20),
        FakeApi([info()]),
        {},
        hf_token="hf",
        dispatch_token="github-token",
        github_repository="owner/repo",
        dry_run=False,
    )

    assert len(prepared) == 1
    saved = state.load("org__Kimi-K3-Abliterated")
    assert saved is not None
    assert saved["status"] == "failed"
    assert saved["total_bytes"] == sum(range(1, 13))
    assert [event[0] for event in events] == ["commit"]
    assert json.loads(watcher.SEEN_PATH.read_text()) == {}
    message = notifications[0][0]
    assert "post-filter total 78 bytes" in message
    assert "declared dtype: float8_e4m3fn" in message
    assert "weight-12.gguf" in message
    assert "weight-03.gguf" in message
    assert "weight-02.gguf" not in message


def test_absent_dispatch_pat_uses_github_token_after_state_commit(
    tmp_path, monkeypatch
):
    files = [metadata("model.safetensors", 100)]
    install_hf_mocks(monkeypatch, files)
    monkeypatch.setattr(state, "JOBS_DIR", tmp_path / "state" / "jobs")
    monkeypatch.setattr(watcher, "SEEN_PATH", tmp_path / "state" / "seen.json")
    monkeypatch.delenv("DISPATCH_PAT", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    events = []

    def commit(paths):
        assert state.load("org__Kimi-K3-Abliterated") is not None
        assert json.loads(watcher.SEEN_PATH.read_text()) == {
            "org/Kimi-K3-Abliterated": SHA_NEW
        }
        events.append("commit")
        return True

    def dispatch(repository, token, payload):
        events.append(("dispatch", token))
        assert payload["revision"] == SHA_NEW

    monkeypatch.setattr(watcher, "_git_commit_state", commit)
    monkeypatch.setattr(watcher, "_post_dispatch", dispatch)
    monkeypatch.setattr(watcher, "notify", lambda *args, **kwargs: None)

    watcher.execute(
        config(),
        FakeApi([info()]),
        {},
        hf_token="hf",
        dispatch_token=watcher._select_dispatch_token(),
        github_repository="owner/repo",
        dry_run=False,
    )

    assert events == ["commit", ("dispatch", "github-token")]


def test_present_dispatch_pat_is_preferred_over_github_token(
    tmp_path, monkeypatch
):
    install_hf_mocks(monkeypatch, [metadata("model.safetensors", 100)])
    monkeypatch.setattr(state, "JOBS_DIR", tmp_path / "state" / "jobs")
    monkeypatch.setattr(watcher, "SEEN_PATH", tmp_path / "state" / "seen.json")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("DISPATCH_PAT", "dispatch-pat")
    dispatched_with = []

    monkeypatch.setattr(watcher, "_git_commit_state", lambda paths: True)
    monkeypatch.setattr(
        watcher,
        "_post_dispatch",
        lambda repository, token, payload: dispatched_with.append(token),
    )
    monkeypatch.setattr(watcher, "notify", lambda *args, **kwargs: None)

    watcher.execute(
        config(),
        FakeApi([info()]),
        {},
        hf_token="hf",
        dispatch_token=watcher._select_dispatch_token(),
        github_repository="owner/repo",
        dry_run=False,
    )

    assert dispatched_with == ["dispatch-pat"]


def test_bf16_only_repo_fails_lists_dropped_weights_and_never_dispatches(
    tmp_path, monkeypatch
):
    files = [
        metadata("weights/BF16/model-00001-of-00002.safetensors", 200),
        metadata("weights/BF16/model-00002-of-00002.safetensors", 200),
        metadata("config.json", 5),
    ]
    install_hf_mocks(monkeypatch, files)
    monkeypatch.setattr(state, "JOBS_DIR", tmp_path / "state" / "jobs")
    monkeypatch.setattr(watcher, "SEEN_PATH", tmp_path / "state" / "seen.json")
    events = []
    notifications = []

    monkeypatch.setattr(
        watcher,
        "_git_commit_state",
        lambda paths: events.append("commit") or True,
    )
    monkeypatch.setattr(
        watcher,
        "_post_dispatch",
        lambda *args: events.append("dispatch"),
    )
    monkeypatch.setattr(
        watcher,
        "notify",
        lambda text, level="info": notifications.append((text, level)),
    )

    prepared = watcher.execute(
        config(),
        FakeApi([info(dtype="bfloat16")]),
        {},
        hf_token="hf",
        dispatch_token="github-token",
        github_repository="owner/repo",
        dry_run=False,
    )

    saved = state.load("org__Kimi-K3-Abliterated")
    assert saved is not None
    assert saved["status"] == "failed"
    assert set(saved["files"]) == {"config.json"}
    assert prepared[0].block_reason == "empty_weights"
    assert events == ["commit"]
    assert json.loads(watcher.SEEN_PATH.read_text()) == {}
    message = notifications[0][0]
    assert "no FP8 weights after filtering - appears BF16-only" in message
    assert "weights/BF16/model-00001-of-00002.safetensors" in message
    assert "weights/BF16/model-00002-of-00002.safetensors" in message
    assert "Declared dtype: bfloat16" in message


def test_mixed_fp8_and_bf16_drops_bf16_and_dispatches_fp8(
    tmp_path, monkeypatch
):
    files = [
        metadata("weights/FP8/model.safetensors", 200),
        metadata("weights/BF16/model.safetensors", 400),
        metadata("config.json", 5),
    ]
    install_hf_mocks(monkeypatch, files)
    monkeypatch.setattr(state, "JOBS_DIR", tmp_path / "state" / "jobs")
    monkeypatch.setattr(watcher, "SEEN_PATH", tmp_path / "state" / "seen.json")
    dispatched = []

    monkeypatch.setattr(watcher, "_git_commit_state", lambda paths: True)
    monkeypatch.setattr(
        watcher,
        "_post_dispatch",
        lambda repository, token, payload: dispatched.append(payload),
    )
    monkeypatch.setattr(watcher, "notify", lambda *args, **kwargs: None)

    prepared = watcher.execute(
        config(),
        FakeApi([info()]),
        {},
        hf_token="hf",
        dispatch_token="github-token",
        github_repository="owner/repo",
        dry_run=False,
    )

    saved = state.load("org__Kimi-K3-Abliterated")
    assert saved is not None
    assert saved["status"] == "planned"
    assert set(saved["files"]) == {
        "weights/FP8/model.safetensors",
        "config.json",
    }
    assert prepared[0].block_reason is None
    assert len(dispatched) == 1
    assert dispatched[0]["total_bytes"] == 205
    assert json.loads(watcher.SEEN_PATH.read_text()) == {
        "org/Kimi-K3-Abliterated": SHA_NEW
    }


def test_dry_run_prints_exact_payload_and_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    files = [metadata("model.safetensors", 100)]
    install_hf_mocks(monkeypatch, files)
    monkeypatch.setattr(state, "JOBS_DIR", tmp_path / "state" / "jobs")
    monkeypatch.setattr(watcher, "SEEN_PATH", tmp_path / "state" / "seen.json")
    monkeypatch.setattr(
        watcher,
        "_git_commit_state",
        lambda paths: pytest.fail("dry-run must not commit"),
    )
    monkeypatch.setattr(
        watcher,
        "_post_dispatch",
        lambda *args: pytest.fail("dry-run must not dispatch"),
    )
    monkeypatch.setattr(
        watcher,
        "notify",
        lambda *args, **kwargs: pytest.fail("dry-run must not notify"),
    )

    watcher.execute(
        config(),
        FakeApi([info()]),
        {},
        hf_token="hf",
        dispatch_token="github-token",
        github_repository="",
        dry_run=True,
    )

    output = capsys.readouterr().out
    assert '"event_type": "grab-model"' in output
    assert f'"revision": "{SHA_NEW}"' in output
    assert not (tmp_path / "state").exists()
