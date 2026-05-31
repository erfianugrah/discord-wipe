# Operations runbook

Everything you do day-to-day with discord-wipe on `servarr`.

## Deploy paths

- **Via Composer** (GitOps, primary): registered as the `discord-wipe`
  stack; push to `main` → GitHub Actions builds + pushes
  `ghcr.io/erfianugrah/discord-wipe:main` → Composer is meant to pull
  via its webhook listener and `docker compose up -d` redeploy. See
  "Composer wiring" below. **In practice the auto-pull has been flaky
  for this stack** (verified 2026-05-28 against v0.3.0 + v0.3.1): the
  image landed on ghcr but composer didn't redeploy either time. After
  `git push`, always verify the running image matches the latest commit:
  ```sh
  ssh servarr 'docker inspect discord-wipe --format "{{.Created}} {{.Image}}"'
  ```
  If `{{.Created}}` is older than your push timestamp, force a pull
  via the composer container (the docker compose binary lives there,
  not on the Unraid host):
  ```sh
  ssh servarr 'docker exec composer sh -c "cd /opt/stacks/discord-wipe && docker compose pull && docker compose up -d"'
  ```
  Or via the composer API (also bypasses webhook):
  ```sh
  curl -sf -X POST -H "X-API-Key: $COMPOSER_API_KEY" \
      "https://composer.erfi.io/api/v1/stacks/discord-wipe/up?async=true"
  ```
- **Direct on the host** (Composer down, or you want a quick out-of-band
  redeploy):
  ```sh
  ssh servarr 'cd /mnt/user/composer/stacks/discord-wipe && \
      docker compose pull && docker compose up -d'
  ```
- **From source** (first-time bring-up, before composer was registered):
  ```sh
  ssh servarr 'git clone git@github.com:erfianugrah/discord-wipe.git \
      /mnt/user/composer/stacks/discord-wipe'
  ssh servarr 'umask 077 && cat > /mnt/user/composer/stacks/discord-wipe/.env' <<EOF
  DISCORD_TOKEN=your-token-here
  DISCORD_WIPE_TAG=main
  TZ=Europe/Amsterdam
  EOF
  ssh servarr 'chown nobody:users /mnt/user/composer/stacks/discord-wipe/.env'
  ssh servarr 'mkdir -p /mnt/user/discord-wipe/{export,state} && \
      chown -R 99:100 /mnt/user/discord-wipe'
  rsync -a ~/erfi-bot/data/exports/discord/package/Messages/ \
      servarr:/mnt/user/discord-wipe/export/Messages/
  ssh servarr 'cd /mnt/user/composer/stacks/discord-wipe && docker compose up -d'
  ```

## Day-to-day operations

| What | How |
|---|---|
| Live log | `ssh servarr docker logs -f discord-wipe` (or composer dashboard at `composer.erfi.io`) |
| Composer log API | `curl -sf -H "X-API-Key: $COMPOSER_API_KEY" https://composer.erfi.io/api/v1/containers/<container-id>/logs?tail=200 \| jq -r '.logs[]'` |
| Recent errors | `ssh servarr 'docker logs discord-wipe 2>&1 \| rg -i "error\|429\|forbidden\|fatal\|terminal 400"'` |
| State summary (v0.4.0+) | `ssh servarr docker exec discord-wipe discord-wipe status` (no API call; reads state.json + heartbeat directly) |
| Prometheus scrape | `ssh servarr curl -s http://127.0.0.1:9090/metrics` (v0.4.0+; localhost-bound on the host) |
| Deleted count | `ssh servarr 'jq ".deleted \| length" /mnt/user/discord-wipe/state/state.json'` |
| State summary (raw) | `ssh servarr 'jq "{deleted: (.deleted\|length), export_consumed, last_pass_at, restart_burst}" /mnt/user/discord-wipe/state/state.json'` |
| Pause cleanly | `ssh servarr docker stop discord-wipe` (SIGTERM, state saved) |
| Resume | `ssh servarr docker start discord-wipe` |
| Force pass now | `ssh servarr docker restart discord-wipe` (interrupts current sleep, starts fresh pass) |
| Bump image | `git push` triggers ghcr build + composer redeploy; or `curl -X POST .../stacks/discord-wipe/pull?async=true` |
| Pin to a build | edit `.env` on servarr, set `DISCORD_WIPE_TAG=sha-<short>` (or `v1.2.3`), `docker compose up -d` |
| Tighten retention | edit `RETENTION_DAYS` in compose.yaml, commit + push (composer redeploys) |
| Skip a guild | add `--exclude-guild <ID>` to the compose `command:`, commit + push |
| Skip a DM | add `--exclude-channel <ID>` similarly |
| Rotate token | see `docs/TOKEN.md` |

## First-run expectations

The first pass deletes ~105K messages from the export. With
header-driven pacing the actual rate settles at ~1.5–1.7s/delete
(Discord's per-account bucket is tighter than the docs suggest), so
the backfill takes **~40–50 hours** rather than the theoretical 29h
at the 1.0s floor. Container stays healthy throughout — `docker logs`
shows steady progress.

Crash recovery: stop / restart at any point. The state file means we
skip already-done IDs and the resume is auditable from logs as:

```
[export] resume: 4521/105327 (4.3%) already done across 6/194
         fully-done channels; 100806 targets remaining
[export 1/194] DM         Direct Message with friend-a#0
               9/9 already done — skip
```

Network blip mid-pass is harmless — the retry handler catches the
next request, and the loss of in-flight progress is bounded by the
"save state every 10 deletes" cadence.

## Reading the live logs

The per-progress line shape:

```
    50/2314 ok=50 gone=0 403=0 | total: 4571/105327 (4.3%) ~36/min ETA 48h59m
```

- `50/2314` — progress within the current channel.
- `ok=50 gone=0 403=0` — per-channel outcomes:
  - `ok` = 204 successful delete
  - `gone` = 404 (Discord already cleaned it / you deleted elsewhere)
  - `403` = forbidden OR HTTP 400 with a terminal Discord code
    (50083 thread archived, 50001 missing access, etc).
- `total: X/Y (Z%)` — cumulative across all channels in this pass,
  with the export's grand total as denominator.
- `~N/min` — running throughput, averaged from pass start.
- `ETA Xh Ym` — estimate based on running throughput.

Log lines you'll see and why:

| Line | What it means | Action |
|---|---|---|
| `[export] resume: X/Y ...` | Pre-scan summary of the export | None — informational |
| `[export N/194] ... N/N already done — skip` | Whole channel was completed in a prior run | None — confirms zero wasted API calls |
| `[delete] terminal 400 code=50083 ...` | Archived thread (or other semantic terminal-400) | None — counted as forbidden |
| `rate-limited; sleep 1.2s` | 5xx retry or significant cooldown | Watch if sustained — may mean account flagged |
| `[FATAL] DISCORD TOKEN REJECTED` | 401 | Rotate token per `docs/TOKEN.md` |
| `[run] === pass start ===` | New pass beginning | None |
| `[catchup N/82] guild:...` | Live phase querying a scope | None |

## Steady state (after backfill)

Every `INTERVAL_HOURS` (default 24):

1. `get_me` pre-flight (catches token rotation).
2. Phase 1 skipped (`export_consumed=True`).
3. Phase 2 sweeps every guild + DM with `messages/search`, deletes
   anything older than `now - RETENTION_DAYS`.
4. Sleep until next pass.

On a typical day you'll see Phase 2 find 0-50 messages per scope and
wrap up in a few minutes.

## Observability (v0.4.0+)

Four layers feed status info to operators:

1. **Heartbeat file** at `/data/state/heartbeat` (bind-mounted to
   `/mnt/user/discord-wipe/state/heartbeat`). Touched on every
   `state.save()` during a pass + every 60s during the long
   inter-pass sleep. Used by:

2. **Docker HEALTHCHECK** — inspects heartbeat mtime, marks unhealthy
   if older than 25h (covers a full INTERVAL_HOURS=24 sleep + buffer).
   Visible in composer's dashboard and `docker ps`.

3. **`discord-wipe status`** subcommand — reads state.json +
   heartbeat, prints a no-API-call summary. Safe to run while a
   wipe is in progress (state writes are atomic via `.tmp + rename`).
   Run from anywhere with read access to the bind-mount:
   ```sh
   ssh servarr docker exec discord-wipe discord-wipe status
   # or, directly against the host file:
   ssh servarr 'python3 -c "
   import json; d=json.load(open(\"/mnt/user/discord-wipe/state/state.json\"))
   print({k: d.get(k) for k in [\"export_consumed\",\"last_pass_at\",\"restart_burst\"]})
   "'
   ```

4. **Prometheus `/metrics`** on port 9090. Compose maps it to
   `127.0.0.1:9090` on the host so external scrapers are blocked.
   Local Prom config:
   ```yaml
   - job_name: discord-wipe
     static_configs:
       - targets: ['127.0.0.1:9090']
   ```
   Series emitted:
   - `discord_wipe_deletes_total{outcome="ok|gone|forbidden"}`
   - `discord_wipe_state_deleted_count`
   - `discord_wipe_export_consumed`
   - `discord_wipe_parked`
   - `discord_wipe_extra_sleep_seconds{phase="export|catchup"}` —
     current pacing floor; useful for spotting bucket-tightening
   - `discord_wipe_last_pass_{start,end}_seconds`

### Push notifications on park

Set `NTFY_URL=https://ntfy.sh/<topic>` in `.env`:

```sh
ssh servarr 'cat >> /mnt/user/composer/stacks/discord-wipe/.env <<EOF
NTFY_URL=https://ntfy.sh/discord-wipe-yourname
EOF
docker compose up -d discord-wipe'
```

The daemon POSTs a high-priority notification on every park event
(401, identity-change, state-unwritable, restart-burst). Subscribe
to the topic on your phone or any ntfy client.

## Failure modes

### 401 — token rejected

The daemon detects this at the `get_me` pre-flight and parks itself
(see `docs/TOKEN.md`). Logs print a large FATAL banner with rotation
instructions. The container stays alive (no restart-loop hammering)
until you `docker compose up -d` with a new `.env`.

### Identity-change — token swapped to a different account

Same paused-exit shape as 401, different banner. Triggers when the
per-pass pre-flight `GET /users/@me` returns a user ID that doesn't
match the one we cached at startup (e.g. `.env` was edited to a
different account's token mid-loop). Without this guard the script
would search for the original user's messages under the new user's
permissions — mostly silent 403s. Banner reads:
```
identity changed mid-loop: was @oldname (id=...), now @newname (id=...)
```
Resolution: edit `.env` back to the right account's token and
`docker compose up -d` (same SIGTERM handshake as the 401 case).

### State-unwritable — disk full, FS frozen, perms wrong (v0.4.0+)

When `state.save()` raises `OSError` (the bind-mount target is
missing, the filesystem is full / frozen, or ownership is wrong),
the daemon parks itself with a FATAL banner explaining the cause.
Fires `NTFY_URL` if configured.

Without this guard, an unwritable state path would crash the script
every pass and `restart: unless-stopped` would respawn it into a hot
loop. Diagnose:
```sh
ssh servarr 'ls -la /mnt/user/discord-wipe/state/ && df -h /mnt/user/'
```
Fix permissions (`chown 99:100`) or free space, then
`docker compose up -d` to recreate the container.

### Restart burst — broken release looping (v0.4.0+)

If the container has restarted >5 times within 600 seconds (counter
persisted in `state.json` as `restart_burst`), the daemon parks
itself instead of letting `restart: unless-stopped` keep spinning.
Defends against a broken `:main` image, missing token, or any
crash-on-startup condition.

Reset by editing `state.json` (`"restart_burst": 0`) AFTER fixing
the cause, then `docker compose up -d`.
Or pin to a known-good build via `.env`:
```
DISCORD_WIPE_TAG=sha-<previous-good-short>
```

### 429 — rate-limited

Handled. The script reads `retry_after` from the body (or
`Retry-After` header) and sleeps. Persistent 429s on a fresh start
usually mean Discord has flagged the account for abuse-suspicion —
stop, wait a day, restart with `DELETE_DELAY=2.0` for a few passes.

### 403 — forbidden

Treated as a non-error per-message: counted as `forbidden`, marked
done in state, moved on. Causes:

- Message is a system message (call, pin, join) — not yours to delete.
- Channel has been revoked since the export was generated.
- DM partner blocked you.

### 404 — already gone

Counted as `gone`, marked done. Either Discord cleaned it up first or
you deleted it elsewhere.

### Unexpected 4xx / 5xx

Logged with status code + first 200 chars of body. Caller retries the
endpoint with backoff. If a specific endpoint keeps failing,
`docker logs` will show repeated entries — file an issue with the
response body.

## Composer wiring

Composer (at `composer.erfi.io`) manages this stack via GitOps. Setup
is one-time, then every push to `main` auto-redeploys:

1. Stack registered as `discord-wipe`, git-backed, tracking `main` on
   `git@github.com:erfianugrah/discord-wipe.git`.
2. Compose file points at `ghcr.io/erfianugrah/discord-wipe:main`
   (no local build needed in prod).
3. GitHub webhook on push → composer endpoint
   `https://composer.erfi.io/api/v1/hooks/<id>` → composer pulls the
   new compose.yaml from git and runs `docker compose pull && up -d`.
4. The `.env` lives outside git on the host
   (`/mnt/user/composer/stacks/discord-wipe/.env`, chmod 600) and survives
   redeploys.

To set this up the first time, see the composer skill or
`docs.erfi.io/composer/`. The agent should NOT run
`./composerd` on the dev machine.

### Optional: pin to a tag instead of `:main`

If you want manual control over upgrades, edit `compose.yaml` and
change `image: ghcr.io/erfianugrah/discord-wipe:main` to
`:v1.2.3`. Then bump only when you've audited the release.

## Reading the state file

```sh
ssh servarr 'jq "{
  deleted: (.deleted | length),
  export_consumed,
  last_pass_at,
  total_passes,
  sample_ids: (.deleted | .[0:3])
}" /mnt/user/discord-wipe/state/state.json'
```

If `deleted` is growing every pass and `last_pass_at` is recent (<2 ×
`INTERVAL_HOURS` ago), everything's working. If `deleted` is stuck and
`last_pass_at` is old, the daemon is either parked on 401, parked on
identity-change, or stopped. `docker logs` will say which.

### Corrupt state.json recovery

If the script can't parse `state.json` on load (rare, but happens if
the filesystem flushed mid-write or someone hand-edited and broke the
JSON), it:

1. Renames the bad file to `state.json.corrupt-YYYYMMDDTHHMMSSZ`.
2. Prints a WARN line: `[state] WARN: ... is corrupt (...); moved to ...; starting fresh`.
3. Resumes with empty state — every "forgotten" ID gets re-issued,
   hits Discord 404, is counted as `gone` and re-marked. Safe but
   slow (~6h extra wall time on a 100K-message backlog).

List backups:
```sh
ssh servarr 'ls -la /mnt/user/discord-wipe/state/state.json.corrupt-*'
```

Forensic inspection of a backup (raw bytes, may not be valid JSON):
```sh
ssh servarr 'hexdump -C /mnt/user/discord-wipe/state/state.json.corrupt-* | head -20'
```

If the backup is *mostly* valid JSON (e.g. truncated mid-array),
recover the deleted IDs manually before restart:
```sh
ssh servarr 'cat /mnt/user/discord-wipe/state/state.json.corrupt-XXX' \
    | sed -E 's/,?$//' \
    | jq -R 'fromjson? // empty' \
    | jq -s '{deleted: (.[0].deleted // []), export_consumed: (.[0].export_consumed // false)}' \
    > /tmp/recovered.json
# Inspect, then push back: scp /tmp/recovered.json servarr:/mnt/user/discord-wipe/state/state.json
```

For live monitoring during the long initial backfill, the composer
dashboard at `composer.erfi.io` has a streaming logs view that
includes the new grand-total / ETA progress lines.

## Customizing paths via DISCORD_WIPE_DATA_DIR

By default the export + state bind-mounts live under
`/mnt/user/discord-wipe/`. To relocate (non-Unraid hosts, alternate
storage pool, etc), set `DISCORD_WIPE_DATA_DIR` in `.env`:

```sh
DISCORD_WIPE_DATA_DIR=/srv/discord-wipe
```

The compose file expands the variable for both bind mounts:

```yaml
volumes:
  - ${DISCORD_WIPE_DATA_DIR:-/mnt/user/discord-wipe}/export:/data/export:ro
  - ${DISCORD_WIPE_DATA_DIR:-/mnt/user/discord-wipe}/state:/data/state
```

Whatever path you set must already exist with the right ownership
(`99:100` on Unraid; whatever your container user maps to elsewhere)
before `docker compose up -d` — Docker creates missing bind-mount
paths but as root, which the unprivileged container can't write to.

## Backup / restore

The state file is small (<10 MB even with 100K IDs as base64 strings).
Back it up with the same R2 backup cron the user runs for everything
else under `/mnt/user/`.

Loss of the state file ≠ disaster. The script will:

- Re-run Phase 1 (since `export_consumed=False`). 404s on already-
  deleted messages count as `gone`, so the pass completes without
  re-deleting anything.
- Re-run Phase 2 normally.

Just slower (every ID gets a redundant HTTP call). Plan for ~6 extra
hours on the next pass and you're fine. The header-driven pacer
still applies, so account-level abuse heuristics stay un-triggered.

This is also why **v0.3.0's State.gc() was so bad**: it silently
produced exactly this "lost state" condition (dropping all old-snowflake
IDs from state on every pass), and the slow-recovery property masked
the fact that the GC was wrong. ~8h of API quota was burned on a live
wipe before the operator noticed the resume counter had reset to 0%.
v0.3.1 reverts the GC and a regression test guards against
reintroduction — see `docs/DESIGN.md` §"What state.deleted is NOT".

## Removing the stack

Via Composer:

```sh
curl -sf -X DELETE -H "X-API-Key: $COMPOSER_API_KEY" \
    "https://composer.erfi.io/api/v1/stacks/discord-wipe"
```

Or directly on servarr:

```sh
ssh servarr 'cd /mnt/user/composer/stacks/discord-wipe && docker compose down'
# Optional, only if you're really done:
ssh servarr 'rm -rf /mnt/user/discord-wipe/state'
```

Stopping the stack leaves your past deletions intact (they're gone
from Discord; the state file only records "what we've done locally").
New messages older than the retention window will accumulate again
until you start the stack back up.
