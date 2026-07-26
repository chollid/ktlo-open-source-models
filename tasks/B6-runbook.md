# B6 — Runbook / README

**Deps:** all. **Owner:** Claude (docs exception — not dispatched to Codex).

Write `README.md` after B1–B5 land, reflecting what was actually built rather than what
was planned.

## Must cover

1. **One-time setup** — HF token, pre-accepted licenses, R2 bucket + S3 API token,
   `DISPATCH_PAT` creation (fine-grained, `contents: write`, this repo only), all GH
   secrets, local rclone `[r2]` remote, archive drive mount.
2. **Why `DISPATCH_PAT` and not `GITHUB_TOKEN`** — the silent-204 failure mode, so it
   is never "simplified" back later.
3. **Seeding** — run `watch.yml` via `workflow_dispatch` once to populate
   `state/seen.json` so the first real poll does not dispatch every historical repo.
4. **Normal flow** — watcher fires → grab shards → collect → sweeper verifies →
   notification → `pull.sh` → `verify_local.sh` → `reclaim.sh`.
5. **Manual grab** — `workflow_dispatch` on `grab.yml` with `repo_id` / `revision`.
6. **Failure playbook**, one section per state:
   - job stuck in `uploading` → sweeper handles it; force with `workflow_dispatch` on sweep
   - job `failed` → read `last_error`, fix, reset attempts, re-dispatch
   - hash mismatches → what the sweeper already did automatically
   - orphan objects in R2 → why they are never auto-deleted
   - pull interrupted → just rerun `pull.sh`
7. **Cost table** — real numbers, with the point that **R2 dwell time is the only
   variable that matters**; pull promptly.
8. **Cold-storage hygiene** — two copies, annual spin-up, keep the manifest for
   re-verification years later.
9. **Known limits** — 6h job ceiling and the shard sizing that respects it; HF rate
   limits; what happens if a repo is updated mid-grab (revision is pinned, so the
   archive stays coherent; a changed sha becomes a new job).

Keep it operational — commands someone can paste at 3am, not prose.
