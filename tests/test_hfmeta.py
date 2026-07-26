from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from lib import hfmeta


SHA = "a" * 40
LFS_SHA = "b" * 64


class FakeGatedRepoError(Exception):
    pass


class FakeHTTPError(Exception):
    response = None


class FakeRepositoryNotFoundError(Exception):
    pass


class FakeRevisionNotFoundError(Exception):
    pass


def install_fake_hub(monkeypatch, *, info=None, error=None):
    calls = []

    class FakeHfApi:
        def __init__(self, token):
            calls.append(("init", token))

        def model_info(self, **kwargs):
            calls.append(("model_info", kwargs))
            if error is not None:
                raise error
            return info

    hub = types.ModuleType("huggingface_hub")
    hub.HfApi = FakeHfApi
    errors = types.ModuleType("huggingface_hub.errors")
    errors.GatedRepoError = FakeGatedRepoError
    errors.HfHubHTTPError = FakeHTTPError
    errors.RepositoryNotFoundError = FakeRepositoryNotFoundError
    errors.RevisionNotFoundError = FakeRevisionNotFoundError
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "huggingface_hub.errors", errors)
    return calls


def test_list_repo_files_uses_files_metadata_and_lfs_sha256(monkeypatch):
    info = SimpleNamespace(
        siblings=[
            SimpleNamespace(
                rfilename="weights/model.safetensors",
                size=123,
                lfs=SimpleNamespace(sha256=LFS_SHA),
            ),
            SimpleNamespace(rfilename="config.json", size=17, lfs=None),
        ]
    )
    calls = install_fake_hub(monkeypatch, info=info)

    files = hfmeta.list_repo_files("org/model", SHA, "hf_private")

    assert files == [
        {"path": "config.json", "size": 17, "sha256": None, "lfs": False},
        {
            "path": "weights/model.safetensors",
            "size": 123,
            "sha256": LFS_SHA,
            "lfs": True,
        },
    ]
    assert calls == [
        ("init", "hf_private"),
        (
            "model_info",
            {
                "repo_id": "org/model",
                "revision": SHA,
                "files_metadata": True,
            },
        ),
    ]


def test_list_repo_files_rejects_mutable_revision_before_hf_call(monkeypatch):
    calls = install_fake_hub(monkeypatch, info=SimpleNamespace(siblings=[]))
    with pytest.raises(ValueError, match="commit SHA"):
        hfmeta.list_repo_files("org/model", "main", None)
    assert calls == []


def test_list_repo_files_requires_size_and_lfs_sha(monkeypatch):
    install_fake_hub(
        monkeypatch,
        info=SimpleNamespace(
            siblings=[SimpleNamespace(rfilename="bad.bin", size=None, lfs=None)]
        ),
    )
    with pytest.raises(hfmeta.HFMetadataError, match="no size metadata"):
        hfmeta.list_repo_files("org/model", SHA, None)

    install_fake_hub(
        monkeypatch,
        info=SimpleNamespace(
            siblings=[
                SimpleNamespace(
                    rfilename="bad-lfs.bin",
                    size=10,
                    lfs=SimpleNamespace(sha256=None),
                )
            ]
        ),
    )
    with pytest.raises(hfmeta.HFMetadataError, match="incomplete LFS metadata"):
        hfmeta.list_repo_files("org/model", SHA, None)


def test_resolve_revision_always_returns_commit_sha(monkeypatch):
    calls = install_fake_hub(monkeypatch, info=SimpleNamespace(sha=SHA.upper()))
    assert hfmeta.resolve_revision("org/model", "main", "token") == SHA
    assert calls[-1][1]["files_metadata"] is False

    install_fake_hub(monkeypatch, info=SimpleNamespace(sha="main"))
    with pytest.raises(hfmeta.HFMetadataError, match="no commit SHA"):
        hfmeta.resolve_revision("org/model", "main", None)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeGatedRepoError("response had hf_secret"), hfmeta.HFRepoAccessError),
        (
            FakeRevisionNotFoundError("response had hf_secret"),
            hfmeta.HFRevisionNotFoundError,
        ),
    ],
)
def test_hf_errors_are_clear_and_do_not_leak_upstream_text(
    monkeypatch, error, expected
):
    install_fake_hub(monkeypatch, error=error)
    with pytest.raises(expected) as captured:
        hfmeta.resolve_revision("org/model", "main", "hf_secret")
    assert "hf_secret" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_download_url_encodes_path_but_preserves_directories():
    assert hfmeta.download_url("org/model", SHA, "weights/a file#1.bin") == (
        f"https://huggingface.co/org/model/resolve/{SHA}/"
        "weights/a%20file%231.bin"
    )
