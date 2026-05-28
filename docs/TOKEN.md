# Token lifecycle — and why there's no "refresh"

## The short version

Discord **user** tokens are static credentials. There is no refresh
flow, no automated rotation, no "expires in N seconds" claim. A token
is valid until something explicitly invalidates it — and when that
happens, the daemon **parks itself** until you provide a new one.

If you read nothing else, read this: **on 401 we sleep until SIGTERM**.
We do NOT exit-and-restart, because Docker's `restart: unless-stopped`
would re-launch us immediately, hitting Discord with the same dead
token every few seconds — exactly the behaviour their abuse heuristics
flag.

## What invalidates a token

| Trigger | Effect |
|---|---|
| You log out from any session (Settings → Devices → Log Out) | Token revoked |
| You change your password | All tokens revoked |
| You enable / disable 2FA | All tokens revoked |
| Discord's token-leak scanner finds your token on GitHub/etc. | Token revoked, email warning sent |
| Discord flags the account for suspicious activity | Token revoked or rate-limited indefinitely |
| Account gets disabled | Token revoked (with extra steps) |

There is **no documented natural expiry**. Tokens have been observed
to survive months of continuous use, but Discord can rotate them at
any time without notice.

## Why no refresh?

OAuth2 with refresh tokens is the right pattern for **bots** and
**third-party apps** the user has explicitly authorized — those go
through the standard OAuth flow at
`/api/oauth2/authorize` and get an access+refresh token pair.

A **user token** is what your *own browser* uses. It's a session
credential, indistinguishable from a cookie. There is no API surface
to refresh it because the canonical way to "refresh" is to log in
again — which is exactly the rotation flow described below.

## How the daemon detects invalidation

Three points of inspection (in `discord_wipe.py`):

1. **Startup** — `cmd_run` calls `get_me()` before doing anything
   else. If the token's dead at boot, we park immediately and never
   touch any other endpoint.
2. **Top of every pass** — same `get_me()` call. Catches mid-life
   rotation. Cost is one HTTP call per `INTERVAL_HOURS` (default
   one per 24h) — negligible.
3. **Every API call** — `_check_auth(response)` is called on the
   response of every wrapper (`get_me`, `list_my_guilds`,
   `list_my_dms`, `search_messages`, `delete_message`). If any of
   those returns 401, an `AuthError` is raised and bubbles up.

Anywhere `AuthError` lands, `_auth_paused_exit` runs and the daemon
parks.

## What "parked" looks like

- Container stays in `running` state. `docker ps` shows `Up`.
- `docker logs discord-wipe --tail 30` shows the FATAL banner:
  ```
  ========================================================================
  [FATAL] DISCORD TOKEN REJECTED.

  reason: Discord rejected the token (401): {'message': '401: Unauthorized', 'code': 0}
  token: MTc0OTM...gieQ (len=72)

  Discord user tokens have NO refresh flow. Causes:
    - You logged out / logged back in (issues a new token).
    - You changed your password.
    - Discord rotated it (suspected abuse / token-theft scanner).

  To rotate:
    1. Grab the new Authorization header from DevTools.
    2. Edit /mnt/user/appdata/discord-wipe/.env on servarr.
    3. `docker compose up -d` (recreates the container with the
       new env, sending us a graceful SIGTERM).

  Sleeping until SIGTERM. Container stays alive but idle so
  restart-unless-stopped doesn't spin and the dashboard shows
  a clear cause.
  ========================================================================
  ```
- CPU is ~0%. The script is in a `time.sleep(5)` loop waiting for the
  STOP signal.
- **No further Discord API calls are made.** Critical for not getting
  the account flagged.

## Rotation procedure

1. **Log into Discord in a browser** (or use the existing logged-in
   session if rotation was triggered by something else).
2. **Open DevTools (F12) → Network tab.**
3. **Send a message** in any channel.
4. **Find the POST request** to
   `https://discord.com/api/v9/channels/.../messages`.
5. **Copy the `Authorization` request header value.** That's the
   token — no `Bot ` prefix, no `Bearer ` prefix.
6. **Update `.env` on servarr:**
   ```sh
   ssh servarr 'umask 077 && cat > /mnt/user/appdata/discord-wipe/.env' <<EOF
   DISCORD_TOKEN=<new-token-here>
   EOF
   ```
7. **Recreate the container** to pick up the new env:
   ```sh
   ssh servarr 'cd /mnt/user/appdata/discord-wipe && docker compose up -d'
   ```
   This sends SIGTERM to the parked daemon, which exits cleanly. Compose
   then starts a fresh container with the new `DISCORD_TOKEN`.
8. **Verify:**
   ```sh
   ssh servarr 'docker logs --tail 5 discord-wipe'
   ```
   You should see `[run] authenticated as @yourname (id=...)` and a
   new `=== pass start ===` log line.

## Token hygiene

- `.env` lives **only** at `/mnt/user/appdata/discord-wipe/.env` on
  servarr, chmod 600, owner `nobody:users`.
- The token is **never** committed to git. The `.gitignore` enforces
  this; `.env.example` only contains the placeholder `replace-me`.
- The token is **never** logged. The daemon's logs only print a safe
  fingerprint: `MTc0OTM...gieQ (len=72)` — first 6 + last 4 chars + length.
- The token is **never** in CI. GitHub Actions doesn't need it; only
  the running container does.
- Long-term, consider storing it in Vaultwarden and pulling it onto
  servarr at deploy time via the user's `vw_save` / env-rehydrate
  helpers. Currently the canonical copy is `/mnt/user/appdata/discord-wipe/.env`.

## What about getting a fresh token automatically?

Theoretically possible: scripted username/password POST to
`/api/v9/auth/login`. We **do not** do this:

- Discord requires CAPTCHA on login for most accounts.
- It would store the user's actual password somewhere on disk.
- It's exactly the pattern token-thieves use — far higher chance of
  triggering account suspension than periodic manual rotation.

If you ever rotate the token >1× per month, the right answer isn't
automation — it's debugging *why* Discord keeps invalidating it. The
most common cause is leaving the same account logged into Discord on
a phone or tablet whose session keeps expiring.

## Footnote: bot tokens vs user tokens

Bot tokens have the prefix `Bot ` in the `Authorization` header and
follow standard OAuth2 (refresh tokens, scopes, etc). This script
does NOT use bot tokens because:

- Bots can only delete messages they sent themselves OR messages in
  channels where they have `MANAGE_MESSAGES`. They can't delete the
  user's old DMs or messages in servers where the user isn't admin.
- We need to delete the *user's* messages, which can only be done
  with the *user's* credentials.

This trade-off is the entire reason for the "self-bot is technically
against ToS" caveat in the README.
