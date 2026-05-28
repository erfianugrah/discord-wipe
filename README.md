# discord-wipe

Rolling-retention deleter for your own Discord messages. Runs as a
long-lived container; every pass it deletes everything you posted older
than `RETENTION_DAYS` (default 7). Sleeps `INTERVAL_HOURS`, repeats.

Two phases per pass:

1. **Export phase** — first run only. Walks your official Discord data
   export (`Messages/c{id}/messages.json`) and deletes every known
   message ID older than the cutoff. Marks the export "consumed" in the
   state file so subsequent passes skip it.
2. **Live catch-up phase** — every pass. Enumerates current guilds via
   `GET /users/@me/guilds` + open DMs via `GET /users/@me/channels` and
   uses `messages/search?author_id=me&max_id=<cutoff_snowflake>` to find
   everything older than the cutoff. Catches stuff sent after the export
   plus any new guilds/DMs since.

State (`state/state.json`) tracks deleted message IDs so crashes,
restarts, and repeat passes never re-attempt the same ID.

## Why this design

- **Export-driven first run is ~3× faster** than search-paginate. Search
  pages need a 30s wait between them so Discord's index can refresh;
  with known IDs we skip that wait entirely. ~105K messages → ~30-50h
  depending on Discord's current per-account bucket tightness.
- **`max_id` snowflake filter** does retention server-side. Recent
  messages are never returned, so we don't waste any calls on them.
- **Header-driven pacing.** Discord publishes `X-RateLimit-Remaining` +
  `Reset-After` on every response; the script paces evenly through the
  remaining quota (`reset / remaining` per delete) and never tips into
  429 territory. `DELETE_DELAY` is the safety floor, not the target.
- **Resumable.** State file tracks every deleted ID. On restart the
  script skips them all without any API call — visible per-channel in
  the logs (e.g. `15/15 already done — skip`).
- **Single command, one mode**: every pass deletes "everything older
  than the cutoff". Backfill and steady-state are the same code path —
  the first pass just happens to find a lot more.

## Repo layout

```
discord-wipe/
├── discord_wipe.py            single-file script (stdlib + requests, ~990 lines)
├── Dockerfile                 python:3.12-slim, non-root, PUID=99/PGID=100
├── compose.yaml               the stack (pulls ghcr.io/erfianugrah/discord-wipe)
├── .env.example               token template
├── .gitignore                 blocks .env / state/ / export/
├── .dockerignore              keeps the image build context lean
├── pyproject.toml             ruff config + project metadata
├── AGENTS.md                  agent-facing project conventions
├── README.md                  this file
├── LICENSE                    MIT
├── tests/                     stdlib unittest, 13 tests
│   └── test_discord_wipe.py   safety mandate + bug regressions + anti-GC guard
├── docs/
│   ├── DESIGN.md              design rationale + alternatives considered
│   ├── OPERATIONS.md          deploy / monitor / debug / rotate / recover
│   └── TOKEN.md               token lifecycle (no refresh; rotation procedure)
└── .github/workflows/
    ├── ci.yml                 ruff + py_compile + unit tests + docker build smoke
    └── release.yml            multi-arch ghcr.io image build on main + tags
```

**Bind-mount paths** are configurable via `DISCORD_WIPE_DATA_DIR`
(default `/mnt/user/discord-wipe`). At runtime that holds:

```
/mnt/user/discord-wipe/
├── export/Messages/    bind-mounted RO from your data export ZIP
└── state/state.json    bind-mounted RW; resume state
```

The stack itself lives at `/mnt/user/composer/stacks/discord-wipe/`
when managed by Composer (the user's GitOps platform), or wherever you
`git clone` it standalone.

## Get your Discord data export

User Settings → Privacy & Safety → **Request All My Data** →
"Messages" checked. Discord emails you a ZIP within a few hours, max
~30 days for big accounts. Inside:

```
package/Messages/index.json          # {channel_id: display_name}
package/Messages/c{channel_id}/
   ├── channel.json                  # {id, type, recipients}
   └── messages.json                 # [{ID, Timestamp, Contents, Attachments}]
```

The script reads `package/Messages/` directly (mounted at
`/data/export/Messages` in the container).

## Local sanity checks (before deploying)

The script has a `uv run` shebang so you can run it from any host with
`uv` installed without setting up a venv:

```bash
# Verify token works.
export DISCORD_TOKEN='your-token-here'
./discord_wipe.py verify

# Inspect what's in the export + what guilds/DMs are live.
./discord_wipe.py discover \
   --export-dir ~/erfi-bot/data/exports/discord/package/Messages

# Dry-run one full pass: NO deletes, just logging.
./discord_wipe.py run --dry-run \
   --export-dir ~/erfi-bot/data/exports/discord/package/Messages \
   --state ./state/state.json \
   --retention-days 7

# Run the test suite (no network; mocks every Discord helper).
python3 -m unittest discover -s tests -v
```

## Deploy

### Via Composer (GitOps)

The production path. Composer at `composer.erfi.io` watches `main`
and auto-redeploys on every push:

```bash
export COMPOSER_API_KEY=...   # from Bitwarden
export BASE=https://composer.erfi.io/api/v1

# One-time: register the stack.
curl -sf -X POST -H "X-API-Key: $COMPOSER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "discord-wipe",
    "repo_url": "git@github.com:erfianugrah/discord-wipe.git",
    "branch": "main",
    "compose_path": "compose.yaml",
    "env_path": ".env",
    "auth_method": "none"
  }' "$BASE/stacks/git"

# One-time: drop the token + sync the export. (.env must be chmod 600
# and owned by nobody:users for composer's container to read it.)
ssh servarr 'umask 077 && cat > /mnt/user/composer/stacks/discord-wipe/.env <<EOF
DISCORD_TOKEN=your-token-here
DISCORD_WIPE_TAG=main
TZ=Europe/Amsterdam
EOF
chown nobody:users /mnt/user/composer/stacks/discord-wipe/.env'

ssh servarr 'mkdir -p /mnt/user/discord-wipe/{export,state} && \
  chown -R 99:100 /mnt/user/discord-wipe'
rsync -a ~/erfi-bot/data/exports/discord/package/Messages/ \
    servarr:/mnt/user/discord-wipe/export/Messages/

# Bring up.
curl -sf -X POST -H "X-API-Key: $COMPOSER_API_KEY" \
    "$BASE/stacks/discord-wipe/up?async=true"
```

After that, every `git push origin main` triggers the
`release.yml` workflow on GitHub Actions → image goes to
`ghcr.io/erfianugrah/discord-wipe:main` → Composer's webhook pulls
and redeploys.

### Standalone (no Composer)

```bash
git clone git@github.com:erfianugrah/discord-wipe.git
cd discord-wipe

echo "DISCORD_TOKEN=your-token-here" > .env
chmod 600 .env

mkdir -p /mnt/user/discord-wipe/{export,state}
rsync -a ~/erfi-bot/data/exports/discord/package/Messages/ \
    /mnt/user/discord-wipe/export/Messages/

docker compose pull && docker compose up -d
docker logs -f discord-wipe
```

## Operator runbook

| What | How |
|---|---|
| Status / live log | `ssh servarr docker logs -f discord-wipe` |
| Count deleted so far | `ssh servarr 'jq ".deleted \| length, .export_consumed, .last_pass_at" /mnt/user/discord-wipe/state/state.json'` |
| Pause cleanly | `ssh servarr docker stop discord-wipe` (state is saved on SIGTERM) |
| Resume | `ssh servarr docker start discord-wipe` |
| Force re-run export phase | stop, edit `state/state.json`, set `"export_consumed": false`, start |
| Skip a guild/DM | edit compose.yaml `command:` to add `--exclude-guild ID` or `--exclude-channel ID`, `docker compose up -d` |
| Tighten/loosen retention | bump `RETENTION_DAYS` in compose.yaml, `docker compose up -d` |
| Bump speed | drop `DELETE_DELAY` toward 0.3s. Header-driven pacer handles per-bucket throttling automatically; the floor mostly defends against account-level abuse heuristics. |
| Filter logs | `docker logs discord-wipe 2>&1 \| rg -i 'error\|429\|forbidden\|fatal\|terminal 400'` |
| Pin a specific build | edit `.env` on servarr, set `DISCORD_WIPE_TAG=v1.2.3` (or any `sha-<short>`), `docker compose up -d` |

## Failure modes / rate limits

- **HTTP 429** — handled. Script reads `retry_after` from the body (or
  `Retry-After` header) and sleeps. Header-driven pacing means routine
  429s are now rare; if they're sustained, Discord has tightened the
  per-account bucket and the pacer is doing its job by absorbing them.
  Persistent multi-second `Retry-After` values mean the account has
  been flagged — stop, wait a day, restart.
- **HTTP 400 with a Discord code** — semantic error, terminal.
  Documented codes the script treats as `forbidden`:
  `50083` (thread archived), `50001` (missing access),
  `50021` (system message), `50034` (message too old),
  `160005` (thread locked). Logged once each, marked done, skipped.
- **HTTP 403** — message is not yours or is a system message
  (call/pin/join). Counted as `forbidden`, marked done, moves on.
- **HTTP 404** — already deleted by you elsewhere or by Discord.
  Counted as `gone`, treated as success.
- **HTTP 401** — token rotated by Discord. The daemon prints a FATAL
  banner with rotation steps and parks itself (no restart-loop
  hammering the API). See `docs/TOKEN.md` for the full procedure.
- **Identity-change** (v0.3.1+) — same banner shape as 401, fires when
  the per-pass pre-flight `/users/@me` returns a different `id` than
  the one cached at startup (token was swapped to a different
  account). Same resolution: edit `.env` back, `docker compose up -d`.
- **Corrupt state.json** (v0.3.1+) — if the script can't parse
  `state/state.json` on load it renames it to `state.json.corrupt-<ts>`,
  logs a WARN, and resumes with empty state. Recovery happens naturally
  as Discord returns 404 (`gone`) for already-deleted IDs. Plan ~6h
  extra wall time on the next pass. See `docs/OPERATIONS.md` for
  forensic recovery of the backup file.
- **Discord API changes** — the search endpoint occasionally moves
  fields around. If you start seeing `unexpected 4xx` logs, check
  upstream tools (victornpb/undiscord, OrbitalCheese/undiscord-lite)
  for what shape changed.

## ToS note

Automating a user account ("self-botting") technically violates Discord
ToS. Bans for personal-history cleanup are rare in practice but
possible. The default delays are conservative for that reason —
Discord's abuse heuristics flag *speed* much more than *volume*. Don't
tune the delays toward zero.

You can only delete your own messages; this is an API-level
restriction, not a script limitation.

## What gets logged (and why)

Every pass writes a progress line every 10 deletes:

```
[export] resume: 4521/105327 (4.3%) already done across 6/194 fully-done channels; 100806 targets remaining
[export 5/194] DM         Direct Message with friend-a#0                    2314 to delete (203/2517 already done)
    10/2314 ok=10 gone=0 403=0 | total: 4531/105327 (4.3%) ~36/min ETA 48h59m
```

- `resume:` line shows what was already done before this pass started —
  so the resume is auditable.
- `already done — skip` lines confirm that channels with all-deleted
  messages were skipped without any API calls.
- The per-channel `N/M` counter and the grand-total `X/Y (Z%) ETA` are
  printed together so you always know both the channel progress and
  the overall progress.
- Sub-1s rate-limit retries are suppressed by design — the
  header-driven pacer is absorbing them and there's nothing to act on.
  `>=1s` retries and 5xx errors are still logged.

## Documentation

- [`AGENTS.md`](AGENTS.md) — conventions for AI agents working in this
  repo (safety rules, architecture, tool routing).
- [`docs/DESIGN.md`](docs/DESIGN.md) — why the script looks the way it
  does. Alternatives considered, rate-limit calibration, state machine.
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — deploy, monitor, debug,
  rotate, backup, remove. Composer wiring details.
- [`docs/TOKEN.md`](docs/TOKEN.md) — token lifecycle. Why there's no
  refresh flow, what invalidates a token, how to rotate cleanly.

## License

[MIT](LICENSE). Personal tool. Do whatever you want with it.
