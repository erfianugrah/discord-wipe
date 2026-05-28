# Operations runbook

Everything you do day-to-day with discord-wipe on `servarr`.

## Deploy paths

- **From source** (one-time bring-up):
  ```sh
  rsync -av --exclude=.env --exclude=state --exclude=__pycache__ \
      ~/discord-wipe/ servarr:/mnt/user/appdata/discord-wipe/
  rsync -av ~/erfi-bot/data/exports/discord/package/Messages/ \
      servarr:/mnt/user/appdata/discord-wipe/export/Messages/
  ssh servarr 'umask 077 && cat > /mnt/user/appdata/discord-wipe/.env' <<EOF
  DISCORD_TOKEN=your-token-here
  EOF
  ssh servarr 'cd /mnt/user/appdata/discord-wipe && docker compose up -d'
  ```
- **From ghcr.io** (steady state):
  ```sh
  ssh servarr 'cd /mnt/user/appdata/discord-wipe && \
      docker compose pull && docker compose up -d'
  ```
- **Via Composer** (GitOps): registered as the `discord-wipe` stack;
  push to `main` → GitHub Actions builds + pushes
  `ghcr.io/erfianugrah/discord-wipe:main` → Composer webhook pulls →
  `docker compose up -d` redeploys. See "Composer wiring" below.

## Day-to-day operations

| What | How |
|---|---|
| Live log | `ssh servarr docker logs -f discord-wipe` |
| Recent errors | `ssh servarr 'docker logs discord-wipe 2>&1 \| rg -i "error\|429\|forbidden\|fatal"'` |
| Deleted count | `ssh servarr 'jq ".deleted \| length" /mnt/user/appdata/discord-wipe/state/state.json'` |
| State summary | `ssh servarr 'jq "{deleted: (.deleted\|length), export_consumed, last_pass_at}" /mnt/user/appdata/discord-wipe/state/state.json'` |
| Pause cleanly | `ssh servarr docker stop discord-wipe` (SIGTERM, state saved) |
| Resume | `ssh servarr docker start discord-wipe` |
| Force pass now | `ssh servarr docker restart discord-wipe` (interrupts current sleep, starts fresh pass) |
| Bump image | `ssh servarr 'cd /mnt/user/appdata/discord-wipe && docker compose pull && docker compose up -d'` |
| Tighten retention | edit `RETENTION_DAYS` in compose.yaml, `docker compose up -d` |
| Skip a guild | add `--exclude-guild <ID>` to the compose `command:` and `docker compose up -d` |
| Skip a DM | add `--exclude-channel <ID>` similarly |
| Rotate token | see `docs/TOKEN.md` |

## First-run expectations

The first pass deletes ~105K messages from the export. At
`DELETE_DELAY=1.0` that's ~29 hours. Container stays healthy throughout
— `docker logs` shows steady progress (one line per channel).

Crash recovery: stop / restart at any point; the state file means we
skip already-done IDs. Network blip mid-pass is harmless.

## Steady state (after backfill)

Every `INTERVAL_HOURS` (default 24):

1. `get_me` pre-flight (catches token rotation).
2. Phase 1 skipped (`export_consumed=True`).
3. Phase 2 sweeps every guild + DM with `messages/search`, deletes
   anything older than `now - RETENTION_DAYS`.
4. Sleep until next pass.

On a typical day you'll see Phase 2 find 0-50 messages per scope and
wrap up in a few minutes.

## Failure modes

### 401 — token rejected

The daemon detects this at the `get_me` pre-flight and parks itself
(see `docs/TOKEN.md`). Logs print a large FATAL banner with rotation
instructions. The container stays alive (no restart-loop hammering)
until you `docker compose up -d` with a new `.env`.

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
   (`/mnt/user/appdata/discord-wipe/.env`, chmod 600) and survives
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
}" /mnt/user/appdata/discord-wipe/state/state.json'
```

If `deleted` is growing every pass and `last_pass_at` is recent (<2 ×
`INTERVAL_HOURS` ago), everything's working. If `deleted` is stuck and
`last_pass_at` is old, the daemon is either parked on 401 or stopped.
`docker logs` will say which.

## Backup / restore

The state file is small (<10 MB even with 100K IDs as base64 strings).
Back it up with the same R2 backup cron the user runs for everything
else under `/mnt/user/appdata/`.

Loss of the state file ≠ disaster. The script will:

- Re-run Phase 1 (since `export_consumed=False`). 404s on already-
  deleted messages count as `gone`, so the pass completes without
  re-deleting anything.
- Re-run Phase 2 normally.

Just slower (every ID gets a redundant HTTP call). Plan for ~6 extra
hours on the next pass and you're fine.

## Removing the stack

```sh
ssh servarr 'cd /mnt/user/appdata/discord-wipe && docker compose down'
# Optional, only if you're really done:
ssh servarr 'rm -rf /mnt/user/appdata/discord-wipe/state'
```

Stopping the stack leaves your past deletions intact (they're gone
from Discord; the state file only records "what we've done locally").
New messages older than the retention window will accumulate again
until you start the stack back up.
