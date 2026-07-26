"""Hugging Face repository metadata helpers.

The dependency import is intentionally deferred so importing the shared library
does not perform authentication, network access, or other Hugging Face setup.
"""

from __future__ import annotations

import re
from typing import TypedDict
from urllib.parse import quote


_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class FileMeta(TypedDict):
    """The exact per-file metadata shape consumed by downstream batches."""

    path: str
    size: int
    sha256: str | None
    lfs: bool


class HFMetadataError(RuntimeError):
    """Base error for safe, secret-free Hugging Face metadata failures."""


class HFRepoAccessError(HFMetadataError):
    """The repository requires access that the supplied token does not have."""


class HFRevisionNotFoundError(HFMetadataError):
    """The requested repository revision does not exist."""


def _model_info(
    repo_id: str,
    revision: str,
    token: str | None,
    *,
    files_metadata: bool,
):
    """Call HfApi while replacing upstream errors with secret-free messages."""

    try:
        from huggingface_hub import HfApi
        from huggingface_hub.errors import (
            GatedRepoError,
            HfHubHTTPError,
            RepositoryNotFoundError,
            RevisionNotFoundError,
        )
    except ImportError:
        raise HFMetadataError(
            "huggingface_hub 1.24.0 is required; install requirements.txt"
        ) from None

    try:
        api = HfApi(token=token)
        return api.model_info(
            repo_id=repo_id,
            revision=revision,
            files_metadata=files_metadata,
        )
    except GatedRepoError:
        raise HFRepoAccessError(
            f"access denied for gated Hugging Face repository {repo_id}"
        ) from None
    except RevisionNotFoundError:
        raise HFRevisionNotFoundError(
            f"Hugging Face revision not found for {repo_id}: {revision}"
        ) from None
    except RepositoryNotFoundError:
        raise HFRepoAccessError(
            f"Hugging Face repository {repo_id} was not found or is not accessible"
        ) from None
    except HfHubHTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in (401, 403):
            raise HFRepoAccessError(
                f"access denied for Hugging Face repository {repo_id}"
            ) from None
        if status_code == 404:
            raise HFRevisionNotFoundError(
                f"Hugging Face repository or revision not found: {repo_id}"
            ) from None
        raise HFMetadataError(
            f"Hugging Face metadata request failed for {repo_id}"
        ) from None
    except Exception:
        # Upstream exceptions can contain request details. Do not chain or copy
        # their text because an Authorization value must never reach a repr/log.
        raise HFMetadataError(
            f"Hugging Face metadata request failed for {repo_id}"
        ) from None


def _require_commit_sha(revision: str) -> None:
    if not _COMMIT_SHA.fullmatch(revision):
        raise ValueError("revision must be a 40-character hexadecimal commit SHA")


def list_repo_files(
    repo_id: str, revision: str, token: str | None
) -> list[FileMeta]:
    """Return all model files at an immutable commit with size/LFS metadata."""

    _require_commit_sha(revision)
    info = _model_info(repo_id, revision, token, files_metadata=True)
    siblings = info.siblings
    if siblings is None:
        raise HFMetadataError(
            f"Hugging Face returned no file listing for repository {repo_id}"
        )

    files: list[FileMeta] = []
    for sibling in siblings:
        if sibling.size is None:
            raise HFMetadataError(
                f"Hugging Face returned no size metadata for {sibling.rfilename}"
            )

        if sibling.lfs is None:
            sha256 = None
            is_lfs = False
        else:
            # huggingface_hub 1.24.0:
            # ModelInfo.siblings[i].lfs is BlobLfsInfo and exposes `.sha256`.
            sha256 = sibling.lfs.sha256
            if not sha256:
                raise HFMetadataError(
                    f"Hugging Face returned incomplete LFS metadata for "
                    f"{sibling.rfilename}"
                )
            is_lfs = True

        files.append(
            {
                "path": sibling.rfilename,
                "size": sibling.size,
                "sha256": sha256,
                "lfs": is_lfs,
            }
        )

    return sorted(files, key=lambda item: item["path"])


def resolve_revision(
    repo_id: str, revision_or_ref: str, token: str | None
) -> str:
    """Resolve a branch, tag, or SHA to the immutable 40-character commit SHA."""

    info = _model_info(
        repo_id, revision_or_ref, token, files_metadata=False
    )
    revision = info.sha
    if not isinstance(revision, str) or not _COMMIT_SHA.fullmatch(revision):
        raise HFMetadataError(
            f"Hugging Face returned no commit SHA for repository {repo_id}"
        )
    return revision.lower()


def download_url(repo_id: str, revision: str, path: str) -> str:
    """Build the immutable streaming download URL for a single repository file."""

    _require_commit_sha(revision)
    encoded_repo = quote(repo_id, safe="/")
    encoded_revision = quote(revision, safe="")
    encoded_path = quote(path.lstrip("/"), safe="/")
    return (
        f"https://huggingface.co/{encoded_repo}/resolve/"
        f"{encoded_revision}/{encoded_path}"
    )
