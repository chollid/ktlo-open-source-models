# PLAN — shared contracts

Every batch (B1–B7) MUST conform to this file. It is the single source of truth for
schemas, naming, secrets, and invariants. Do not invent alternatives.

Parent spec: `../model-archive-pipeline-SPEC.md` (§5 grabber is SUPERSEDED — see below).

---

## 0. Architecture delta vs. SPEC

The SPEC's §5 "ephemeral cloud VM with multi-TB scratch volume" is **replaced** by a
**disk-free streaming grabber** that runs on GitHub Actions runners.

| SPEC said | We build |
|---|---|
| Provision VM + 3TB volume, `hf download` to `/scratch`, then rclone → R2 | Stream each file HF → R2 directly, never touching disk |
| `CLOUD_API_TOKEN`, provisioning + teardown scripts | None. No VM, no provider account |
| `sha256sum` over local tree | SHA256 from HF LFS `oid` (authoritative) + hash computed in-stream at upload |
| Single monolithic job | Sharded matrix jobs + a convergence sweeper |

Rationale: no multi-TB scratch is needed if bytes are never stored; removes the
leaked-VM failure mode; GH Actions minutes are free on a public repo.

**Repo is PUBLIC.** Actions minutes are unlimited and free. See §6 for the security
rules this imposes.

---

## 1. Naming

`safe_name` = `repo_id` with `/` replaced by `__`.

    moonshotai/Kimi-K3  ->  moonshotai__Kimi-K3

R2 object key for a repo file `path/to/f.safetensors`:

    r2:${R2_BUCKET}/${safe_name}/path/to/f.safetensors

State file path:

    state/jobs/${safe_name}.json

Seen-repos file (watcher only):

    state/seen.json      # { "<repo_id>": "<revision sha>" }

---

## 2. Job state schema — `state/jobs/<safe_name>.json`

This is the durable, git-committed state machine. Every batch reads/writes it only
through `lib/state.py`. Never hand-roll JSON access.

```json
{
  "repo_id": "moonshotai/Kimi-K3",
  "safe_name": "moonshotai__Kimi-K3",
  "revision": "e3f9a1c...",
  "priority": "base",
  "status": "discovered",
  "discovered_utc": "2026-07-27T04:00:00Z",
  "updated_utc": "2026-07-27T04:00:00Z",
  "total_bytes": 2800000000000,
  "total_files": 563,
  "files": {
    "model-00001-of-00560.safetensors": {
      "size": 4998123456,
      "sha256": "9f86d081884c7d65...",
      "lfs": true,
      "uploaded": false,
      "verified": false,
      "attempts": 0,
      "last_error": null
    }
  }
}
```

### status values (strict enum, forward-only except on retry)

| status | meaning | set by |
|---|---|---|
| `discovered` | watcher matched the repo, file list not yet enumerated | B2 watcher |
| `planned` | file list enumerated, shards computed | B3 plan_shards |
| `uploading` | at least one shard job running | B3 grab workflow |
| `verifying` | all files `uploaded`, hash check in progress | B4 sweeper |
| `verified` | every file `uploaded` AND `verified` — safe to pull | B4 sweeper |
| `pulled` | operator confirmed local copy verified | B5 (manual, `verify_local.sh`) |
| `failed` | a file exceeded `MAX_ATTEMPTS` — human needed | any |

Terminal states: `verified` (until manually advanced to `pulled`), `failed`.

### field rules

- `sha256` — from HF LFS metadata. `null` for non-LFS files (small config/tokenizer
  files); those are verified by size + the hash computed in-stream at upload.
- `lfs` — true if HF reports LFS metadata for this file.
- `attempts` — incremented on every failed upload OR failed verification of that file.
- `MAX_ATTEMPTS = 5`. On exceeding, set job `status: failed`, set `last_error`, notify.
- `updated_utc` — refreshed on every write. Use `datetime.now(timezone.utc)`.

### concurrency

Shard jobs run in parallel and MUST NOT all write the state file (git push races).
Rule: **shard jobs write nothing.** Each shard emits a JSON result artifact
(`shard-<n>-result.json`) listing `{path: {uploaded, sha256_observed, error}}`.
A single serialized `collect` job (`needs: [grab]`) merges artifacts into the state
file and pushes once. The sweeper is the only other writer and is `concurrency`-gated.

Use a GH Actions `concurrency: group: state-write` on every job that pushes state.

---

## 3. HF metadata contract — `lib/hfmeta.py`

Source of truth for file list, sizes, and SHA256.

- Use `huggingface_hub.HfApi`.
- **Verify the exact API surface against current huggingface_hub docs before writing**
  (`npx ctx7@latest library huggingface_hub "list repo files with size and lfs sha256"`
  then `npx ctx7@latest docs <id> "<question>"`). Do not trust memory for attribute
  names — the `siblings` / `files_metadata` / LFS-info field names have changed across
  versions.
- Intent: fetch model info with file metadata enabled, and for each file return
  `{path, size, sha256_or_none, is_lfs}`.
- Must pin `revision` to a commit SHA, never `main`, so a repo updated mid-grab does
  not produce a mixed archive.
- Must work for gated repos using `HF_TOKEN`.

Download URL for streaming a single file:

    https://huggingface.co/{repo_id}/resolve/{revision}/{path}

with header `Authorization: Bearer ${HF_TOKEN}` and following redirects (HF 302s to a
CDN). Public repos work without the header; send it always.

---

## 4. Streaming upload contract (B3)

Per file, one pass, no disk:

```bash
curl -sSL --fail --retry 8 --retry-all-errors --retry-delay 5 \
     -H "Authorization: Bearer ${HF_TOKEN}" \
     "https://huggingface.co/${REPO_ID}/resolve/${REVISION}/${FILE}" \
  | tee >(sha256sum | cut -d' ' -f1 > "${TMP}/${SAFE_FILE}.sha") \
  | rclone rcat "r2:${R2_BUCKET}/${SAFE_NAME}/${FILE}" \
      --s3-chunk-size 64M \
      --s3-upload-concurrency 4 \
      --retries 5 --low-level-retries 20
```

- `tee >(sha256sum)` hashes **exactly the bytes uploaded**, in the same pass. Compare
  against the HF-declared `sha256`; mismatch ⇒ file failed, do not mark uploaded.
- Parallelism across files within a shard: `xargs -P 8`.
- **Memory budget is a hard constraint.** `--s3-chunk-size 64M × --s3-upload-concurrency 4`
  ≈ 256 MB per rcat process; × 8 parallel ≈ 2 GB. GH runners have ~16 GB. Do not raise
  these without recomputing. Exceeding RAM kills the runner with no useful error.
- `bash` (not `sh`) is required — process substitution.
- Skip any file already marked `uploaded: true` in state.

### sharding (`plan_shards.py`)

- Greedy largest-first bin packing over file sizes.
- Target shard size **300 GB**; `N = clamp(ceil(total_bytes / 300GB), 1, 20)`.
- Emits GH Actions matrix JSON to `$GITHUB_OUTPUT`.
- Rationale for the cap: GH job limit is 6h; 300 GB at a conservative 100 MB/s ≈ 50 min,
  leaving wide margin for a slow HF day.
- `strategy.fail-fast: false` — one bad shard must not cancel the others.

---

## 5. Failsafe layers (all six must exist)

| L | Mechanism | Where |
|---|---|---|
| L1 | Idempotent transfer — skip files already `uploaded` | B3 |
| L2 | In-transfer retry — `curl --retry 8`, `rclone --retries 5 --low-level-retries 20` | B3 |
| L3 | Shard isolation — matrix + `fail-fast: false` | B3 |
| L4 | **Sweeper** — cron every 2h; diff HF file list vs `rclone lsjson` of R2; re-dispatch only missing/short files. Converges regardless of how a job died | B4 |
| L5 | **Byte verification** — `rclone cat` each object, sha256, compare to HF `oid`. R2 egress is $0 so this is free. Mismatch ⇒ delete object, reset `uploaded`, re-dispatch | B4 |
| L6 | **Budgeted resumable pull** — `--max-transfer` + `--cutoff-mode CAUTIOUS` + `--size-only` | B5 |

L4 is the real safety net: it must converge a half-finished job with no memory of what
the failed run was doing, using only HF + R2 + the state file.

### poison-pill guard

`attempts > 5` on any file ⇒ job `status: failed`, notify, stop retrying that job.
Prevents an infinite retry loop.

### purge guard

R2 deletion is **never** automatic. `reclaim.sh` is manual-only and must refuse to run
unless local `sha256sum -c` has passed for every file in the manifest.

---

## 6. Secrets and public-repo security

Repo is PUBLIC. Secrets live in GH Actions secrets, never in files.

```
HF_TOKEN               # HF read token
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
R2_ACCOUNT_ID
R2_BUCKET              # e.g. model-archive
DISPATCH_PAT           # fine-grained PAT, contents:write — see below
NOTIFY_WEBHOOK         # optional
```

### CRITICAL — `GITHUB_TOKEN` cannot trigger the grabber

GitHub blocks events raised using `GITHUB_TOKEN` from starting new workflow runs
(anti-recursion). A `repository_dispatch` POST authenticated with `GITHUB_TOKEN`
returns **204 Success and silently does nothing**.

⇒ The watcher MUST authenticate its `repository_dispatch` call with `DISPATCH_PAT`
(fine-grained PAT, `contents: write` on this repo only). The SPEC §4 code using
`secrets.GITHUB_TOKEN` for dispatch is wrong and must not be copied.

Include an assertion in `watcher.py`: if `DISPATCH_PAT` is unset, fail loudly rather
than degrade.

### public-repo hardening (mandatory)

- Workflows trigger ONLY on `schedule`, `workflow_dispatch`, `repository_dispatch`.
- **Never** use `pull_request_target`, and never `pull_request` on a workflow that
  touches secrets — a fork PR would exfiltrate them.
- Set least-privilege `permissions:` at workflow level. State-pushing jobs get
  `contents: write`; everything else `contents: read`.
- Never echo a secret. Set `HF_TOKEN` via env, not on an argv line (argv is visible in
  process listings and in `set -x` output). No `set -x` in scripts handling secrets.
- `.gitignore` must cover `rclone.conf`, `*.sha`, `.env`, `shard-*-result.json`.

### rclone config

Generate at runtime from env into `~/.config/rclone/rclone.conf`; never commit it.

```
[r2]
type = s3
provider = Cloudflare
access_key_id = ${R2_ACCESS_KEY_ID}
secret_access_key = ${R2_SECRET_ACCESS_KEY}
endpoint = https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com
acl = private
no_check_bucket = true
```

---

## 7. Conventions

- Python 3.12. Standard library + `huggingface_hub`, `pyyaml`, `requests`. No heavy deps.
- Bash scripts: `#!/usr/bin/env bash`, `set -euo pipefail`.
- All timestamps UTC ISO-8601 with `Z`.
- All shared logic in `lib/`; no duplication across batches.
- Every script supports `--dry-run` where it mutates anything (R2, state, dispatch).
- Notifications via `lib/notify.py`; no-op cleanly when `NOTIFY_WEBHOOK` is unset.
- Unit tests with `pytest`, HF API and rclone mocked. No network in tests.

## 8. Repo layout

```
watchlist.yaml
lib/{hfmeta,state,notify}.py
watcher.py
plan_shards.py
grab_shard.sh
sweeper.py
verify_remote.py
scripts/{pull,verify_local,reclaim}.sh
.github/workflows/{watch,grab,sweep}.yml
state/seen.json
state/jobs/*.json
tests/
README.md
```
