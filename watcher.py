"""Poll Hugging Face for archive targets and dispatch durable grab jobs."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from lib import hfmeta, state
from lib.notify import notify


WATCHLIST_PATH = Path("watchlist.yaml")
SEEN_PATH = Path("state/seen.json")
WEIGHT_EXTENSIONS = frozenset({".safetensors", ".gguf", ".bin", ".pt"})
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SPLIT_FILE = re.compile(
    r"^(?P<prefix>.+)-(?P<part>\d+)-of-(?P<total>\d+)"
    r"(?P<suffix>(?:\.[^/]+)*)$"
)
_DTYPE_KEYS = frozenset({"torch_dtype", "dtype", "model_dtype"})
_DTYPE_TAG = re.compile(
    r"(?i)(?:^|[-_:])"
    r"(?:fp8|float8(?:_e[45]m[23](?:fnuz)?)?|bf16|bfloat16|"
    r"fp16|float16|f16|fp32|float32|f32)"
    r"(?:$|[-_:])"
)


class WatcherError(RuntimeError):
    """A watcher operation failed without exposing credentials."""


class SplitSetIntegrityError(WatcherError):
    """File rules selected only part of a sharded weight set."""


@dataclass(frozen=True)
class PreparedJob:
    """One fully enumerated job awaiting durable persistence."""

    job: state.Job
    declared_dtype: str
    payload: dict[str, object] | None
    max_total_bytes: int
    block_reason: Literal["empty_weights", "size"] | None
    dropped_weights: tuple[str, ...]


def _string_list(value: object, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise WatcherError(f"{field} must be a list of non-empty strings")
    if not allow_empty and not value:
        raise WatcherError(f"{field} must not be empty")
    return value


def validate_config(config: object) -> dict[str, Any]:
    """Validate the safety-critical watchlist fields and return the mapping."""

    if not isinstance(config, dict):
        raise WatcherError("watchlist.yaml must contain a mapping")
    targets = config.get("targets")
    gates = config.get("quality_gates")
    if not isinstance(targets, list) or not targets:
        raise WatcherError("watchlist.yaml targets must be a non-empty list")
    if not isinstance(gates, dict):
        raise WatcherError("watchlist.yaml quality_gates must be a mapping")

    min_downloads = gates.get("min_downloads")
    if (
        not isinstance(min_downloads, int)
        or isinstance(min_downloads, bool)
        or min_downloads < 0
    ):
        raise WatcherError("quality_gates.min_downloads must be a non-negative integer")
    _string_list(
        gates.get("require_method"),
        "quality_gates.require_method",
        allow_empty=False,
    )

    for index, target in enumerate(targets):
        prefix = f"targets[{index}]"
        if not isinstance(target, dict):
            raise WatcherError(f"{prefix} must be a mapping")
        if not isinstance(target.get("author"), str) or not target["author"]:
            raise WatcherError(f"{prefix}.author must be a non-empty string")
        _string_list(target.get("include"), f"{prefix}.include", allow_empty=False)
        if target.get("priority") not in {"base", "abliterated"}:
            raise WatcherError(f"{prefix}.priority must be base or abliterated")

        rules = target.get("file_rules")
        if not isinstance(rules, dict):
            raise WatcherError(f"{prefix}.file_rules must be a mapping")
        for rule_name in ("always_keep", "include", "exclude"):
            _string_list(rules.get(rule_name), f"{prefix}.file_rules.{rule_name}")

        maximum = target.get("max_total_bytes")
        if (
            not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or maximum <= 0
        ):
            raise WatcherError(
                f"{prefix}.max_total_bytes must be a positive integer"
            )
    return config


def _load_config(path: Path = WATCHLIST_PATH) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        raise WatcherError("PyYAML is required; install requirements.txt") from None

    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise WatcherError(f"cannot read a valid watchlist from {path}") from None
    return validate_config(config)


def _read_seen(path: Path | None = None) -> dict[str, str]:
    path = SEEN_PATH if path is None else path
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise WatcherError(f"cannot read valid watcher state from {path}") from None
    if not isinstance(raw, dict) or any(
        not isinstance(repo_id, str)
        or not repo_id
        or not isinstance(revision, str)
        or not _COMMIT_SHA.fullmatch(revision)
        for repo_id, revision in raw.items()
    ):
        raise WatcherError("state/seen.json must map repo IDs to commit SHAs")
    return {repo_id: revision.lower() for repo_id, revision in raw.items()}


def _write_seen(seen: Mapping[str, str], path: Path | None = None) -> None:
    """Atomically persist the watcher-only seen map."""

    path = SEEN_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temp_name = output.name
            json.dump(dict(sorted(seen.items())), output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink()
            except FileNotFoundError:
                pass


def passes_gates(info: object, priority: str, gates: Mapping[str, object]) -> bool:
    """Return whether a matching repository passes its priority quality gates."""

    if priority == "base":
        return True

    downloads = getattr(info, "downloads", 0) or 0
    if downloads < gates["min_downloads"]:
        return False

    repo_id = str(getattr(info, "id", ""))
    tags = getattr(info, "tags", None) or []
    # list_models exposes the repository ID and tags in the same response. It does
    # not expose the model-card body; card_data is only YAML metadata (including
    # license, which says nothing about the abliteration method). Avoiding another
    # mutable-ref fetch also keeps this gate tied to the listing we are evaluating.
    method_text = " ".join([repo_id, *(str(tag) for tag in tags)]).lower()
    return any(
        str(method).lower() in method_text
        for method in gates["require_method"]  # type: ignore[union-attr]
    )


def _matches_glob(path: str, pattern: str) -> bool:
    """Match either the full repository path or its basename."""

    return fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(
        PurePosixPath(path).name, pattern
    )


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(_matches_glob(path, pattern) for pattern in patterns)


def _split_set_key(path: str) -> tuple[str, str, str] | None:
    match = _SPLIT_FILE.fullmatch(path)
    if match is None:
        return None
    return (
        match.group("prefix"),
        match.group("total"),
        match.group("suffix"),
    )


def filter_repo_files(
    files: Sequence[hfmeta.FileMeta], file_rules: Mapping[str, object]
) -> list[hfmeta.FileMeta]:
    """Apply target file rules and reject partial split weight sets."""

    always_keep = _string_list(
        file_rules.get("always_keep"), "file_rules.always_keep"
    )
    includes = _string_list(file_rules.get("include"), "file_rules.include")
    excludes = _string_list(file_rules.get("exclude"), "file_rules.exclude")

    kept: list[hfmeta.FileMeta] = []
    kept_paths: set[str] = set()
    split_sets: dict[tuple[str, str, str], list[str]] = {}

    for metadata in files:
        path = metadata["path"]
        split_key = _split_set_key(path)
        if split_key is not None:
            split_sets.setdefault(split_key, []).append(path)

        if _matches_any(path, always_keep):
            should_keep = True
        elif PurePosixPath(path).suffix.lower() in WEIGHT_EXTENSIONS:
            included = not includes or _matches_any(path, includes)
            should_keep = included and not _matches_any(path, excludes)
        else:
            should_keep = True

        if should_keep:
            kept.append(metadata)
            kept_paths.add(path)

    partial_sets: list[str] = []
    for (prefix, total, suffix), members in split_sets.items():
        kept_members = [path for path in members if path in kept_paths]
        if kept_members and len(kept_members) != len(members):
            partial_sets.append(
                f"{prefix}-NNNNN-of-{total}{suffix}: "
                f"kept {len(kept_members)} of {len(members)} listed parts"
            )
    if partial_sets:
        details = "; ".join(partial_sets[:5])
        raise SplitSetIntegrityError(
            f"file rules would create partial split set(s): {details}"
        )

    return kept


def _is_weight_file(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in WEIGHT_EXTENSIONS


def _dtype_values(value: object) -> Iterable[str]:
    if not isinstance(value, Mapping):
        return
    for key, nested in value.items():
        if str(key).lower() in _DTYPE_KEYS:
            if isinstance(nested, (str, int, float)):
                yield str(nested)
            elif isinstance(nested, list):
                yield from (str(item) for item in nested)
        if isinstance(nested, Mapping):
            yield from _dtype_values(nested)
        elif isinstance(nested, list):
            for item in nested:
                if isinstance(item, Mapping):
                    yield from _dtype_values(item)


def declared_dtype(info: object) -> str:
    """Read declared dtype values from fetched config, card metadata, or tags."""

    values: list[str] = []
    config = getattr(info, "config", None)
    values.extend(_dtype_values(config))

    card_data = getattr(info, "card_data", None)
    if card_data is not None:
        try:
            card_mapping = card_data.to_dict()
        except (AttributeError, TypeError, ValueError):
            card_mapping = None
        values.extend(_dtype_values(card_mapping))

    if not values:
        for tag in getattr(info, "tags", None) or []:
            tag_text = str(tag)
            if _DTYPE_TAG.search(tag_text):
                values.append(tag_text)

    unique = list(dict.fromkeys(value for value in values if value))
    return ", ".join(unique) if unique else "unknown"


def _normalized_listed_sha(info: object) -> str | None:
    revision = getattr(info, "sha", None)
    if isinstance(revision, str) and _COMMIT_SHA.fullmatch(revision):
        return revision.lower()
    return None


def _repo_matches(repo_id: str, includes: Sequence[str]) -> bool:
    lowered = repo_id.lower()
    return any(fragment.lower() in lowered for fragment in includes)


def prepare_jobs(
    config: Mapping[str, Any],
    api: object,
    seen: Mapping[str, str],
    hf_token: str | None,
) -> tuple[list[PreparedJob], dict[str, str]]:
    """Enumerate and fully plan changed repositories without writing state."""

    prepared: list[PreparedJob] = []
    next_seen = dict(seen)
    matched_this_poll: set[str] = set()
    gates = config["quality_gates"]

    for target in config["targets"]:
        author = target["author"]
        try:
            infos = api.list_models(  # type: ignore[attr-defined]
                author=author,
                full=True,
                cardData=True,
                fetch_config=True,
                limit=200,
            )
            infos = list(infos)
        except Exception:
            raise WatcherError(
                f"Hugging Face model listing failed for author {author}"
            ) from None

        for info in infos:
            repo_id = getattr(info, "id", None)
            if not isinstance(repo_id, str) or not _repo_matches(
                repo_id, target["include"]
            ):
                continue
            if repo_id in matched_this_poll:
                continue
            matched_this_poll.add(repo_id)

            listed_sha = _normalized_listed_sha(info)
            if listed_sha is not None and seen.get(repo_id) == listed_sha:
                continue
            if not passes_gates(info, target["priority"], gates):
                continue

            revision = hfmeta.resolve_revision(
                repo_id, listed_sha or "main", hf_token
            )
            if seen.get(repo_id) == revision:
                continue

            all_files = hfmeta.list_repo_files(repo_id, revision, hf_token)
            filtered_files = filter_repo_files(all_files, target["file_rules"])
            job = state.create(repo_id, revision, target["priority"])
            # Excluded files never reach the binding state API.
            state.set_files(job, filtered_files)
            dtype = declared_dtype(info)
            maximum = target["max_total_bytes"]
            filtered_weight_paths = {
                metadata["path"]
                for metadata in filtered_files
                if _is_weight_file(metadata["path"])
            }
            dropped_weights = tuple(
                metadata["path"]
                for metadata in all_files
                if _is_weight_file(metadata["path"])
                and metadata["path"] not in filtered_weight_paths
            )

            # Re-examination is keyed on a changed revision SHA, not elapsed time.
            # Mid-upload file additions create a new SHA for the next poll; retrying
            # an unchanged blocked revision would only repeat identical work.
            if not filtered_weight_paths:
                # A metadata-only archive is not a model. Persist the failed job,
                # but create no payload.
                job["status"] = "failed"
                payload = None
                next_seen[repo_id] = revision
                block_reason: Literal["empty_weights", "size"] | None = (
                    "empty_weights"
                )
            elif job["total_bytes"] > maximum:
                # planned -> failed is a legal forward transition checked by save().
                job["status"] = "failed"
                payload = None
                next_seen[repo_id] = revision
                block_reason = "size"
            else:
                payload = {
                    "repo_id": repo_id,
                    "revision": revision,
                    "priority": target["priority"],
                    "downloads": getattr(info, "downloads", 0) or 0,
                    "total_bytes": job["total_bytes"],
                    "total_files": job["total_files"],
                    "declared_dtype": dtype,
                }
                next_seen[repo_id] = revision
                block_reason = None

            prepared.append(
                PreparedJob(
                    job=job,
                    declared_dtype=dtype,
                    payload=payload,
                    max_total_bytes=maximum,
                    block_reason=block_reason,
                    dropped_weights=dropped_weights,
                )
            )

    return prepared, next_seen


def _largest_files(job: state.Job, limit: int = 10) -> list[tuple[str, int]]:
    files = (
        (path, file_state["size"])
        for path, file_state in job["files"].items()
    )
    return sorted(files, key=lambda item: (-item[1], item[0]))[:limit]


def _size_gate_message(item: PreparedJob) -> str:
    largest = "\n".join(
        f"- {path}: {size} bytes" for path, size in _largest_files(item.job)
    )
    return (
        f"Size gate blocked {item.job['repo_id']} at {item.job['revision']}: "
        f"post-filter total {item.job['total_bytes']} bytes exceeds "
        f"max_total_bytes {item.max_total_bytes}; declared dtype: "
        f"{item.declared_dtype}. Raise the target limit deliberately to retry. "
        f"Ten largest filtered files:\n{largest}"
    )


def _empty_weight_message(item: PreparedJob) -> str:
    weight_files = ", ".join(item.dropped_weights) or "(none)"
    return (
        f"{item.job['repo_id']}: no FP8 weights after filtering - appears "
        f"BF16-only. Human decision required. Weight files seen: {weight_files}. "
        f"Declared dtype: {item.declared_dtype}"
    )


def _blocked_message(item: PreparedJob) -> str:
    if item.block_reason == "empty_weights":
        return _empty_weight_message(item)
    if item.block_reason == "size":
        return _size_gate_message(item)
    raise WatcherError(
        f"blocked job for {item.job['repo_id']} has no failure reason"
    )


def _git_commit_state(paths: Sequence[Path]) -> bool:
    """Commit and push exact state paths; raise before dispatch on any failure."""

    path_args = [str(path) for path in dict.fromkeys(paths)]
    subprocess.run(["git", "add", "--", *path_args], check=True)
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], check=False
    )
    if diff.returncode == 0:
        return False
    if diff.returncode != 1:
        raise WatcherError("git could not inspect staged watcher state")

    subprocess.run(
        ["git", "config", "user.name", "model-archive-bot"], check=True
    )
    subprocess.run(
        [
            "git",
            "config",
            "user.email",
            "model-archive-bot@users.noreply.github.com",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "state: record watcher discoveries"], check=True
    )
    subprocess.run(["git", "pull", "--rebase"], check=True)
    subprocess.run(["git", "push"], check=True)
    return True


def _validate_dispatch_payload(payload: Mapping[str, object]) -> None:
    if len(payload) > 10:
        raise WatcherError("repository dispatch payload exceeds 10 top-level fields")
    body = {"event_type": "grab-model", "client_payload": dict(payload)}
    if len(json.dumps(body, separators=(",", ":")).encode("utf-8")) >= 64 * 1024:
        raise WatcherError("repository dispatch payload must be smaller than 64KB")


def _post_dispatch(
    github_repository: str, dispatch_token: str, payload: Mapping[str, object]
) -> None:
    _validate_dispatch_payload(payload)
    if (
        not github_repository
        or github_repository.count("/") != 1
        or any(not part for part in github_repository.split("/"))
    ):
        raise WatcherError("GITHUB_REPOSITORY must be in owner/repository form")

    try:
        import requests
    except ImportError:
        raise WatcherError("requests is required; install requirements.txt") from None

    try:
        response = requests.post(
            f"https://api.github.com/repos/{github_repository}/dispatches",
            headers={
                "Authorization": f"Bearer {dispatch_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"event_type": "grab-model", "client_payload": dict(payload)},
            timeout=30,
        )
    except Exception:
        raise WatcherError("GitHub repository dispatch request failed") from None
    if response.status_code != 204:
        raise WatcherError(
            f"GitHub repository dispatch failed with HTTP {response.status_code}"
        )


def execute(
    config: Mapping[str, Any],
    api: object,
    seen: Mapping[str, str],
    *,
    hf_token: str | None,
    dispatch_token: str,
    github_repository: str,
    dry_run: bool,
) -> list[PreparedJob]:
    """Prepare, durably persist, and then dispatch all eligible jobs."""

    prepared, next_seen = prepare_jobs(config, api, seen, hf_token)
    dispatchable = [item for item in prepared if item.payload is not None]
    blocked = [item for item in prepared if item.payload is None]

    if dry_run:
        for item in blocked:
            print(f"DRY RUN: {_blocked_message(item)}")
        for item in dispatchable:
            body = {"event_type": "grab-model", "client_payload": item.payload}
            print(f"DRY RUN: would dispatch {json.dumps(body, sort_keys=True)}")
        if not prepared:
            print("no new matches")
        return prepared

    changed_paths = [SEEN_PATH]
    for item in prepared:
        state.save(item.job)
        changed_paths.append(
            state.JOBS_DIR / f"{item.job['safe_name']}.json"
        )
    _write_seen(next_seen)

    # This push is intentionally inside the watcher. A successful HTTP dispatch
    # is not allowed until the recoverable job plan exists in repository history.
    try:
        _git_commit_state(changed_paths)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WatcherError(
            f"could not commit and push watcher state before dispatch "
            f"({type(exc).__name__})"
        ) from None

    for item in blocked:
        notify(_blocked_message(item), level="error")

    for item in dispatchable:
        assert item.payload is not None
        try:
            _post_dispatch(github_repository, dispatch_token, item.payload)
        except WatcherError:
            notify(
                f"Dispatch failed for {item.job['repo_id']} at "
                f"{item.job['revision']}; the planned job is committed for "
                f"sweeper recovery. Declared dtype: {item.declared_dtype}",
                level="error",
            )
            raise
        notify(
            f"Dispatched model archive job for {item.job['repo_id']} at "
            f"{item.job['revision']}: {item.job['total_bytes']} bytes across "
            f"{item.job['total_files']} files; declared dtype: "
            f"{item.declared_dtype}"
        )

    if not prepared:
        print("no new matches")
    return prepared


def _make_api(token: str | None) -> object:
    try:
        from huggingface_hub import HfApi
    except ImportError:
        raise WatcherError(
            "huggingface_hub 1.24.0 is required; install requirements.txt"
        ) from None
    return HfApi(token=token)


def _select_dispatch_token() -> str:
    """Prefer an explicit PAT override, otherwise use the per-run GitHub token."""

    dispatch_token = os.environ.get("DISPATCH_PAT") or os.environ.get(
        "GITHUB_TOKEN"
    )
    if not dispatch_token:
        raise SystemExit(
            "GITHUB_TOKEN is required for repository dispatch when the optional "
            "DISPATCH_PAT override is unset"
        )
    return dispatch_token


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Poll Hugging Face and dispatch durable model archive jobs."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print exact dispatches without state, git, webhook, or GitHub writes",
    )
    args = parser.parse_args(argv)

    dispatch_token = _select_dispatch_token()
    hf_token = os.environ.get("HF_TOKEN")

    try:
        config = _load_config()
        seen = _read_seen()
        api = _make_api(hf_token)
        execute(
            config,
            api,
            seen,
            hf_token=hf_token,
            dispatch_token=dispatch_token,
            github_repository=os.environ.get("GITHUB_REPOSITORY", ""),
            dry_run=args.dry_run,
        )
    except WatcherError as exc:
        if not args.dry_run:
            notify(f"Watcher failed: {exc}", level="error")
        raise SystemExit(str(exc)) from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
