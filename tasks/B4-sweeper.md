# B4 — Sweeper + remote verification

**Deps:** B1, B3. **Blocks:** none. **Tier:** Codex 5.5 xhigh.

The safety net (L4 + L5). Must converge a half-finished job with **no memory of what
the failed run was doing** — using only HF's file list, R2's object list, and the state
file. Assume the previous run died at the worst possible moment.

## Deliverables

### `sweeper.py`

For every `state/jobs/*.json` not in a terminal state (`verified`, `pulled`, `failed`):

1. Fetch the authoritative HF file list at the job's pinned revision.
2. `rclone lsjson r2:$R2_BUCKET/$SAFE_NAME --recursive` for what actually landed.
3. Diff:
   - in HF, absent from R2 ⇒ reset `uploaded: false`
   - present but size mismatch ⇒ delete the R2 object, reset `uploaded: false`
   - in R2, absent from HF ⇒ report as orphan; do NOT delete (could be a manual copy).
     Log loudly.
   - state says `uploaded` but R2 disagrees ⇒ trust R2, reset
4. If any file needs re-upload, `repository_dispatch` a new `grab-model` for that job
   (using `DISPATCH_PAT` — PLAN §6). The grabber's skip-if-uploaded logic makes this
   cheap.
5. If every file is `uploaded` but not all `verified`, set status `verifying` and run
   `verify_remote.py`.
6. If every file is `uploaded` and `verified`, set `verified`, notify "ready to pull".
7. Respect the poison-pill guard: any file over `MAX_ATTEMPTS` ⇒ job `failed` + notify.
   Never re-dispatch a `failed` job.

Idempotent. Safe to run concurrently with nothing else running; guarded by
`concurrency: state-write` at the workflow level.

`--dry-run` prints the diff and intended actions, mutates nothing.

### `verify_remote.py`

Byte-level verification. R2 egress is $0, so this is free — read everything.

- For each file `uploaded && !verified`: `rclone cat r2:$BUCKET/$SAFE/$PATH` piped to a
  streaming sha256 (constant memory — never buffer a 5 GB file).
- Compare to the HF-declared `sha256`. For non-LFS files (`sha256 is None`) compare
  size only and mark verified with a `size_only: true` note.
- Match ⇒ `mark_verified`. Mismatch ⇒ delete the R2 object, `reset_file`,
  `record_failure`, so the next sweep re-uploads it.
- `--limit N` / `--max-bytes` so a run can be bounded when needed.
- Report total bytes verified and wall time.

### `.github/workflows/sweep.yml`

- Triggers: `schedule` (cron `0 */2 * * *`) and `workflow_dispatch`.
- `permissions: contents: write`; `concurrency: { group: state-write, cancel-in-progress: false }`.
- Runs `sweeper.py`, commits and pushes state.
- `timeout-minutes: 350`.
- Exits 0 when there is nothing to do — a no-op sweep must not look like a failure.

## Acceptance

- Tests with a mocked HF list and a fake `rclone lsjson` payload covering: nothing
  landed, partial, all landed unverified, all verified, size mismatch, orphan object,
  poison-pill trip.
- Prove convergence: simulate a job killed mid-shard, run the sweeper, confirm it
  re-dispatches exactly the missing files and nothing else.
- `verify_remote.py` memory must stay flat regardless of file size — demonstrate with a
  large synthetic stream.
- Confirm a job already `verified` is a complete no-op (no HF calls, no R2 calls).
