# B5 — Local pull / verify / reclaim

**Deps:** none (pure bash, runs on the operator's Mac/Linux box). **Tier:** Codex 5.5 high.

Layer L6. These run by hand on the machine holding the archive drive. Optimize for
"safe to run again at any time, from any state."

## Deliverables

### `scripts/pull.sh <safe_name> [budget]`

Budgeted, resumable R2 → local drive copy.

```bash
rclone copy "r2:${R2_BUCKET}/${SAFE_NAME}" "${ARCHIVE}/${SAFE_NAME}" \
  --max-transfer "${BUDGET:-off}" \
  --cutoff-mode CAUTIOUS \
  --size-only \
  --transfers 8 --checkers 16 --fast-list \
  --log-file "${ARCHIVE}/${SAFE_NAME}.pull.log" --log-level INFO \
  --stats 30s -P
```

Rationale (do not change without reason):
- `CAUTIOUS` refuses to *start* a transfer that would exceed the budget, so every stop
  lands on a clean file boundary and there are never partial files. Default `HARD`
  would kill mid-file.
- `--size-only` — model shards are immutable; size comparison is sufficient and avoids
  a checksum round-trip against R2 (multipart ETags are not MD5).

Exit-code handling (confirmed from rclone `lib/exitcode/exitcode.go`):

| code | meaning | message |
|---|---|---|
| 0 | complete | "PULL COMPLETE — run verify_local.sh" |
| 8 | `TransferExceeded` | "budget reached — rerun to continue" |
| 10 | `DurationExceeded` | "time window closed — rerun to continue" |
| other | error | "error <n> — rerun is safe (idempotent)" |

Preserve and re-exit the original code. Optional flags: `--max-duration`, `--bwlimit`
(support a timetable string, e.g. `"08:00,20M 23:00,off"`).

Print a resume hint on every non-zero exit showing the exact command to rerun.

### `scripts/verify_local.sh <safe_name>`

- Fetch the job's manifest (from `state/jobs/<safe_name>.json`) and check every local
  file's sha256 against it. Do not rely on a `SHA256SUMS` file having been uploaded —
  the state file is authoritative.
- Report pass/fail per file; exit non-zero if any file fails or is missing.
- Emit a re-pull command listing only the bad files.
- Must work offline against a local state copy.

### `scripts/reclaim.sh <safe_name> [--yes]`

Deletes R2 objects. **Destructive — treat accordingly.**

- Refuse to run unless `verify_local.sh` has passed for every file in the manifest.
  Re-run the verification itself; do not trust a cached flag.
- Default to `--dry-run` behavior: print exactly what would be deleted and the total
  bytes, then require explicit `--yes` to proceed.
- Support `--verified-only` for incremental reclaim during a long slow pull (delete
  only objects already verified locally), which halves R2 storage cost on a
  multi-week pull.
- Never delete when the local copy is absent, short, or unverified.
- Print a final confirmation of what was deleted.

## Acceptance

- All three run against a local `rclone` directory backend (no real R2) in tests.
- `pull.sh` interrupted at any point and rerun reaches the same final state.
- `reclaim.sh` refuses on: missing local files, size mismatch, hash mismatch, and
  absent `--yes`. Prove each refusal with a test.
- Scripts pass `shellcheck`.
