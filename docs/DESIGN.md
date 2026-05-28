# Design notes

## Goals

1. **Rolling retention** — at any point in time, every message I sent
   more than `RETENTION_DAYS` ago should be gone. New messages are
   left alone until they age past the cutoff.
2. **Only my messages** — never delete anyone else's, even when I have
   admin permission in a server that would otherwise allow it.
3. **Resumable** — a 100K-message backfill takes ~30 hours at safe
   rate limits; the daemon must survive crashes, restarts, and Docker
   image upgrades without re-attempting the same IDs.
4. **Boring infrastructure** — a single Compose stack on Unraid; no
   k8s, no separate scheduler, no extra moving parts.

## Two phases per pass

### Phase 1 — Export

Reads Discord's official data export (User Settings → Privacy &
Safety → Request All My Data). The ZIP contains:

```
package/Messages/index.json          {channel_id: display_name}
package/Messages/c{channel_id}/
   ├── channel.json                  {id, type, recipients}
   └── messages.json                 [{ID, Timestamp, Contents, …}]
```

Every `messages.json` entry is a message **the requester sent** —
Discord generates the export from the requester's perspective, so
by definition there's nothing else in there. That's the first layer
of the "only-my-messages" defence.

Phase 1 runs **once**: when it completes, it sets `export_consumed=True`
in the state file and is skipped on every subsequent pass. The export
itself stays mounted (in case we ever want to re-run with a fresh ZIP),
but the steady-state runtime is just Phase 2.

### Phase 2 — Live catchup

Enumerates current scopes:

- `GET /users/@me/guilds` → guild IDs we're in right now.
- `GET /users/@me/channels` → open DM and group-DM channel IDs.

For each scope, queries:

```
GET /{guilds|channels}/<id>/messages/search
    ?author_id=<my_id>
    &max_id=<cutoff_snowflake>
    &offset=0
```

`author_id` is server-side-filtered, so Discord only returns messages
*I* sent. `max_id` is a Discord **snowflake** computed from the
retention cutoff:

```
snowflake = (unix_ms_at_cutoff - 1420070400000) << 22
```

Snowflakes encode their creation timestamp in the upper bits, so
`max_id=<cutoff_snowflake>` means "only messages older than the
cutoff". Recent messages are never returned and the script never
wastes a call on them.

Pagination: the search endpoint returns up to 25 hits per response.
We delete them, wait `SEARCH_DELAY` seconds (~30s — long enough for
Discord's search index to refresh), and re-query. When a page returns
zero hits twice in a row, the scope is done.

## Why not use a third-party tool?

- **`victornpb/undiscord`** is a browser-tab userscript. Great for
  ad-hoc cleanup; useless for a long-lived daemon.
- **`erfianugrah/undiscord-lite`** (and similar forks) — same shape.
- **A bot account** can't delete messages another user sent, so the
  problem is fundamental: only the user's own token can delete the
  user's own messages.

So we wrote ~750 lines of Python.

## Why the export phase at all?

The search API (Phase 2) can in principle find everything older than
the cutoff in every scope. So why bother with the export?

- **3× faster on the backfill.** With known IDs from the export we
  skip the 30s "wait for the search index to refresh" between pages.
  At ~100K messages that's 100K / 25 × 30s ≈ 33 *extra* hours of
  waiting we don't do.
- **Catches dead scopes.** If you left a server two years ago, you
  can't search its messages anymore — but the export still has the
  IDs and you can still `DELETE` them by ID directly.

After the first run, the export phase is skipped and steady-state is
just Phase 2.

## Why one big script and not a framework?

- Single file is easy to audit before running it on every Discord
  message you've ever sent.
- `requests` is stable; no async runtime, no library churn, no
  surprise breaking changes between minor versions.
- The state machine is tiny — a set of deleted IDs + an
  `export_consumed` boolean — and lives in one JSON file. Anything
  bigger (sqlite, redis) is overkill.

## Rate-limit calibration

Discord's documented per-route limits for `DELETE channel messages`
are ~5/sec/channel-bucket. Our `DELETE_DELAY=1.0` keeps us at 1/sec
globally — well under the per-channel limit, and conservative enough
that the rolling per-account abuse-detector doesn't twitch.

The `messages/search` endpoint has a separate, slower limit. We hit
each scope once per `SEARCH_DELAY` (30s) so the per-route bucket
never gets close to empty.

On 429 we honor `retry_after` from the response body (or the
`Retry-After` header) and back off. The default DELETE delay also
auto-extends if a request returns `X-RateLimit-Remaining: 0` —
meaning we just consumed the last token of the bucket — for the value
of `X-RateLimit-Reset-After`.

## State machine

`state/state.json`:

```json
{
  "deleted": ["<msg_id>", ...],
  "export_consumed": true,
  "last_pass_at": "2026-05-28T08:44:50.788Z",
  "total_passes": 12
}
```

- `deleted` — every ID we've successfully DELETEd OR observed as
  already-gone (404). De-dupes across crashes.
- `export_consumed` — Phase 1 runs once. Flipped at the end of the
  first successful export pass. Dry-runs never flip this flag (this
  was a bug discovered by the first end-to-end dry-run; see
  `git log`).
- `last_pass_at` — observability only.
- `total_passes` — observability only.

The file is saved atomically (`state.json.tmp` + `rename`) every 25
deletes within a pass, plus once after each scope, plus on `SIGTERM`.

## What doesn't work

- **Reactions.** The script doesn't remove your reactions on other
  people's messages. Discord doesn't expose those via a sensible
  enumeration, and they're not visible in the export.
- **Threads in archived guilds.** If the guild is gone and the thread
  isn't in the export, there's no way to find the IDs.
- **Voice channel call records.** "User joined call" system messages
  aren't yours to delete; we get 403 and skip.
- **Group-DM rename / add events.** Same — 403, skip.

These are limitations, not bugs.
