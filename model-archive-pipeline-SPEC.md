# Open-Weight Model Archival Pipeline — SPEC

**Goal:** Automatically detect when a target open-weight model (base + abliterated variants) lands on Hugging Face, stream it into zero-egress object storage (Cloudflare R2) via ephemeral compute, then pull it down to a cold-storage drive on your own schedule — with near-zero standing cost while you wait.

**Author target:** run/maintain via Claude Code as the agentic authoring + local-run layer. Scheduling that survives your laptop being off runs on GitHub Actions cron (you already run GH Actions pipelines elsewhere).

---

## 0. Honest status before you build (read this first)

- **The "gold-standard abliterated Kimi K3" does not verifiably exist yet.** Kimi K3's base weights are due on Hugging Face ~**July 27, 2026**. The one pre-staged abliterated repo I found (`audnai/penclaw-Kimi-K3.0-abliterated-GGUF`) states its method is "under commercial NDA, to be detailed upon open weights release." You cannot quality-verify an abliteration of a model whose base weights aren't public. Treat any pre-release "abliterated K3" repo as **unverified** until: (a) base weights are public, and (b) the uploader publishes a KL-divergence / refusal-rate number you can sanity-check.
- **Abliteration = safety training removed at the weight level.** It doesn't just strip political/topic censorship; it removes refusal behavior broadly. That's a factual property of what you'd be archiving, not a warning — plan storage/handling accordingly and don't expose an endpoint you don't control.
- **Size reality (corrects the spoken math):** Kimi K3 is ~2.8T params (MoE). Rough footprints:
  - BF16 ≈ **~5.6 TB** (one model barely fits an 8TB drive)
  - FP8 ≈ **~2.8 TB**
  - GGUF Q4 ≈ **~1.4–1.6 TB**
  - GGUF IQ2 ≈ **~0.7–0.8 TB**
  - "2–3 models on 8TB" only holds at **quantized** precision. At BF16 you get one.
- **No GB/s guarantees.** HF egress is fast but rate-limited and variable; plan for a few hundred MB/s to low-GB/s aggregate with parallel connections, not a fixed multi-GB/s.

---

## 1. Architecture

```
                 ┌──────────────────────────────────────┐
                 │  A. WATCHER (GitHub Actions cron)      │
                 │  - polls HF API every N hours          │
                 │  - watches base org + abliteration     │
                 │    authors, filters by name/regex      │
                 │  - compares vs state file (seen repos)  │
                 │  - on NEW match -> repository_dispatch  │
                 └───────────────┬──────────────────────┘
                                 │ (trigger + repo id, revision)
                                 ▼
                 ┌──────────────────────────────────────┐
                 │  B. GRABBER (ephemeral cloud VM)       │
                 │  - provisioned ONLY on trigger         │
                 │  - huggingface-cli download -> scratch │
                 │  - rclone copy scratch -> R2 bucket    │
                 │  - writes SHA256SUMS + manifest        │
                 │  - self-destructs                      │
                 └───────────────┬──────────────────────┘
                                 │ (lands in R2; free ingress)
                                 ▼
                 ┌──────────────────────────────────────┐
                 │  C. R2 bucket (Cloudflare)             │
                 │  - pay only for GB actually stored     │
                 │  - $0 egress, any volume               │
                 └───────────────┬──────────────────────┘
                                 │ (you pull, on your schedule, $0 egress)
                                 ▼
                 ┌──────────────────────────────────────┐
                 │  D. Cold-storage drive (Seagate CMR)   │
                 │  - rclone copy R2 -> /mnt/archive      │
                 │  - verify checksums, then empty bucket │
                 └──────────────────────────────────────┘
```

**Why this shape:**
- The **watcher** needs no disk and no compute — it's just API polling, so it lives free on GH Actions cron.
- The **grabber** is where bandwidth + disk are needed, so it's ephemeral: provisioned on trigger, killed after. This is the "auto-provision only when needed" you wanted — you pay for the VM-hours of one download, nothing while idle.
- **R2** is pay-per-GB-stored; an empty bucket waiting 4 months costs ~pennies. Its role is the **$0-egress staging area** so pulling to your drive is free.
- **Your drive** is the permanent archive; R2 is transient.

---

## 2. Prerequisites (stage today)

1. **Hugging Face account + token**
   - Create account, generate a **read** access token (`Settings → Access Tokens`).
   - Pre-accept licenses on the base model page(s) so gated repos don't block the automated grab.
2. **Cloudflare R2**
   - Create an R2 bucket, e.g. `model-archive`.
   - Create an R2 **API token** (S3-compatible): gives you `access_key_id`, `secret_access_key`, and an account endpoint `https://<accountid>.r2.cloudflarestorage.com`.
3. **GitHub repo** (private) to host the watcher + workflows.
4. **Ephemeral compute provider** for the grabber (pick one you already have creds for; see §5 for the disk/egress tradeoff). Store its API token as a GH secret.
5. **`rclone`** installed locally for the pull-down step.

GH repo secrets to set:
```
HF_TOKEN
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
R2_ACCOUNT_ID
R2_BUCKET=model-archive
CLOUD_API_TOKEN        # for the ephemeral VM provider
NOTIFY_WEBHOOK         # optional: Slack/Discord/ntfy URL for alerts
```

---

## 3. Watch list (config)

Store as `watchlist.yaml` in the repo. You watch **authors/orgs**, not exact repo names — because you can't poll for a repo that doesn't exist yet by name.

```yaml
# watchlist.yaml
poll_hours: 3
targets:
  # base weights = the reliable archival anchor
  - author: moonshotai
    include: ["Kimi-K3", "Kimi-K2.7", "Kimi-K2.6"]
    priority: base
  # abliteration maintainers seen in the wild (verify before trusting)
  - author: huihui-ai
    include: ["Kimi-K3", "GLM-5", "Qwen3"]
    priority: abliterated
  - author: DavidAU
    include: ["Kimi-K3", "GLM-5", "Heretic"]
    priority: abliterated
  - author: Youssofal
    include: ["Kimi-K3", "Heretic"]
    priority: abliterated
  - author: audnai
    include: ["Kimi-K3"]
    priority: abliterated
  # other frontier bases worth mirroring
  - author: zai-org           # GLM
    include: ["GLM-5"]
    priority: base
  - author: deepseek-ai
    include: ["V4"]
    priority: base
quality_gates:            # applied to abliterated repos before auto-grab
  min_downloads: 50       # skip brand-new zero-signal uploads
  require_method: ["heretic", "abliterat"]   # name/card must mention a known method
  prefer_safetensors_over_gguf: true         # for archival fidelity
```

---

## 4. Component A — Watcher (GitHub Actions)

`.github/workflows/watch.yml`:

```yaml
name: watch-hf
on:
  schedule:
    - cron: "0 */3 * * *"   # every 3h; align with poll_hours
  workflow_dispatch: {}

jobs:
  watch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install huggingface_hub pyyaml requests
      - name: Poll HF
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          NOTIFY_WEBHOOK: ${{ secrets.NOTIFY_WEBHOOK }}
        run: python watcher.py
      - name: Persist seen-state
        run: |
          git config user.name  "watcher-bot"
          git config user.email "bot@users.noreply.github.com"
          git add state/seen.json
          git commit -m "update seen state" || echo "no change"
          git push || echo "nothing to push"
```

`watcher.py`:

```python
import json, os, re, pathlib, requests, yaml
from huggingface_hub import HfApi

api = HfApi(token=os.environ["HF_TOKEN"])
cfg = yaml.safe_load(open("watchlist.yaml"))
gates = cfg["quality_gates"]

STATE = pathlib.Path("state/seen.json")
STATE.parent.mkdir(exist_ok=True)
seen = json.loads(STATE.read_text()) if STATE.exists() else {}

def passes_gates(info, priority):
    if priority == "base":
        return True
    if (info.downloads or 0) < gates["min_downloads"]:
        return False
    name = (info.id + " " + (info.card_data.to_dict().get("license","") if info.card_data else "")).lower()
    if not any(m in name for m in gates["require_method"]):
        return False
    return True

new_hits = []
for t in cfg["targets"]:
    for info in api.list_models(author=t["author"], full=True, limit=200):
        if not any(inc.lower() in info.id.lower() for inc in t["include"]):
            continue
        rev = info.sha  # latest commit sha; re-grab if it changes
        key = info.id
        if seen.get(key) == rev:
            continue
        if not passes_gates(info, t["priority"]):
            continue
        new_hits.append({"repo_id": info.id, "revision": rev,
                         "priority": t["priority"],
                         "downloads": info.downloads or 0})
        seen[key] = rev

STATE.write_text(json.dumps(seen, indent=2))

if new_hits:
    # notify
    if os.environ.get("NOTIFY_WEBHOOK"):
        requests.post(os.environ["NOTIFY_WEBHOOK"],
                      json={"text": f"New model(s): {[h['repo_id'] for h in new_hits]}"})
    # trigger the grabber via repository_dispatch
    gh = os.environ["GH_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    for h in new_hits:
        requests.post(
            f"https://api.github.com/repos/{repo}/dispatches",
            headers={"Authorization": f"Bearer {gh}",
                     "Accept": "application/vnd.github+json"},
            json={"event_type": "grab-model", "client_payload": h},
        )
    print("dispatched:", new_hits)
else:
    print("no new matches")
```

**Note:** `list_models(author=...)` with `sha` lets you re-grab if a repo is updated in place (common with abliterations getting fixed). `min_downloads` intentionally means a brand-new upload won't auto-grab on the very first poll — it waits for community signal. If you want first-mover capture for the **base** model, `priority: base` bypasses gates.

---

## 5. Component B — Grabber (ephemeral VM)

Triggered by `repository_dispatch: grab-model`. The heavy lift can't run on a standard GH runner (≈14 GB disk). Two options:

### Option 1 — Ephemeral cloud VM (recommended for hands-off)
A workflow provisions a VM with a large scratch volume, runs the download+sync, then destroys it. You pay VM-hours for one download only.

`.github/workflows/grab.yml`:

```yaml
name: grab-model
on:
  repository_dispatch:
    types: [grab-model]
  workflow_dispatch:
    inputs:
      repo_id:   { required: true }
      revision:  { required: false, default: "main" }

jobs:
  grab:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Resolve inputs
        id: in
        run: |
          echo "repo_id=${{ github.event.client_payload.repo_id || github.event.inputs.repo_id }}" >> $GITHUB_OUTPUT
          echo "revision=${{ github.event.client_payload.revision || github.event.inputs.revision }}" >> $GITHUB_OUTPUT
      - name: Provision VM, run grab, destroy
        env:
          CLOUD_API_TOKEN: ${{ secrets.CLOUD_API_TOKEN }}
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          R2_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
          R2_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
          R2_ACCOUNT_ID: ${{ secrets.R2_ACCOUNT_ID }}
          R2_BUCKET: ${{ secrets.R2_BUCKET }}
          REPO_ID: ${{ steps.in.outputs.repo_id }}
          REVISION: ${{ steps.in.outputs.revision }}
        run: bash provision_and_grab.sh
```

`grab_on_vm.sh` (the cloud-init / remote script that runs ON the VM):

```bash
#!/usr/bin/env bash
set -euo pipefail

# --- deps ---
pip install -U "huggingface_hub[cli,hf_transfer]"
curl https://rclone.org/install.sh | sudo bash
export HF_HUB_ENABLE_HF_TRANSFER=1   # parallel, faster HF pulls

# --- rclone R2 remote ---
mkdir -p ~/.config/rclone
cat > ~/.config/rclone/rclone.conf <<EOF
[r2]
type = s3
provider = Cloudflare
access_key_id = ${R2_ACCESS_KEY_ID}
secret_access_key = ${R2_SECRET_ACCESS_KEY}
endpoint = https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com
acl = private
no_check_bucket = true
EOF

SAFE_NAME="${REPO_ID//\//__}"   # moonshotai/Kimi-K3 -> moonshotai__Kimi-K3
DEST="/scratch/${SAFE_NAME}"
mkdir -p "$DEST"

# --- download full repo (weights + ALL config/tokenizer/json) ---
huggingface-cli download "$REPO_ID" \
  --revision "$REVISION" \
  --local-dir "$DEST" \
  --local-dir-use-symlinks False \
  --token "$HF_TOKEN"

# --- integrity manifest BEFORE upload ---
( cd "$DEST" && find . -type f -exec sha256sum {} \; > /tmp/SHA256SUMS )
cp /tmp/SHA256SUMS "$DEST/SHA256SUMS"
cat > "$DEST/MANIFEST.json" <<EOF
{ "repo_id": "${REPO_ID}", "revision": "${REVISION}",
  "grabbed_utc": "$(date -u +%FT%TZ)",
  "bytes": $(du -sb "$DEST" | cut -f1) }
EOF

# --- sync to R2 (free ingress) ---
rclone copy "$DEST" "r2:${R2_BUCKET}/${SAFE_NAME}" \
  --transfers 16 --checkers 32 --s3-chunk-size 128M --fast-list -P

echo "DONE -> r2:${R2_BUCKET}/${SAFE_NAME}"
```

**VM sizing:** scratch volume must be > model footprint (e.g., 3 TB for FP8 K3, 6 TB for BF16). Pick a high-bandwidth instance; you only run it for the download window (hours), then destroy it in the same workflow (`provision_and_grab.sh` ends with the provider's `delete` call — always in a `trap`/`finally` so a failed download still tears down the VM).

**Egress note:** HF→VM ingress is normally free; VM→R2 is egress *from the VM* and may incur a small one-time charge on some providers (a few hundred GB, once). R2 ingress itself is free. The big repeated win — pulling from R2 to your drive — is **$0** regardless.

### Option 2 — Your own always-on box
If you have a home server/NAS that's on anyway, skip the VM entirely: GH Actions watcher just fires `NOTIFY_WEBHOOK`, and a local `systemd` service running the same `grab_on_vm.sh` (pointed at a local disk instead of R2, or straight to the archive drive) does the pull. Zero cloud cost, but only captures the window if the box is on and has bandwidth/disk. Given you may be asleep during a release window, Option 1 is the safer capture.

---

## 6. Component C/D — Pull to cold storage (on your schedule, $0 egress)

Local, whenever convenient after a grab:

```bash
# one-time: same [r2] rclone remote as above, in your local rclone.conf

MODEL="moonshotai__Kimi-K3"
ARCHIVE="/mnt/archive"          # your Seagate CMR drive

rclone copy "r2:model-archive/${MODEL}" "${ARCHIVE}/${MODEL}" \
  --transfers 8 --checkers 16 --fast-list -P

# verify integrity against the manifest made on the VM
( cd "${ARCHIVE}/${MODEL}" && sha256sum -c SHA256SUMS )

# if clean, reclaim R2 (stop paying storage):
rclone purge "r2:model-archive/${MODEL}"
```

Then follow cold-storage hygiene: two copies in two places, spin up ~once a year, cool/dry/stable, keep the `SHA256SUMS` so you can re-verify years later.

---

## 7. Cost reality (no hype)

| Item | Cost |
|---|---|
| R2 storage | ~$0.015/GB-mo. Empty bucket idle 4 months ≈ pennies. A 1.5 TB model parked 1 week ≈ **~$5–6** |
| R2 egress (R2 → your drive) | **$0**, any volume |
| GH Actions watcher | polling only; within free/included minutes for a private repo |
| Ephemeral VM | pay per download run (hours), then destroyed. Dominated by instance rate + one-time VM→R2 egress |
| Standing cost while waiting | ≈ **$0** |

The pipeline's whole economic point: **nothing meaningful accrues until a model actually drops**, and the pull-down is free.

---

## 8. Decisions to make before you flip it on

1. **Precision to archive.** Recommend grabbing the **base safetensors** at the highest precision you'll actually store (FP8 ~2.8 TB is the pragmatic frontier archive) **plus** one well-verified abliterated **GGUF Q4** (~1.5 TB) once it exists and passes gates. Base = fidelity anchor; abliterated = usability.
2. **Which ephemeral provider.** Choose on: large attachable volume, high bandwidth, and cheapest VM→R2 egress. Store its CLI token as `CLOUD_API_TOKEN`.
3. **Gate strictness.** `min_downloads` trades capture speed vs. trusting a fresh, unvetted upload. For abliterated repos, waiting for signal is the safer default; for the base model, capture immediately.
4. **Abliterated verification.** Before trusting one for the long-term archive: confirm it's built on the *released* base, prefer **Heretic-method** uploads, look for a published KL-divergence / refusal-rate figure, and favor high-download uploaders (huihui-ai, DavidAU, Youssofal are the recurring names). A repo claiming an abliteration *before* base weights are public is a yellow flag, not a green one.

---

## 9. Today checklist

- [ ] HF account + read token; pre-accept `moonshotai/Kimi-K3` license
- [ ] R2 bucket `model-archive` + S3 API token
- [ ] Private GH repo; add `watchlist.yaml`, `watcher.py`, both workflows
- [ ] Set GH secrets (§2)
- [ ] Pick + wire ephemeral VM provider; store `CLOUD_API_TOKEN`
- [ ] Install rclone locally; add `[r2]` remote
- [ ] Run `watch.yml` via `workflow_dispatch` once to seed `state/seen.json`
- [ ] Format/mount the Seagate CMR drive as `/mnt/archive`
- [ ] Wait for ~July 27; let the watcher fire, or trigger `grab.yml` manually with `repo_id=moonshotai/Kimi-K3` once weights are live
