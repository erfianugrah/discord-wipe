# AGENTS.md — discord-wipe

Context for AI agents working in this repo. Read this top-to-bottom before
making any change — most of it is non-obvious from the code alone.

## What this is

A rolling-retention bulk deleter for **your own** Discord messages. Runs
forever as a Docker container; every pass deletes everything you posted
older than `RETENTION_DAYS` (default 7). One file of Python (stdlib +
`requests`), one Dockerfile, one Compose stack.

Lives at `/mnt/user/appdata/discord-wipe/` on `servarr` in production.
Image is published to `ghcr.io/erfianugrah/discord-wipe`.

## Hard safety rules (read these or break things)

- **Self-bot.** Automating a user account technically violates Discord
  ToS. `DELETE_DELAY` (default `1.0s`) is the **safety floor**, not the
  target pace — the real pace is set by `X-RateLimit-Reset-After /
  X-RateLimit-Remaining` (header-driven, see `delete_message`). The
  floor exists to defend against Discord's account-level abuse
  heuristics which are SEPARATE from per-route buckets and watch
  overall request frequency. **Do not lower it below 0.3s** without
  re-doing that math. Discord flags *speed* much more than *volume*.
- **Only-my-messages is a load-bearing property.** The user may be a
  server admin in many guilds, which gives the API permission to delete
  anyone's messages. The script must never enumerate "all messages in
  channel X" — only paths that target messages where `author_id == me`.
  Three layers of defence (in `discord_wipe.py`):
  1. Export phase reads `c{id}/messages.json` which by definition
     contains only the requester's messages.
  2. Live phase queries `messages/search?author_id=<self>&max_id=…`
     so Discord server-side-filters.
  3. A 403 on DELETE is treated as `forbidden` and skipped (never
     retried) — defence in depth in case ① or ② regresses.
  Any change that adds a code path producing message IDs to delete
  MUST keep all three layers intact. Add a unit test if you touch this.
- **Token is in `.env`, never committed.** `.gitignore` blocks `.env`,
  `state/`, and `export/`. Never paste a token into a file the agent
  writes — pi's `edit_dotenv` tool-guard exists for this reason.
- **No refresh flow.** Discord user tokens are static; on 401 the
  daemon parks itself (`_auth_paused_exit`) and waits for a manual
  rotation via `.env` + `docker compose up -d`. See `docs/TOKEN.md`.

## Architecture in one paragraph

`discord_wipe.py run --watch` loops forever. Each pass: (1) **export
phase** reads the official Discord data export from a read-only bind
mount and deletes every message-ID whose timestamp predates `now -
RETENTION_DAYS`, then sets `export_consumed=True` in the state file so
future passes skip it; (2) **live catchup phase** enumerates current
guilds (`GET /users/@me/guilds`) + open DMs (`GET /users/@me/channels`)
and queries `messages/search?author_id=me&max_id=<cutoff_snowflake>` on
each scope, deleting every hit. Cutoff is encoded as a Discord snowflake
(`(unix_ms - 1420070400000) << 22`) so retention is filtered
server-side. State (`state/state.json`) persists deleted IDs so a
crash mid-pass doesn't re-attempt anything — the resume is auditable
from logs via `[export] resume:` and per-channel `N/N already done —
skip` lines. Pacing is header-driven: `max(DELETE_DELAY, Reset-After /
Remaining)`, so 429s are rare in steady state and ETA adapts to
whatever Discord's bucket is currently advertising.

## Repo layout

```
discord-wipe/
├── AGENTS.md              this file
├── README.md              user-facing overview + quick deploy
├── discord_wipe.py        the single-file script (stdlib + requests)
├── Dockerfile             python:3.12-slim, non-root, PUID=99/PGID=100
├── compose.yaml           the Compose stack (ghcr image; build is fallback)
├── .env.example           token template; never commit a real .env
├── .gitignore             blocks .env, state/, export/
├── .dockerignore          keeps build context lean
├── docs/
│   ├── DESIGN.md          design rationale + alternatives considered
│   ├── OPERATIONS.md      runbook (deploy, rotate, debug, backfill)
│   └── TOKEN.md           token lifecycle, 401 behaviour, rotation
└── .github/workflows/
    ├── ci.yml             lint + py_compile on push/PR
    └── release.yml        multi-arch ghcr.io image on main + tags
```

At runtime on servarr:

- **Stack files** live at `/mnt/user/composer/stacks/discord-wipe/`
  (composer-managed git clone of this repo) — `.env` lives here too
  (chmod 600, `nobody:users`, never committed).
- **Bind-mount data** lives at `/mnt/user/discord-wipe/` (default,
  override via `DISCORD_WIPE_DATA_DIR`):
  ```
  ├── export/Messages/       bind-mounted RO from data export ZIP
  └── state/state.json       bind-mounted RW; resume state
  ```
  Owned by `99:100` (Unraid `nobody:users`) to match the container
  user. Located OUTSIDE composer's stacks dir so it survives `git pull`
  and isn't blown away on stack re-clone.

Why the split? Composer runs the stack via `docker compose` from
**inside** the composer container, so relative paths like `./export`
resolve to composer's container view (`/opt/stacks/discord-wipe/`) and
silently miss the host filesystem. Absolute host paths via the env var
fix that and match every other stack on this homelab
(bonkled → `/mnt/user/bonkled/`, atuin → `/mnt/user/data/atuin/`, etc.).

## Commands (host dev box)

```sh
# Syntax check.
python3 -m py_compile discord_wipe.py

# Token check (in-memory only; never write to a file).
DISCORD_TOKEN=... python3 discord_wipe.py verify

# Inventory.
DISCORD_TOKEN=... python3 discord_wipe.py discover \
    --export-dir ~/erfi-bot/data/exports/discord/package/Messages

# Full dry-run (NO deletes; uses a SEPARATE state file to avoid
# poisoning the production state).
mkdir -p state-dryrun
DISCORD_TOKEN=... python3 discord_wipe.py run --dry-run \
    --export-dir ~/erfi-bot/data/exports/discord/package/Messages \
    --state ./state-dryrun/state.json \
    --retention-days 7 \
    --search-delay 6
```

Dry-runs DO hit the live API for read operations (`/users/@me`,
`/guilds`, `/channels`, `messages/search`) — they just skip the
`DELETE` calls. So a dry-run still proves out auth + permission +
pagination behaviour against production.

Three dry-run-specific correctness bugs landed in the first commits
and are now defended against, so if you touch the dry-run paths,
grep for these comments and don't regress:

- Search-no-progress guard in catchup (line ~634). In dry-run the
  same search page keeps returning because nothing is deleted; the
  guard breaks out when every hit on a page is already in
  `state.deleted`. Same code defends real runs against a stale
  search index re-serving deleted IDs.
- `state.export_consumed` is NOT flipped on dry-run (line ~498) —
  otherwise a follow-up real run would skip the export entirely.
- Dry-run still calls `state.mark(mid)` (line ~474) so catchup
  doesn't double-count export IDs.

## Commands (production on servarr)

Once published to `ghcr.io/erfianugrah/discord-wipe`, the deploy is
just `docker compose pull && docker compose up -d`. Token lives in
`/mnt/user/appdata/discord-wipe/.env`. Full runbook in
`docs/OPERATIONS.md`.

## Image pipeline

- `main` push → `.github/workflows/release.yml` builds amd64+arm64
  image and pushes to `ghcr.io/erfianugrah/discord-wipe:main` and
  `:sha-<short>`.
- `v*` tag push → same workflow also tags `:v1.2.3`, `:1.2`, `:1`, and
  `:latest`.
- Composer subscribes to the `:main` tag via its webhook listener and
  pulls + redeploys on push. See `docs/OPERATIONS.md` for the wiring.

Verify a build via `gh run list --workflow=release.yml` or
`oci_tags ghcr.io/erfianugrah/discord-wipe`. The image must build
before composer can `docker compose pull` it — if the deploy job
finishes "successfully" but the running image SHA hasn't changed,
the build either hasn't landed yet or the `:main` tag is being
overwritten by an in-flight workflow.

## CI hygiene rules

- Every PR must pass `.github/workflows/ci.yml` (py_compile + ruff +
  docker build smoke).
- Bump `__version__` in `discord_wipe.py` for any behaviour change.
  Tag releases as `v<major>.<minor>.<patch>` from `main` only.
- Never commit a file that contains a Discord token, even a fake one
  that looks real (Discord's secret-scanner will revoke it).
- The `.env.example` template uses `replace-me` as the placeholder.
- Token fingerprints in committed docs use generic placeholders
  (`AAAAAAAA...zzzz`) — the script logs real first-6/last-4 chars at
  runtime so an operator can disambiguate during rotation, but those
  fragments must never reach git.
- Use `ruff check --fix && ruff format` before committing. The
  pyproject.toml selects `E F B I SIM RUF W` and ignores `E501`.

## When the LLM should ask vs proceed

- **Proceed:** code changes inside `discord_wipe.py`, doc edits,
  workflow tweaks, dry-runs against the live API, dockerfile changes.
- **Ask first:** any real `run` (not `--dry-run`) against production,
  changing the safety-defence-in-depth logic in §"Hard safety rules",
  bumping `DELETE_DELAY` below 0.5s, removing `--watch`, removing
  state-file persistence.

## Tool-routing for discord-wipe questions

1. Source-of-truth code → `read /home/erfi/discord-wipe/discord_wipe.py`.
   It's ~750 lines and self-contained.
2. Token / 401 / rotation behaviour → `docs/TOKEN.md`.
3. Deploy / debug → `docs/OPERATIONS.md`.
4. "Why this design?" → `docs/DESIGN.md`.
5. Discord API surface → `docs.erfi.io` has no `discord` source, so
   fall back to `web_research` against `discord.com/developers/docs/...`.
6. Image versions → `oci_tags ghcr.io/erfianugrah/discord-wipe`.
