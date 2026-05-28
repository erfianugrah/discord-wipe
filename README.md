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
  with known IDs we skip that wait entirely. ~105K messages → ~29h via
  export pass at the default 1s delete delay.
- **`max_id` snowflake filter** does retention server-side. Recent
  messages are never returned, so we don't waste any calls on them.
- **Single command, one mode**: every pass deletes "everything older
  than the cutoff". Backfill and steady-state are the same code path —
  the first pass just happens to find a lot more.

## Repo layout

```
discord-wipe/
├── discord_wipe.py     # the script (single-file, stdlib + requests)
├── Dockerfile          # python:3.12-slim + requests
├── compose.yaml        # the stack definition
├── .env.example        # copy to .env, fill in DISCORD_TOKEN
├── .gitignore          # blocks .env / state/ / export/
└── README.md           # this file
```

At runtime (on servarr), the project lives at
`/mnt/user/appdata/discord-wipe/` and gets two extra dirs:

```
├── export/Messages/    # bind-mounted RO from your data export ZIP
└── state/state.json    # bind-mounted RW; resume state
```

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
```

## Deploy on servarr

```bash
# 1. Copy the project + export across.
rsync -av --exclude=.env --exclude=state ~/discord-wipe/ \
    servarr:/mnt/user/appdata/discord-wipe/
rsync -av ~/erfi-bot/data/exports/discord/package/Messages/ \
    servarr:/mnt/user/appdata/discord-wipe/export/Messages/

# 2. Drop the token in .env on servarr (chmod 600).
ssh servarr 'umask 077 && cat > /mnt/user/appdata/discord-wipe/.env' <<EOF
DISCORD_TOKEN=your-token-here
EOF

# 3. Build + start.
ssh servarr 'cd /mnt/user/appdata/discord-wipe && docker compose up -d --build'

# 4. Watch it work.
ssh servarr 'docker logs -f discord-wipe'
```

## Operator runbook

| What | How |
|---|---|
| Status / live log | `ssh servarr docker logs -f discord-wipe` |
| Count deleted so far | `ssh servarr 'jq ".deleted \| length, .export_consumed, .last_pass_at" /mnt/user/appdata/discord-wipe/state/state.json'` |
| Pause cleanly | `ssh servarr docker stop discord-wipe` (state is saved on SIGTERM) |
| Resume | `ssh servarr docker start discord-wipe` |
| Force re-run export phase | stop, edit `state/state.json`, set `"export_consumed": false`, start |
| Skip a guild/DM | edit compose.yaml `command:` to add `--exclude-guild ID` or `--exclude-channel ID`, `docker compose up -d` |
| Tighten/loosen retention | bump `RETENTION_DAYS` in compose.yaml, `docker compose up -d` |
| Bump speed | drop `DELETE_DELAY` toward 0.3s. Stop tightening when 429s appear. |
| Filter logs | `docker logs discord-wipe 2>&1 \| rg -i 'error\|429\|forbidden\|unexpected'` |

## Failure modes / rate limits

- **HTTP 429** — handled. Script reads `retry_after` from the body (or
  `Retry-After` header) and sleeps. Persistent 429s mean either you're
  too fast (raise `DELETE_DELAY`) or Discord has flagged the account
  for inspection (back off, run dry-run, wait a day before trying
  again).
- **HTTP 403** — message is not yours or is a system message
  (call/pin/join). Counted as `forbidden`, marked done, moves on.
- **HTTP 404** — already deleted by you elsewhere or by Discord.
  Counted as `gone`, treated as success.
- **Token rotation** — if you log out / change password, the token
  invalidates. Re-grab it from DevTools, edit `.env`, restart the
  container. The daemon needs a token that stays valid for the
  duration of the wipe.
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

## License

Personal tool. Do whatever you want with it.
