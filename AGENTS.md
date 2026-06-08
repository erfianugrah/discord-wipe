# AGENTS.md — discord-wipe

Context for AI agents working in this repo. Read this top-to-bottom before
making any change — most of it is non-obvious from the code alone.

## What this is

A rolling-retention bulk deleter for **your own** Discord messages. Runs
forever as a Docker container; every pass deletes everything you posted
older than `RETENTION_DAYS` (default 14). One file of Python (stdlib +
`requests`), one Dockerfile, one Compose stack.

Stack files live at `/mnt/user/composer/stacks/discord-wipe/` on `servarr`
(composer-managed git clone); data (export RO, state RW) lives at
`/mnt/user/discord-wipe/`. Image is published to `ghcr.io/erfianugrah/discord-wipe`.
Current version: see `__version__` in `discord_wipe.py` (0.4.3 as of this commit).

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

v0.4.1 fixed a restart-burst false-fire: a transient DNS failure at
startup (`discord.com` not yet resolvable on host reboot, before the
Docker network / upstream resolver is ready) crashed `get_me()` with an
uncaught `ConnectionError`; `restart: unless-stopped` respawned it; six
crashes inside the 600s window tripped the restart-burst guard and
PARKED the daemon on a self-healing 30-second blip (observed live
2026-06-04). Fix: `_request()` wraps every session call with bounded
exponential-backoff retry on connection-level errors (DNS / reset /
timeout) — `NET_RETRY_MAX`/`_BASE`/`_CAP` env-tunable, ~2min ride-out
per call by default; HTTP responses of any status pass through untouched
so the 401/429/403 semantics are preserved. The pass loop also catches
`ConnectionError`/`Timeout` so a sustained outage ends the pass cleanly
instead of crashing, and a successful auth now resets `restart_burst` to
0 (auth proves none of the crash-loop causes apply). Tests:
`Bug10_TransientNetworkErrorIsRetriedNotFatal` (4) +
`Bug11_SuccessfulAuthClearsRestartBurst` (1).

v0.4.3 fixed the single worst failure mode this project has had: a
**0-byte `state.json` truncation** that silently erased a *completed*
wipe and forced a from-scratch re-grind. Observed live 2026-06-08: the
daemon logged `(107025 IDs already done; export_consumed=True)`, then
`state.json` went to 0 bytes, `_load()` saw `Expecting value: line 1
column 1 (char 0)`, reset to empty, and re-issued DELETE on ~105k
already-deleted messages — all returning `404 gone`, `ok=0`, paced at
~16/min by Discord's punishing **old-message DELETE rate limit** (every
single-message delete of a >14-day-old message hits a separate, much
stricter bucket; this is a documented Discord behaviour, not a bug in
this tool). Six 0-byte `state.json.corrupt-*` backups across 8 days were
the fingerprint. Root cause: `state.save()` did `write_text()` +
`rename()` with **no `fsync`**, which is not crash-safe on Unraid's
`/mnt/user` shfs FUSE overlay — the rename metadata journals but the
data pages may never flush when the container is SIGKILLed (stop-grace
overrun / host reboot / OOM) inside the writeback window. Fix:
`save()` now `fsync`s the temp file before the rename, rotates the
previous good file to `state.json.bak`, and `fsync`s the parent
directory after; `_load()` falls back to `.bak` when `state.json` is
missing/empty/corrupt, so a torn write loses at most the last ~10
deletes instead of the whole set. The relentless rate-limiting in the
logs was a *symptom* — the real defect was re-deleting messages that
were already gone. Tests: `Bug12_StateSurvivesZeroByteTruncation` (3).

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
│   └── test_discord_wipe.py  stdlib unittest, 44 tests across 23 classes:
│                             3 safety mandate (only-my-messages defence-
│                             in-depth) + regression (one per fixed bug,
│                             plus the v0.3.0 anti-GC guards, the v0.4.1
│                             transient-network + burst-reset fixes, and the
│                             v0.4.3 Bug12 0-byte-state-durability guards) +
│                             v0.4.0 feature tests (heartbeat, restart-burst,
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
    --retention-days 14 \
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
- Composer git-syncs this stack from the repo (`auto_sync: true`,
  remote `git@github.com:erfianugrah/discord-wipe.git`, SSH via the
  encrypted `id_gh_wsl` key at `/home/composer/.ssh/`). On a healthy
  clone, a `main` push auto-pulls `compose.yaml`/`.env` and redeploys.

- **2026-05-28 → 2026-06-08 this was stuck, and the cause was NOT
  "flaky composer" / a broken SSH key** (an earlier note here blamed
  both — wrong). The clone's local `main` was pinned to an ORPHANED
  commit (`25d5197`) left behind by a history rewrite, so it was not
  an ancestor of `origin/main`. composer's go-git pull only
  fast-forwards, so every sync errored with `non-fast-forward update`
  and the clone never advanced — which is why every deploy back then
  needed a manual redeploy. SSH auth was fine the whole time.

- **Diagnosing composer git-sync** (do NOT test as `root` — composer
  runs as the `composer` user, and its key is encrypted at rest so raw
  `ssh -T git@github.com` / `git fetch` always fail regardless of
  user; that proves nothing). Ask composer instead:
  ```sh
  curl -s -H "X-API-Key: $COMPOSER_API_KEY" \
    https://composer.erfi.io/api/v1/stacks/discord-wipe/git/status | jq
  # sync_status: "synced" = healthy; "error" = read the message
  ```

- **Fix for a diverged / non-fast-forward clone** — reconcile to the
  already-fetched origin ref (no network needed; discards orphaned
  local commits + dirty tracked files, both superseded by origin;
  untracked `.env` is preserved). **Run git AS the `composer` user
  (uid 99), never as host root** — a root-run `git reset` rewrites
  `.git/refs/heads/main` + the checked-out files as root-owned, and
  then composer (uid 99) can't update the ref, so the next sync dies
  with `open .git/refs/heads/main: permission denied`. Do it inside the
  container instead, then re-sync via the API:
  ```sh
  ssh servarr 'docker exec -u composer composer sh -c "\
    git config --global --add safe.directory /opt/stacks/discord-wipe; \
    git -C /opt/stacks/discord-wipe reset --hard refs/remotes/origin/main"'
  curl -s -X POST -H "X-API-Key: $COMPOSER_API_KEY" \
    https://composer.erfi.io/api/v1/stacks/discord-wipe/sync   # "Git pull + detect changes"
  ```
  (If you already ran it as root and hit the perms error, repair with
  `ssh servarr 'find /mnt/user/composer/stacks/discord-wipe -uid 0 -exec chown 99:101 {} +'`
  then re-sync.)

- **Manual image-only redeploy** (when you just want the latest `:main`
  image without touching git) — prefer the composer API, which injects
  env + recreates: `POST /api/v1/stacks/discord-wipe/{pull,up}` (see the
  `.env` footgun below before running `up`). The lower-level path is
  `docker exec composer sh -c "cd /opt/stacks/discord-wipe && docker
  compose pull && docker compose up -d"` — note the in-container stack
  path is `/opt/stacks/discord-wipe/`, NOT the host's
  `/mnt/user/composer/stacks/discord-wipe/`, because composer runs
  `docker compose` from inside its own container.

- **The `.env` is load-bearing AND fragile — restore it server-side, not
  from your context.** `compose.yaml` declares `env_file: .env`, and
  composer stores NO env vars for this stack (`GET /api/v1/stacks/
  discord-wipe` → `has_env: 0`). So the on-disk `.env` (just
  `DISCORD_TOKEN=...`) is the ONLY source of the token. It is gitignored,
  so a clone re-sync / re-clone during a recovery event can delete it —
  after which EVERY composer `up` (and the lower-level `docker compose
  up`) 500s with `env file /opt/stacks/discord-wipe/.env not found`,
  even though the running container keeps working (its env is baked in at
  create time and survives `docker start`). Observed 2026-06-08. Recover
  WITHOUT the token entering the agent's context by piping it out of the
  still-running container into the file on the host:
  ```sh
  ssh servarr 'ENV=/mnt/user/composer/stacks/discord-wipe/.env; \
    docker exec discord-wipe printenv DISCORD_TOKEN \
      | { IFS= read -r t; printf "DISCORD_TOKEN=%s\n" "$t"; } > "$ENV"; \
    chmod 600 "$ENV"; chown 99:101 "$ENV"'   # owner 99:101 matches the clone
  ```
  Verify without printing the secret: `wc -l`, `head -c 14` (shows the
  `DISCORD_TOKEN=` prefix only), `stat -c %s` (~85 bytes for a valid
  user token), and a `grep -q replace-me` placeholder check.

- **Recover a state-loss without a 4-day re-grind:** `seed-from-export`
  (v0.4.3+) marks every export message older than the cutoff as deleted
  and sets `export_consumed=True`, so the daemon skips re-issuing DELETE
  on already-gone messages. Token-less, no API calls. Stop the container
  first so the save isn't raced, run it as a one-off against the same
  mounts, then bring the daemon back:
  ```sh
  ssh servarr 'docker stop discord-wipe; \
    docker run --rm \
      -v /mnt/user/discord-wipe/export:/data/export:ro \
      -v /mnt/user/discord-wipe/state:/data/state \
      ghcr.io/erfianugrah/discord-wipe:main \
      seed-from-export --retention-days 14'
  # then composer up (after the .env exists) to start the daemon on the seed
  ```
  Only messages OLDER than the cutoff are seeded; recent ones stay
  unmarked so live catchup still deletes them when they age out. It does
  NOT verify the messages are gone — only run it when a prior pass is
  known to have completed the wipe.

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
