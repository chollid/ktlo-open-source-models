# B2 — Watcher

**Deps:** B1. **Blocks:** none. **Tier:** Codex 5.5 high.

Polls HF for new/updated target repos, applies quality gates, creates a job state file,
and dispatches the grabber.

## Deliverables

### `watcher.py`

Base on SPEC §4, with these mandatory corrections:

1. **Dispatch auth.** Use `DISPATCH_PAT`, NOT `GITHUB_TOKEN`. See PLAN §6 — a dispatch
   authenticated with `GITHUB_TOKEN` returns 204 and silently never runs. Assert
   `DISPATCH_PAT` is present at startup and exit non-zero with a clear message if not.
2. **Pin the revision.** Resolve to a commit SHA via `lib.hfmeta.resolve_revision`
   before dispatching. Never dispatch `main`.
3. **Create job state.** Call `lib.state.create(...)` and commit `state/jobs/<n>.json`
   before dispatch, so the sweeper can recover even if the grab workflow never starts.
4. **Gate bug in SPEC.** SPEC's `passes_gates` matches `require_method` against
   `info.id + license`, which is nearly always wrong — `license` has no bearing on
   abliteration method. Match against `info.id` plus the model card **content/tags**.
   If card text is unavailable without an extra fetch, match on `info.id` and `tags`
   only, and say so in a comment.
5. Re-grab when `info.sha` differs from `state/seen.json` (repo updated in place).
6. `--dry-run` prints what would be dispatched and writes nothing.

### `.github/workflows/watch.yml`

- Triggers: `schedule` (cron `0 */3 * * *`) and `workflow_dispatch` only.
- `permissions: contents: write` (needs to push state).
- `concurrency: { group: state-write, cancel-in-progress: false }`.
- Steps: checkout → setup-python 3.12 → `pip install huggingface_hub pyyaml requests`
  → run `watcher.py` → commit and push `state/` (tolerate "nothing to commit").
- Push must use a token that can write to a public repo's default branch; if branch
  protection blocks it, document the required setting in the job summary.
- On failure, call the notify webhook.

## Acceptance

- `python watcher.py --dry-run` runs clean with fake creds and mocked HF, printing the
  matched repos and the exact dispatch payload.
- Tests: gate logic (base bypasses gates, abliterated respects `min_downloads` and
  `require_method`), sha-change re-grab, unchanged-sha no-op, missing `DISPATCH_PAT`
  hard-fails.
- Verify against current GitHub docs that `repository_dispatch` with a fine-grained PAT
  requires `contents: write` and confirm the payload size limit accommodates our
  `client_payload`.

---

## ADDENDUM (locked 2026-07-26) — file filtering is part of B2

Read PLAN.md §9 in full. Two additions to this batch:

### A. Implement file-level filtering (PLAN §9.2)

`watchlist.yaml` gains per-target `file_rules` (`always_keep` / `include` / `exclude`)
and `max_total_bytes`. The watcher applies them when enumerating files, BEFORE calling
`lib.state.set_files`, so filtered-out files never enter state and can never be grabbed.

Why this exists: GGUF uploaders ship every quant level in one repo. At K3 scale that is
10-15 TB in a single repo, and the streaming grabber has no moment where a human would
notice. Some base repos ship FP8 and BF16 together, silently doubling the transfer.

Requirements:
- `always_keep` bypasses include/exclude (config, tokenizer, small metadata files).
- Weight-like extensions (`.safetensors`, `.gguf`, `.bin`, `.pt`) are subject to
  include/exclude. Empty `include` means keep all.
- **Split-set integrity:** a filter that keeps only some parts of an `-NNNNN-of-MMMMM`
  set produces an unusable archive. Detect partial split sets and fail loudly.
  Test with realistic names, e.g. `Model-Q4_K_M-00007-of-00024.gguf` alongside
  `Model-Q5_K_M-00003-of-00030.gguf`.

### B. Size sanity gate (PLAN §9.3)

Before dispatch, compare post-filter `total_bytes` to the target's `max_total_bytes`.
If over: do NOT dispatch, set job `failed`, notify with the computed total and the ten
largest files, and require a human to raise the limit deliberately.

A wrong filter costs a five-figure R2 bill and a full drive, discovered days later.
A false alarm costs one manual re-run. Fail closed.

### C. watchlist.yaml content

Encode the locked decisions from PLAN §9.1:
- `moonshotai` / `Kimi-K3`, `priority: base` — exclude BF16 weights, keep FP8
- abliterated GGUF targets — `include: ["*Q4_K_M*"]`
- keep `min_downloads: 50` and `require_method`

Also surface the repo's declared dtype (from config.json or the model card) in the
notification, so the operator can confirm whether FP8 is native or BF16 is an upcast.
