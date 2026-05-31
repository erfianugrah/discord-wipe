# AGENTS.md — discord-wipe

Context for AI agents working in this repo. Read this top-to-bottom before
making any change — most of it is non-obvious from the code alone.

## What this is

A rolling-retention bulk deleter for **your own** Discord messages. Runs
forever as a Docker container; every pass deletes everything you posted
older than `RETENTION_DAYS` (default 7). One file of Python (stdlib +
`requests`), one Dockerfile, one Compose stack.

Stack files live at `/mnt/user/composer/stacks/discord-wipe/` on `servarr`
(composer-managed git clone); data (export RO, state RW) lives at
`/mnt/user/discord-wipe/`. Image is published to `ghcr.io/erfianugrah/discord-wipe`.
Current version: see `__version__` in `discord_wipe.py` (0.4.0 as of this commit).

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
- **`state.deleted` is NOT garbage-collectable by snowflake age.** This
  was the v0.3.0 footgun (commit `fc1b289`, reverted by `5cbbcb3`).
  The set holds IDs of messages we **just deleted** — and we only
  deleted them because they were OLDER than the retention cutoff.
  Their snowflake timestamps are therefore OLD by definition. Any GC
  of the form "drop IDs older than X" will sweep out the just-deleted
  set; the next pass re-attempts 100% of them against Discord. v0.3.0
  shipped exactly that and burned ~8h of a live wipe's API quota
  before the operator noticed. A `test_state_has_no_snowflake_based_gc_method`
  regression test in `tests/test_discord_wipe.py` fires if anyone
  reintroduces `State.gc()`. The correct shape if growth ever becomes
  a real problem is to track MARK-TIME per ID (when *we* learned about
  the deletion), not the message's own snowflake — that needs a state
  schema change. Until then: do not GC.

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

v0.4.0 added five resilience + observability layers without changing
the core flow: (a) a per-save heartbeat file feeds a docker
HEALTHCHECK so composer's dashboard shows real liveness; (b) a
`StateUnwritableError` park path catches FS-full / mount-frozen /
perms-wrong instead of crashing into a restart loop; (c) a
restart-burst counter persisted in `state.json` parks the daemon if
it's been restarted >5 times in <10 minutes (broken `:main` image
guard); (d) a Prometheus `/metrics` endpoint on :9090 (localhost-mapped
by compose); (e) an opt-in `NTFY_URL` webhook that fires on every
park event. None of these touch the delete pipeline; all are
bypassable via env vars.

## Repo layout

```
discord-wipe/
├── AGENTS.md              this file
├── README.md              user-facing overview + quick deploy
├── discord_wipe.py        the single-file script (stdlib + requests, ~990 lines)
├── Dockerfile             python:3.12-slim, non-root, PUID=99/PGID=100
├── compose.yaml           the Compose stack (ghcr image; build is fallback)
├── .env.example           token template; never commit a real .env
├── .gitignore             blocks .env, state/, export/
├── .dockerignore          keeps build context lean
├── pyproject.toml         ruff config + project metadata
├── tests/
│   ├── __init__.py
│   └── test_discord_wipe.py  stdlib unittest, 33 tests across 14 classes:
│                             3 safety mandate (only-my-messages defence-
│                             in-depth) + 11 regression (one per fixed bug,
│                             plus the v0.3.0 anti-GC guards) + 16 v0.4.0
│                             feature tests (heartbeat, restart-burst,
│                             state-unwritable, notify-on-park, metrics,
│                             status subcommand)
├── docs/
│   ├── DESIGN.md          design rationale + alternatives considered
│   ├── OPERATIONS.md      runbook (deploy, rotate, debug, backfill, recovery)
│   └── TOKEN.md           token lifecycle, 401 behaviour, rotation
└── .github/workflows/
    ├── ci.yml             ruff + py_compile + unit tests + docker build smoke
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
and are now defended against. Grep `discord_wipe.py` for these
behaviours and don't regress (line numbers drift; behaviours don't):

- **Search-no-progress guard** in catchup. In dry-run the same search
  page keeps returning because nothing is deleted; the guard breaks
  out when every hit on a page is already in `state.deleted`. Same
  code defends real runs against a stale search index re-serving
  deleted IDs. Grep `new_in_page`.
- **`state.export_consumed` is NOT flipped on dry-run** — otherwise a
  follow-up real run would skip the export entirely. Grep
  `if not STOP and not cfg.dry_run:`.
- **Dry-run still calls `state.mark(mid)`** so catchup doesn't
  double-count export IDs. Grep `if cfg.dry_run:` in `phase_export`.

## Tests

13 stdlib `unittest` tests under `tests/`. Run with:

```sh
python3 -m unittest discover -s tests -v
```

Categories:

- **Safety mandate** (3 tests, AGENTS.md "only-my-messages" rule): export
  reads only `c<id>/messages.json` per channel; catchup `search_messages`
  always passes `author_id=self`; `delete_message` returns `forbidden`
  on 403 without retry. Re-run these before merging any change that
  touches the delete pipeline.
- **Regression** (8 tests, one per v0.3.0/v0.3.1 fix): SIGTERM-during-
  retry must not mark (export + catchup), ZeroDivisionError on empty
  export, catchup pacing uses `max()` not sum, corrupt state.json is
  backed up before reset, corrupt `messages.json` doesn't crash the
  pass, `messages.json` parsed once per channel per pass, pre-flight
  bails on identity change.
- **Anti-GC guard** (2 tests, the v0.3.0 lesson): IDs with old
  snowflake timestamps survive a save/load round-trip, AND
  `hasattr(dw.State, "gc")` must remain False. The second test
  catches anyone reintroducing the footgun.

Mocks live at the helper-function boundary (`delete_message`,
`search_messages`, `get_me`, `list_my_guilds`, `list_my_dms`) so
tests exercise the real `phase_export` / `phase_live_catchup` control
flow without touching Discord.

## Commands (production on servarr)

Once published to `ghcr.io/erfianugrah/discord-wipe`, the deploy is
just `docker compose pull && docker compose up -d`. Token lives in
`/mnt/user/composer/stacks/discord-wipe/.env`. Full runbook in
`docs/OPERATIONS.md`.

## Image pipeline

- `main` push → `.github/workflows/release.yml` builds amd64+arm64
  image and pushes to `ghcr.io/erfianugrah/discord-wipe:main` and
  `:sha-<short>`.
- `v*` tag push → same workflow also tags `:v1.2.3`, `:1.2`, `:1`, and
  `:latest`.
- Composer is meant to subscribe to the `:main` tag via its webhook
  listener and pull + redeploy on push. **In practice this has been
  flaky** for this stack (verified 2026-05-28 against v0.3.0 + v0.3.1):
  the new tag landed on ghcr but composer didn't auto-pull either time.
  Manual redeploy workaround:
  ```sh
  ssh servarr 'docker exec composer sh -c "cd /opt/stacks/discord-wipe && docker compose pull && docker compose up -d"'
  ```
  Note the in-container stack path is `/opt/stacks/discord-wipe/`,
  NOT the host's `/mnt/user/composer/stacks/discord-wipe/` — composer
  runs `docker compose` from inside its own container.

Verify a build via `gh run list --workflow=release.yml` or
`oci_tags ghcr.io/erfianugrah/discord-wipe`. Verify the running image
is the latest commit:

```sh
ssh servarr 'docker inspect discord-wipe --format "{{.Created}} {{.Image}}"'
# Compare {{.Created}} to the time of your push.
```

## CI hygiene rules

- Every PR must pass `.github/workflows/ci.yml` (ruff check + ruff
  format --check + py_compile + `unittest discover -s tests` +
  docker build smoke).
- Bump `__version__` in `discord_wipe.py` for any behaviour change.
  Tag releases as `v<major>.<minor>.<patch>` from `main` only. Also
  bump `pyproject.toml`'s `version =` to match.
- When adding a regression test for a fixed bug, name the test class
  `BugN_<concise-description>` and reference the commit SHA in the
  docstring so future `grep Bug` surfaces the trail.
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
   ~990 lines, self-contained.
2. Test patterns + safety-mandate coverage →
   `read /home/erfi/discord-wipe/tests/test_discord_wipe.py`.
3. Token / 401 / identity-change / rotation behaviour → `docs/TOKEN.md`.
4. Deploy / debug / state-corruption recovery → `docs/OPERATIONS.md`.
5. "Why this design?" + state-machine semantics → `docs/DESIGN.md`.
6. Discord API surface → `docs.erfi.io` has no `discord` source, so
   fall back to `web_research` against `discord.com/developers/docs/...`.
7. Image versions → `oci_tags ghcr.io/erfianugrah/discord-wipe`.
