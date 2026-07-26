# B7 — End-to-end smoke test

**Deps:** B1–B5. **Tier:** Codex 5.5 high. **Run before anything real.**

The single highest-value task here. K3 lands imminently, the pipeline fires unattended,
and you get one shot at the window. Finding out the R2 endpoint is misconfigured *then*
is the expensive failure. This exercises the entire path for ~$0 in about four minutes.

## Deliverables

### `.github/workflows/smoke.yml`

`workflow_dispatch` only. Runs the **real** pipeline against a tiny public model
(`hf-internal-testing/tiny-random-gpt2`, a few MB) using the **real** HF token and
**real** R2 bucket under a `smoke/` key prefix.

Sequence, asserting at every step:

1. `lib.hfmeta` — resolve revision to a SHA, list files. Assert non-empty, assert at
   least one file reports a size, record whether LFS sha256 is present.
2. Create job state. Assert file written and schema-valid.
3. `plan_shards.py` — assert a valid matrix (will be a single shard).
4. `grab_shard.sh` — real stream HF → R2. Assert every file uploaded and every
   in-stream sha256 matched.
5. `collect` — assert state merged correctly.
6. `sweeper.py` — assert it reports fully converged, dispatches nothing.
7. `verify_remote.py` — real `rclone cat` from R2, assert all hashes match.
8. Assert job status is `verified`.
9. **Negative test:** delete one object from R2, rerun the sweeper, assert it detects
   the gap and re-dispatches exactly that one file. This is the layer that matters most
   and the one hardest to trust untested.
10. **Negative test:** corrupt one object (overwrite with wrong bytes of the same
    length), rerun `verify_remote.py`, assert mismatch detected, object deleted, file
    reset.
11. Cleanup: purge the `smoke/` prefix. Always runs, `if: always()`.

Job must fail loudly on any assertion. Post the full result to `NOTIFY_WEBHOOK` so the
alerting path is proven too — a notification channel that has never fired is not a
notification channel.

### `scripts/smoke_local.sh`

Local half: `pull.sh` the smoke model to a temp dir, `verify_local.sh`, then
`reclaim.sh --yes`. Proves the operator-side tools work against real R2 before you need
them at 3am with 2.8 TB in flight.

Also exercise the budget path: run `pull.sh` with a budget smaller than the model,
assert exit code 8, rerun, assert completion. That proves resumability end to end.

## Acceptance

- Green run start to finish against real HF + real R2.
- Both negative tests demonstrably fail-then-recover.
- Total R2 spend for a run is effectively zero; confirm the `smoke/` prefix is empty
  afterward.
- Document the measured wall time and observed throughput in the run summary — that
  number is the input to shard sizing for the real grab.
