# B1 — Foundation

**Deps:** none. **Blocks:** B2, B3, B4. **Tier:** Codex 5.5 xhigh.

Everything downstream consumes these contracts. Getting the state schema or the HF
metadata shape wrong propagates into five other batches. Read `tasks/PLAN.md` fully
before writing a line.

## Deliverables

### `lib/hfmeta.py`

- `list_repo_files(repo_id, revision, token) -> list[FileMeta]` where
  `FileMeta = {path: str, size: int, sha256: str | None, lfs: bool}`.
- `resolve_revision(repo_id, revision_or_ref, token) -> str` — always return a commit
  SHA. Callers pass `main`; nothing downstream may ever use a mutable ref.
- `download_url(repo_id, revision, path) -> str`.

**Before writing this file**, confirm the current `huggingface_hub` API for fetching
per-file size and LFS SHA256:

```
npx ctx7@latest library huggingface_hub "list repo files with size and lfs sha256 metadata"
npx ctx7@latest docs <resolved-id> "get model info with files_metadata, sibling size and lfs sha256"
```

Do not rely on training-data recollection of attribute names — they have changed
across versions. If the LFS SHA256 is not exposed, say so explicitly in your report
rather than silently substituting a different hash; the entire L5 verification layer
depends on it.

Handle: gated repos (401/403 with a clear error), missing revision (404), and files
with no LFS metadata (`sha256 = None`, `lfs = False`).

### `lib/state.py`

Typed read/modify/write for `state/jobs/<safe_name>.json` per PLAN §2.

- `safe_name(repo_id) -> str`
- `load(safe_name) -> Job | None`, `save(job)` — atomic write (temp + `os.replace`)
- `create(repo_id, revision, priority) -> Job` (status `discovered`)
- `set_files(job, list[FileMeta])` — populates `files`, `total_bytes`, `total_files`,
  status → `planned`
- `mark_uploaded(job, path, sha256_observed)` — verify observed vs declared; on
  mismatch raise and increment `attempts`
- `mark_verified(job, path)`
- `record_failure(job, path, error)` — increment `attempts`; if `> MAX_ATTEMPTS` set
  job status `failed`
- `merge_shard_results(job, results: dict)` — for the collect job
- `recompute_status(job)` — derive `uploading`/`verifying`/`verified` from file flags
- `pending_uploads(job)`, `pending_verifications(job)`

Validate the status enum on write. Reject illegal transitions (e.g. `verified` →
`uploading` without an explicit `reset` call). Provide `reset_file(job, path)` for the
sweeper to legally reopen a file.

### `lib/notify.py`

- `notify(text, level="info")` — POST `{"text": ...}` to `NOTIFY_WEBHOOK`.
- No-op silently when unset. Never raise — a failed notification must never fail a job.
- Never include secret values in the payload.

### `watchlist.yaml`

Copy from SPEC §3 verbatim, with these changes:
- `poll_hours: 3`
- add a top-level `smoke_test_repo: hf-internal-testing/tiny-random-gpt2` used by B7.

### `tests/`

`pytest` covering: `safe_name` edge cases, atomic save, every state transition
including illegal ones, `MAX_ATTEMPTS` triggering `failed`, `recompute_status` across
partial-progress combinations, sha256-mismatch handling, and `notify` no-op when unset.
Mock all HF calls — no network.

## Acceptance

- `pytest` green.
- `python -c "import lib.hfmeta, lib.state, lib.notify"` clean.
- No secret ever reaches a log line or an exception message.
- Report explicitly whether HF exposes LFS SHA256 in the version you targeted, and the
  exact attribute path you used.
