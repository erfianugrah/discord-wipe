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

Two rate-limit systems matter and the script honours both:

### Per-route bucket (header-driven)

Discord publishes the per-bucket state on every response:

| Header | Meaning |
|---|---|
| `X-RateLimit-Limit` | Max requests in the current window |
| `X-RateLimit-Remaining` | Requests left in this window |
| `X-RateLimit-Reset-After` | Seconds until the bucket refills |
| `X-RateLimit-Bucket` | Opaque bucket id |

Optimal pacing is `Reset-After / Remaining` — it spreads the remaining
quota evenly through the window so no request is ever the one that
tips us into 429. The script computes this on every 204 and returns
it as the recommended sleep:

```python
if rem == 0 and reset > 0:
    return "ok", reset                  # bucket empty: wait full window
if rem > 0 and reset > 0:
    return "ok", reset / rem            # spread the remaining quota
return "ok", 0.0                        # no header guidance
```

### Account-level abuse heuristics (floor-driven)

Discord has a SECOND rate-limiter that watches overall request
frequency from an account, independent of per-route buckets. It
flags *speed* much more than *volume* — a hot loop hitting the
release/bucket boundary perfectly for hours is far more likely to
trigger CAPTCHAs / temp-lockouts / token rotations than a leisurely
1 req/sec sustained pace.

`DELETE_DELAY` (default `1.0s`) is the safety floor for this. The
final sleep is `max(DELETE_DELAY, header_pace)` — so when the bucket
is loose, the floor wins; when the bucket is tight, the header wins.

### 429 handling

On 429 we honor `retry_after` from the response body (or
`Retry-After` header) and back off. With proper header-driven
pacing, 429s are rare in steady state — if they're sustained, the
pacer is doing its job by absorbing them.

Sub-1s 429-retry logs are suppressed (the pacer handles them and
there's nothing to act on). `>=1s` retries and 5xx errors are still
logged so persistent throttling is visible.

**Floor is overwritten on 429, persists on header-less 404 (v0.3.3+).**
`extra_sleep` is hoisted to scope level (per-channel in export,
per-scope in catchup) so pacing info learned from one message
applies to the next. The rules:

- **On 429**: `extra_sleep = hint` (the retry_after). Plain overwrite.
  Discord's most recent 429 is by definition the freshest signal
  about current bucket state. v0.3.2 used `max()` here — a ratchet
  that trapped the floor at the historical worst. A single 2.3s
  retry early in a run held pacing at 2.3s forever even when
  subsequent 429s reported the bucket had relaxed to 1.0s. Cost:
  ~10/min throughput loss observed in the 2026-05-28 v0.3.2 deploy.
- **On 204 with valid headers**: `extra_sleep = hint` (the
  `Reset-After / Remaining` estimate). Same overwrite.
- **On 404 with valid headers**: same.
- **On 204/404 with `hint == 0.0`** (no rate-limit headers in the
  response): do NOT touch `extra_sleep`. The `hint > 0` guard means
  a header-less 404 cannot erase a freshly-set 429 floor. Without
  this guard, the 404-dominant degenerate case (re-running a wipe
  whose state was lost) would cycle 429→retry→404→floor=0→429
  forever at ~18/min.

Resulting per-call sleep is `max(DELETE_DELAY, extra_sleep)` — the
static floor for account-level abuse protection plus the dynamic
bucket-derived pace.

Reality check: Discord's bucket is the absolute ceiling. On the
operator's account it currently sits around 1.0-1.7s/call depending
on load. The achievable rate is bounded by Discord's choice, not
ours; our job is to converge cleanly to whatever bucket pace Discord
is currently advertising. Theoretical ceiling at 1.0s/retry would be
~50/min, but the double-pay design (sleep retry_after + sleep
post-mark) soft-caps the 404-dominant phase at ~25-30/min.

### Search endpoint

The `messages/search` endpoint has a separate, much slower limit.
We hit each scope once per `SEARCH_DELAY` (30s) so the per-route
bucket never gets close to empty. The 30s also gives Discord's
search index time to refresh between pages — deleted messages won't
disappear from search results immediately.

### Empirical pace

First live backfill saw header-driven pace settle at ~1.5–1.7s per
delete (Discord's per-account bucket is tighter than the documented
5/5s would suggest). ETA for 105K messages ended up ~48h, vs the
~29h theoretical at 1.0s/delete. The pacer was correct — the docs
were optimistic.

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
  already-gone (404). De-dupes across crashes. Both phases
  pre-filter this set BEFORE any API call:
  - Export phase strips IDs from the per-channel `targets` list.
  - Catchup phase skips on the inner delete loop and counts only
    NEW hits in its no-progress guard.
  Zero wasted API calls on restart — visible in logs as
  `N/N already done — skip` per channel.
- `export_consumed` — Phase 1 runs once. Flipped at the end of the
  first successful export pass. Dry-runs never flip this flag.
- `last_pass_at` / `total_passes` — observability only.

The file is saved atomically (`state.json.tmp` + `rename`) every 10
deletes within a pass, plus once after each scope, plus on `SIGTERM`.

### What `state.deleted` is NOT (the v0.3.0 footgun)

The set looks like a "recently-deleted messages" cache. It is not.
It is a **monotonically-growing log of every ID this script has ever
touched.** Specifically: an ID lands in `state.deleted` because we
just deleted (or observed-gone) a message that was OLDER THAN the
retention cutoff. Its snowflake timestamp is, by construction, OLD.

v0.3.0 shipped a `State.gc(retention_days)` method that interpreted
the set the first way — "drop IDs older than 2x retention, they can
never reappear in a future max_id-bounded search". This was correct
for messages STILL ON DISCORD. It was catastrophically wrong for
messages we'd ALREADY DELETED, because by definition every entry in
`state.deleted` has an old snowflake. The next pass started with a
blank set, re-issued DELETE against the entire just-deleted backlog,
Discord answered 404 ("gone") for each, the script re-marked them —
wasting ~30 redundant DELETEs/min on a live wipe until the operator
stopped the container.

v0.3.1 (commit `5cbbcb3`) reverted the GC entirely and added a
regression test (`Bug4_StateDoesNotGcRecentlyDeletedOldMessages`)
that fires if `hasattr(State, "gc")` ever becomes True again.

If unbounded growth ever becomes a real problem (typical user:
<100 IDs/day in steady state, ~2MB/year), the correct shape is
to track **mark-time** per ID (when the script learned about each
deletion), NOT the message's own snowflake — a state schema change,
deferred until needed.

### Corrupt-state recovery

On load, if `state.json` fails to parse, the script:

1. Renames the bad file to `state.json.corrupt-<ISO timestamp>`
   (so an operator can `jq` it later for forensics).
2. Prints a WARN line naming both paths.
3. Starts fresh with `deleted=set()` and `export_consumed=False`.

The fresh-start is safe — every "forgotten" ID gets re-issued, hits
Discord 404 ("gone"), and is re-marked. Slower, not wrong. The
backup-before-reset is an auditability improvement over v0.2.0
(which silently discarded the file).

### Identity-change pause

The `cmd_run` pre-flight (one `GET /users/@me` per pass) catches not
just 401-rejected tokens but also identity changes: if the operator
rotates `.env` to a *different* account's token, `fresh["id"] !=
me["id"]` triggers `_auth_paused_exit` with a banner saying
`was @X (id=...) now @Y (id=...)`. Without this, the script would
silently search for the original user's messages under the new
user's permissions — mostly 403s and zero results, but invisible.
v0.3.0+ behaviour.

## Resume visibility

When the script restarts mid-backfill, three log lines confirm the
resume is real and no API calls are wasted:

```
[export] resume: 4521/105327 (4.3%) already done across 6/194
         fully-done channels; 100806 targets remaining
[export 1/194] DM         Direct Message with friend-a#0
               9/9 already done — skip
[export 5/194] DM         Direct Message with friend-b#0
               2314 to delete (203/2517 already done)
```

- The `resume:` line comes from a one-time pre-scan that also
  computes the ETA denominator — zero added cost.
- `already done — skip` lines prove the per-channel skip is
  happening before any HTTP.
- The `(X/Y already done)` annotation on partial channels surfaces
  the in-flight resume context.

The pre-scan was added in 25d5197 — see commit message for the
full rationale.

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
