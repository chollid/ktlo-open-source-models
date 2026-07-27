# Open-Weight Model Archival Pipeline

Watches Hugging Face for a target model, streams it into Cloudflare R2 with no disk,
verifies every byte, and lets you pull it to a cold-storage drive on your own schedule.

Near-zero standing cost while waiting. Nothing meaningful accrues until a model drops.

---

## 0. Read this first

**What is archived** (decided 2026-07-26):

| Artifact | Size | Why |
|---|---|---|
| FP8 base safetensors | ~2.8 TB | Preservation anchor. Probably the native release dtype. Every future quantization derives from it. |
| Q4_K_M GGUF (abliterated, once verified) | ~1.6 TB | The runnable artifact. Loads on a ~$9k dual-EPYC / 2 TB DDR4 box at ~3–10 tok/s. |

**BF16 is never archived.** Likely an upcast of FP8 carrying zero extra information at
2× the bytes, and unrunnable on attainable hardware either way. If a release turns out to
be BF16-only, the pipeline **fails closed and tells you** rather than archiving a
tokenizer and calling it success (see §6).

Total ~4.4 TB, targeting an 8 TB drive.

---

## 1. One-time setup

### 1.1 Hugging Face

1. Create a **read** token: Settings → Access Tokens → New token.
2. **Pre-accept the license** on `moonshotai/Kimi-K3` (and any other gated base model).
   A gated repo will block the automated grab, and you will not be awake to click through.

### 1.2 Cloudflare R2

1. Create a bucket, e.g. `model-archive`.
2. Manage R2 API Tokens → create an S3-compatible token.
3. Record `access_key_id`, `secret_access_key`, and your **account ID**
   (the endpoint is `https://<accountid>.r2.cloudflarestorage.com`).

### 1.3 Alerts (ntfy)

Pick a random topic name — `k3-archive-8f3a2c91`, not `k3-archive`.
**Treat the topic name as a password**: anyone who knows it can read your alerts.
Install the ntfy app and subscribe. No account required.

Your webhook URL is `https://ntfy.sh/<your-topic>`.

Alerts contain repo names and byte counts. They never contain credentials.

### 1.4 GitHub secrets

Repo → Settings → Secrets and variables → Actions:

```
HF_TOKEN
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
R2_ACCOUNT_ID
R2_BUCKET            # e.g. model-archive
NOTIFY_WEBHOOK       # https://ntfy.sh/<your-topic>
```

`DISPATCH_PAT` is **not required** — see §9.1.

### 1.5 Local machine

```bash
brew install rclone          # required for the pull
```

Python **3.12+** is required. A bare `python3` that is older will fail with a clear
message rather than a cryptic `TypeError`. Set `MODEL_ARCHIVE_PYTHON` to an explicit
interpreter if discovery picks the wrong one.

Add the R2 remote to your local `~/.config/rclone/rclone.conf`:

```ini
[r2]
type = s3
provider = Cloudflare
access_key_id = <R2_ACCESS_KEY_ID>
secret_access_key = <R2_SECRET_ACCESS_KEY>
endpoint = https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com
acl = private
no_check_bucket = true
```

Mount your archive drive and export `ARCHIVE=/mnt/archive` (or wherever it lives).

---

## 2. Before anything real: run the smoke test

**Do this first. It is the highest-value four minutes in this project.**

Actions → `smoke` → Run workflow.

It drives the entire real chain against `hf-internal-testing/tiny-random-gpt2` (a few MB)
under a `smoke/` prefix in your real bucket, then deletes it. Cost is effectively zero.

It also injects two deliberate failures and asserts recovery:

- deletes an R2 object → the sweeper must re-dispatch exactly that file
- corrupts an object with same-length wrong bytes → verification must catch the SHA
  mismatch, delete it, and recover

And it reports **peak RSS** during the parallel upload, plus observed throughput. That
throughput number is your input for shard sizing on the real grab.

If a secret is missing you get:

```
ERROR: required GitHub Actions secret <NAME> is missing or empty
```

Kimi K3 lands on a window that cannot be repeated, and this pipeline runs unattended
while you are asleep. Discovering a misconfigured R2 endpoint *then* is the expensive
failure. Discovering it now costs nothing.

### Seed the watcher

Actions → `watch-hf` → Run workflow, once. This populates `state/seen.json` so the first
scheduled poll does not treat every historical repo as new.

---

## 3. Normal flow

```
watch-hf (cron 3h)
  └─ matches a target, filters files, size-gates
     └─ repository_dispatch ──▶ grab-model
                                 ├─ plan   : bin-pack into ≤300 GB shards
                                 ├─ grab   : matrix, streams HF ──▶ R2, no disk
                                 └─ collect: merges shard results, pushes state
sweep (cron 2h)
  ├─ reconciles HF vs R2 vs state, re-dispatches gaps
  ├─ verifies every byte (R2 egress is $0, so this is free)
  └─ status → verified ──▶ ntfy "ready to pull"

you, locally:
  scripts/pull.sh <safe_name> 500G     # nightly budget
  scripts/verify_local.sh <safe_name>
  scripts/reclaim.sh <safe_name> --yes # only after verification passes
```

`safe_name` is the repo id with `/` replaced by `__` — `moonshotai/Kimi-K3` becomes
`moonshotai__Kimi-K3`.

### Manual grab

Actions → `grab-model` → Run workflow, with `repo_id` and optionally `revision`.

---

## 4. Pulling to your drive

**Budgeted by default.** This avoids tripping ISP traffic management on an "unlimited"
plan.

```bash
export ARCHIVE=/mnt/archive R2_BUCKET=model-archive
scripts/pull.sh moonshotai__Kimi-K3 500G
```

Roughly six nights for 2.8 TB. Re-run the identical command each night — it resumes.

Exit codes:

| Code | Meaning | Action |
|---|---|---|
| 0 | Complete | Run `verify_local.sh` |
| 8 | Budget reached | Re-run tomorrow |
| 10 | Time window closed | Re-run |
| other | Error | Re-run; it is idempotent |

Useful flags:

```bash
scripts/pull.sh <name> 500G --max-duration 6h
scripts/pull.sh <name> --bwlimit "08:00,20M 23:00,off"   # throttle by day, open at night
```

### The budget is approximate — know the bound

`--cutoff-mode CAUTIOUS` prevents rclone from *starting* a transfer that would exceed the
budget, but it cannot cancel transfers already in flight. So overshoot is bounded by
roughly **(parallel transfers) × (largest remaining file)**.

`pull.sh` handles this by computing `transfers = clamp(budget ÷ largest_pending, 1, 8)`
and printing the bound:

```
Effective transfers: 2
BUDGET REPORT: requested 5M (5242880 bytes); actual 4194304 bytes in 2 completed file(s)
BUDGET BOUND: 2 transfer(s) x 2097152 largest-pending bytes
```

If your budget is smaller than a single file it warns loudly and makes no progress —
raise the budget to at least one whole file.

### Then verify, then reclaim

```bash
scripts/verify_local.sh moonshotai__Kimi-K3      # sha256 against the job manifest
scripts/reclaim.sh      moonshotai__Kimi-K3      # dry run — prints the plan
scripts/reclaim.sh      moonshotai__Kimi-K3 --yes
```

`reclaim.sh` **re-runs verification itself** and refuses to delete if the local copy is
missing, short, or hash-mismatched. It never trusts a cached "verified" flag.

For a long slow pull, `--verified-only` deletes just what you have already verified,
so the bucket drains as the drive fills. On a month-long pull that roughly halves the
R2 bill.

---

## 5. Costs

| Item | Cost |
|---|---|
| R2 storage | $0.015/GB-month |
| R2 egress (R2 → your drive) | **$0**, any volume |
| Byte verification (full re-read) | **$0** — egress is free, ops are in free tier |
| GitHub Actions | $0 — public repo, unlimited minutes |
| Standing cost while waiting | ≈ **$0** |

**R2 dwell time is the only variable that matters.** 2.8 TB:

| Time parked | Storage cost |
|---|---|
| 3 days | ~$4 |
| 1 week | ~$10 |
| 1 month | ~$42 |

Pull promptly. Use `--verified-only` reclaim if the pull will be slow.

---

## 6. Failure playbook

### Job stuck in `uploading`
The sweeper handles it within 2 hours. To force: Actions → `sweep` → Run workflow.
It reconciles from HF + R2 + state with no memory of what the failed run was doing.

### Job `failed`
Read `last_error` in `state/jobs/<safe_name>.json`. A job goes terminal only after a
single file has failed **more than 5 times**. Fix the cause, reset the attempt count,
and re-dispatch via `grab-model`.

### `no FP8 weights after filtering — appears BF16-only`
The release shipped BF16 only. **This is a decision for you, not a bug.** The alert lists
the weight filenames that were dropped. Either wait for community FP8/GGUF quants, or
deliberately widen `file_rules` in `watchlist.yaml` and accept 5.6 TB.

### `total_bytes exceeds max_total_bytes`
The filter matched more than expected — most often a GGUF repo shipping every quant
level. The alert lists the ten largest files. Tighten `include` in `watchlist.yaml`
(e.g. `["*Q4_K_M*"]`), or raise `max_total_bytes` deliberately.
`seen.json` is not advanced, so a corrected config retries cleanly.

### `SplitSetIntegrityError`
Your filter would keep only some parts of an `-NNNNN-of-MMMMM` set, producing an archive
that will not load. Fix the pattern so it matches all parts of one quant.

### Hash mismatches
Already handled: the object is deleted, the file is reset, and the next sweep re-uploads
it. You only need to intervene if the same file trips the 5-attempt poison pill.

### Orphan objects in R2
Reported loudly, **never auto-deleted** — an unrecognized object could be something you
put there deliberately. Remove manually if you are sure.

### Pull interrupted
Re-run `pull.sh`. It resumes at a clean file boundary. Nothing is lost; R2 holds the
remainder and the drive holds what landed.

### Watcher not firing
Check that the `watch-hf` schedule is enabled — GitHub disables cron on repos with no
activity for 60 days. Push any commit to re-enable.

---

## 7. Cold-storage hygiene

- **Two copies in two places.** R2 is transient; one drive is not an archive.
- Keep `state/jobs/<safe_name>.json` — it is your manifest for re-verification years
  from now, and it is committed to git.
- Spin the drive up about once a year and re-run `verify_local.sh`.
- Cool, dry, stable. CMR, not SMR.

---

## 8. Known limits

- **6-hour GitHub job ceiling.** Shards target 300 GB (≈50 min at a conservative
  100 MB/s), capped at 20 shards, so there is wide margin.
- **HF rate limits** are variable. Retries and the sweeper absorb this.
- **Repo updated mid-grab:** the revision is pinned to a commit SHA, so your archive
  stays internally coherent. A changed SHA becomes a new job.
- **Peak RSS** is measured by the smoke test. Configured budget is
  `--s3-chunk-size 64M × --s3-upload-concurrency 4 × 8 parallel` ≈ 2 GB against a
  ~16 GB runner. Do not raise these without recomputing — exceeding RAM kills the runner
  with no useful error.

---

## 9. Design notes worth not re-litigating

### 9.1 `GITHUB_TOKEN` is correct for dispatch

An earlier revision of this design claimed `GITHUB_TOKEN` cannot trigger
`repository_dispatch` and mandated a PAT. **That was wrong.** GitHub's docs state that
`workflow_dispatch` and `repository_dispatch` **always create workflow runs** — they are
explicit exceptions to the recursion guard.

`GITHUB_TOKEN` is therefore the default. `DISPATCH_PAT` remains an optional override.
Requiring a PAT would have introduced an expiry failure mode: a fine-grained PAT silently
expiring months later is exactly the class of failure this pipeline exists to prevent.

### 9.2 No VM, no scratch disk

The original design provisioned a cloud VM with a multi-TB volume. Streaming HF → R2
through memory needs no disk at all, which removes the provisioning code, the provider
account, and the leaked-VM failure mode where a forgotten 3 TB volume bills silently for
months.

The tradeoff: a VM filling a disk throws an error, while streaming just keeps going and
bills you. The **size sanity gate** (§6) exists specifically to replace the disk running
out as a natural stopping point.

### 9.3 Only one job writes state

Shard jobs emit result artifacts and write nothing. A single serialized `collect` job
merges and pushes once. `grab.yml` collect and `sweep.yml` share a `state-write`
concurrency group so they can never both push.

### 9.4 Destructive operations fail closed

Every deletion path — `reclaim.sh`, the sweeper, smoke cleanup — refuses when it cannot
prove the deletion is safe. Leftover objects cost pennies. A deleted archive is
unrecoverable, and the source repo may not exist anymore.
