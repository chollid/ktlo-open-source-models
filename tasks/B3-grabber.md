# B3 — Streaming grabber

**Deps:** B1. **Blocks:** B4. **Tier:** Codex 5.5 xhigh.

The core of the pipeline. Streams every file HF → R2 with no disk, sharded across
parallel matrix jobs, hashing in-stream. Read PLAN §4 carefully — the memory budget and
the `tee`/`rcat` pipeline are exact requirements, not suggestions.

## Deliverables

### `plan_shards.py`

- Input: `--safe-name`, reads job state.
- Enumerate files still needing upload (`uploaded == false`).
- Greedy largest-first bin packing, target shard 300 GB,
  `N = clamp(ceil(total/300GB), 1, 20)`.
- Write `shards/shard-<i>.json` (list of file paths) and emit the matrix to
  `$GITHUB_OUTPUT` as `matrix=<json>`.
- Set job status → `planned` (or `uploading` when the grab starts).
- Handle the empty case: zero pending files ⇒ emit an empty matrix and a
  `skip=true` output so the workflow short-circuits instead of failing.

### `grab_shard.sh`

`bash`, `set -euo pipefail`. Args: shard file, repo id, revision, safe name.

- Write `~/.config/rclone/rclone.conf` from env (PLAN §6). Never log it.
- For each file in the shard, in parallel with `xargs -P 8`, run the exact
  curl → `tee >(sha256sum)` → `rclone rcat` pipeline from PLAN §4.
- **`set -o pipefail` alone does not catch a failing `rcat` inside process
  substitution.** Verify each file's success explicitly: non-zero from any stage, or a
  sha256 mismatch vs the HF-declared hash, marks that file failed. Do not trust the
  pipeline's exit status.
- Emit `shard-<i>-result.json`: `{path: {uploaded: bool, sha256_observed: str|null,
  error: str|null}}`. Upload as a workflow artifact.
- A failed file must not abort the shard — record and continue.
- No `set -x`. `HF_TOKEN` passed via env, never on an argv line.

### `.github/workflows/grab.yml`

- Triggers: `repository_dispatch: [grab-model]` and `workflow_dispatch`
  (inputs `repo_id`, `revision`). No `pull_request*` triggers ever.
- Job `plan` → runs `plan_shards.py`, outputs matrix.
- Job `grab` → `needs: plan`, `strategy: { matrix: ..., fail-fast: false }`,
  runs `grab_shard.sh`, uploads result artifact. `permissions: contents: read`.
- Job `collect` → `needs: grab`, `if: always()`. Downloads all artifacts, merges via
  `lib.state.merge_shard_results`, recomputes status, commits and pushes state once.
  `permissions: contents: write`, `concurrency: { group: state-write }`.
  **This is the only job that writes state** (PLAN §2 concurrency rule).
- `timeout-minutes: 350` on grab jobs (under the 6h ceiling).
- Notify on completion with counts uploaded / failed / remaining.

## Acceptance

- Bin packing tested: uneven sizes, single huge file exceeding shard target (must get
  its own shard, not be dropped), zero files, exactly-at-boundary.
- `grab_shard.sh` tested against a local HTTP server + `rclone` pointed at a local
  directory backend — no real HF or R2 needed. Must prove: a mid-file kill leaves the
  file marked NOT uploaded, and a corrupted body is caught by the sha256 comparison.
- Confirm actual peak RSS with 8 parallel `rcat` at the configured chunk size; report
  the measured number. If it exceeds ~4 GB, lower concurrency and say so.
- Re-running a shard with all files already uploaded is a fast no-op.
