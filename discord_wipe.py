#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.32"]
# ///
"""
discord-wipe — rolling-retention bulk delete for your own Discord messages.

Deletes every message you've posted that is older than RETENTION_DAYS.
Designed to run forever in a container; one full pass per INTERVAL_HOURS.

Two phases per pass:

  1. Export phase (first run only). Reads your official Discord data
     export (User Settings → Privacy & Safety → Request All My Data).
     Deletes every message in the export whose timestamp is older
     than the cutoff. Marks the export as consumed in the state file
     so subsequent runs skip this phase.

  2. Live catch-up phase (every run). Enumerates your current
     guilds + open DMs via the API and uses the search endpoint with
     a `max_id` snowflake set to the cutoff. Deletes every match.
     Catches anything the export missed (export is stale the moment
     it's generated) plus everything posted between runs.

State file (`STATE_PATH`) tracks which message IDs have been deleted
so crashes / restarts / repeat passes don't re-attempt the same ID.

Auth: Discord user token. Get it from browser DevTools → Network tab
→ send a message → copy the `Authorization` request header. Pass via
DISCORD_TOKEN env var or --token flag. NEVER commit it.

Subcommands:
  verify    POST /users/@me — confirms the token works.
  discover  Show live guilds, open DMs, and export channel counts.
  run       The actual wipe loop. --watch keeps it running forever.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

__version__ = "0.2.0"  # bump on every behaviour change; tag releases as vX.Y.Z

API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000  # 2015-01-01T00:00:00Z

DEFAULT_EXPORT = pathlib.Path(os.environ.get("EXPORT_DIR", "/data/export/Messages"))
DEFAULT_STATE = pathlib.Path(os.environ.get("STATE_PATH", "/data/state/state.json"))

# Mimic a recent Chrome on Linux. Discord's anti-abuse heuristics are gentler
# on requests that look browser-shaped than on `python-requests/2.x`.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Stop flag for clean shutdown on SIGINT / SIGTERM
# ---------------------------------------------------------------------------

STOP = False


def install_signal_handlers() -> None:
    def _handler(signum, frame):
        global STOP
        if STOP:
            print("[signal] second signal — hard exit", file=sys.stderr)
            sys.exit(130)
        STOP = True
        print(
            f"[signal {signum}] finishing current request and exiting cleanly...",
            file=sys.stderr,
        )

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# ---------------------------------------------------------------------------
# Snowflake helpers
# ---------------------------------------------------------------------------


def snowflake_at(dt: datetime) -> int:
    """Snowflake whose timestamp is `dt`. Useful as max_id / min_id bound."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    return (ms - DISCORD_EPOCH_MS) << 22


def snowflake_to_dt(sf: int) -> datetime:
    ms = (int(sf) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# ---------------------------------------------------------------------------
# State file (resume across runs)
# ---------------------------------------------------------------------------


class State:
    """JSON-on-disk state. Tracks deleted IDs + export-consumed flag."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.deleted: set[str] = set()
        self.export_consumed: bool = False
        self.last_pass_at: Optional[str] = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text())
        except json.JSONDecodeError as e:
            print(f"[state] WARN: {self.path} is corrupt ({e}); starting fresh", file=sys.stderr)
            return
        self.deleted = set(d.get("deleted", []))
        self.export_consumed = bool(d.get("export_consumed", False))
        self.last_pass_at = d.get("last_pass_at")

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "deleted": sorted(self.deleted),
                    "export_consumed": self.export_consumed,
                    "last_pass_at": self.last_pass_at,
                }
            )
        )
        tmp.replace(self.path)

    def mark(self, msg_id: str) -> None:
        self.deleted.add(str(msg_id))


# ---------------------------------------------------------------------------
# HTTP client + Discord API wrappers
# ---------------------------------------------------------------------------


class AuthError(RuntimeError):
    """Token rejected by Discord (401). Caller must stop and ask the human
    to rotate the token — there is no refresh flow for user tokens."""


def _check_auth(r: requests.Response) -> None:
    """Raise AuthError on 401 instead of generic HTTPError.

    Discord returns 401 with body {"message": "401: Unauthorized", "code": 0}
    when the token is invalid / revoked / rotated. Treat that as a terminal
    condition — retrying will only attract more abuse-detection attention.
    """
    if r.status_code == 401:
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:200]}
        raise AuthError(f"Discord rejected the token (401): {body}")


def make_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": token,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
    )
    return s


def get_me(s: requests.Session) -> dict:
    r = s.get(f"{API}/users/@me", timeout=30)
    _check_auth(r)
    r.raise_for_status()
    return r.json()


def list_my_guilds(s: requests.Session) -> list[dict]:
    r = s.get(f"{API}/users/@me/guilds", timeout=30)
    _check_auth(r)
    r.raise_for_status()
    return r.json()


def list_my_dms(s: requests.Session) -> list[dict]:
    r = s.get(f"{API}/users/@me/channels", timeout=30)
    _check_auth(r)
    r.raise_for_status()
    return r.json()


def search_messages(
    s: requests.Session,
    *,
    scope: str,  # "guild" or "channel"
    scope_id: str,
    author_id: str,
    max_id: int,
    offset: int = 0,
) -> tuple[Optional[int], list[dict], Optional[float]]:
    """Search returning (total_results, hit_messages, retry_after_secs).

    On 429, returns (None, [], retry_after). On 403/404, returns (-1, [], None) — caller skips scope.
    Otherwise raises on hard errors.
    """
    if scope == "guild":
        url = f"{API}/guilds/{scope_id}/messages/search"
    else:
        url = f"{API}/channels/{scope_id}/messages/search"

    params = {
        "author_id": author_id,
        "max_id": str(max_id),
        "offset": offset,
        "include_nsfw": "true",
    }
    r = s.get(url, params=params, timeout=60)
    _check_auth(r)

    if r.status_code == 429:
        try:
            retry = float(r.json().get("retry_after", 1))
        except Exception:
            retry = float(r.headers.get("Retry-After") or 1)
        return None, [], retry

    if r.status_code in (403, 404):
        # Bot can't search this scope (e.g. guild it can't read, DM that vanished).
        return -1, [], None

    if r.status_code == 202:
        # Search index not yet populated. Respond with retry hint.
        try:
            retry = float(r.json().get("retry_after", 5))
        except Exception:
            retry = 5.0
        return None, [], retry

    r.raise_for_status()
    body = r.json()
    msgs: list[dict] = []
    for group in body.get("messages", []):
        for m in group:
            if m.get("hit"):
                msgs.append(m)
    return body.get("total_results", 0), msgs, None


def delete_message(s: requests.Session, channel_id: str, msg_id: str) -> tuple[str, float]:
    """Delete one message.

    Returns (status, sleep_hint_seconds):
      'ok'        — deleted (204).
      'gone'      — already deleted (404). Treat as success.
      'forbidden' — system message / not yours / channel revoked (403).
      'retry'     — caller should sleep `sleep_hint` and try again.
    """
    url = f"{API}/channels/{channel_id}/messages/{msg_id}"
    r = s.delete(url, timeout=30)
    _check_auth(r)

    if r.status_code == 204:
        # Precise pacing via Discord's per-bucket rate-limit headers.
        # Discord publishes:
        #   X-RateLimit-Limit         max requests in this bucket window
        #   X-RateLimit-Remaining     requests left in current window
        #   X-RateLimit-Reset-After   seconds until the bucket refills
        #   X-RateLimit-Bucket        opaque bucket id
        #
        # Optimal pacing = Reset-After / Remaining — spreads the quota
        # evenly so we never tip into a 429. If Remaining is 0 we wait
        # the full window. The caller takes max(DELETE_DELAY, hint) so
        # DELETE_DELAY remains a safety floor against account-level
        # abuse detection (which is independent of per-route buckets).
        rem_s = r.headers.get("X-RateLimit-Remaining", "")
        reset = float(r.headers.get("X-RateLimit-Reset-After", "0") or 0)
        try:
            rem = int(rem_s) if rem_s != "" else -1
        except ValueError:
            rem = -1
        if rem == 0 and reset > 0:
            return "ok", reset  # bucket empty: wait full window
        if rem > 0 and reset > 0:
            return "ok", reset / rem  # spread the remaining quota
        return "ok", 0.0

    if r.status_code == 404:
        return "gone", 0.0

    if r.status_code == 400:
        # Discord uses HTTP 400 with a semantic `code` field for terminal
        # errors that retrying will never fix:
        #   50083  Thread is archived. Cannot delete messages in archived
        #          threads without unarchiving first; we don't have
        #          MANAGE_THREADS in most servers, so skip.
        #   50001  Missing access (channel revoked since export).
        #   50021  Cannot execute action on a system message.
        #   50034  Message too old to bulk-delete (we don't bulk-delete,
        #          but Discord sometimes returns this on archived stuff).
        #   160005 Thread is locked.
        # All are non-retryable. Log once, count as forbidden, move on.
        try:
            code = int(r.json().get("code", 0))
            msg = r.json().get("message", "")
        except Exception:
            code, msg = 0, r.text[:120]
        print(
            f"[delete] terminal 400 code={code} for {channel_id}/{msg_id}: {msg}",
            file=sys.stderr,
        )
        return "forbidden", 0.0

    if r.status_code == 403:
        return "forbidden", 0.0

    if r.status_code == 429:
        try:
            retry = float(r.json().get("retry_after", 1))
        except Exception:
            retry = float(r.headers.get("Retry-After") or 1)
        return "retry", retry

    if r.status_code >= 500:
        return "retry", 5.0

    print(
        f"[delete] unexpected {r.status_code} for {channel_id}/{msg_id}: {r.text[:200]}",
        file=sys.stderr,
    )
    return "retry", 2.0


# ---------------------------------------------------------------------------
# Export reader
# ---------------------------------------------------------------------------


@dataclass
class ExportChannel:
    id: str
    type: str  # "DM" | "GROUP_DM" | "GUILD_TEXT" | ...
    name: str  # human-readable from index.json
    msgs_path: pathlib.Path


def read_export(export_dir: pathlib.Path) -> list[ExportChannel]:
    if not export_dir.exists():
        raise FileNotFoundError(f"export dir not found: {export_dir}")
    index_path = export_dir / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    out: list[ExportChannel] = []
    for d in sorted(export_dir.iterdir()):
        if not d.is_dir() or not d.name.startswith("c"):
            continue
        cid = d.name[1:]
        ch_meta = json.loads((d / "channel.json").read_text())
        out.append(
            ExportChannel(
                id=cid,
                type=ch_meta.get("type", "UNKNOWN"),
                name=index.get(cid, "?"),
                msgs_path=d / "messages.json",
            )
        )
    return out


def parse_export_ts(s: str) -> datetime:
    """Export timestamps are 'YYYY-MM-DD HH:MM:SS' UTC."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------


@dataclass
class WipeConfig:
    token: str
    me_id: str
    state: State
    cutoff: datetime
    delete_delay: float
    search_delay: float
    dry_run: bool
    exclude_guilds: set[str]
    exclude_channels: set[str]


def _format_eta(seconds: float) -> str:
    """Human-friendly ETA string for progress logs (e.g. '22h13m', '4m12s')."""
    if seconds < 0 or seconds != seconds:  # NaN-safe
        return "?"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds // 60:.0f}m{seconds % 60:.0f}s"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hours:.0f}h{mins:.0f}m"


def phase_export(s: requests.Session, cfg: WipeConfig, export_dir: pathlib.Path) -> None:
    """Delete every message in the export with timestamp < cutoff."""
    if cfg.state.export_consumed:
        print("[export] already consumed in a previous run — skipping")
        return

    print(f"[export] reading {export_dir}")
    channels = read_export(export_dir)
    cutoff_local = cfg.cutoff
    print(
        f"[export] {len(channels)} channels in export; "
        f"deleting messages older than {cutoff_local.isoformat()}"
    )

    counters = {"ok": 0, "gone": 0, "forbidden": 0, "skip_recent": 0, "skip_done": 0}
    # Resume summary upfront: how much was already done, how much is
    # still left, and how many channels are fully done so the resume
    # is auditable from the logs alone. Pre-counts all targets (194
    # small JSON reads, ~2s) which also feeds the running ETA.
    grand_total = 0
    already_done_total = 0
    fully_done_channels = 0
    for ch in channels:
        with contextlib.suppress(Exception):
            msgs = json.loads(ch.msgs_path.read_text())
            grand_total += len(msgs)
            ch_done = sum(1 for m in msgs if str(m["ID"]) in cfg.state.deleted)
            already_done_total += ch_done
            if ch_done == len(msgs):
                fully_done_channels += 1
    grand_done = already_done_total
    remaining = grand_total - already_done_total
    t0 = time.monotonic()
    deletes_since_t0 = 0
    print(
        f"[export] resume: {already_done_total}/{grand_total} "
        f"({100.0 * already_done_total / grand_total:.1f}%) already done "
        f"across {fully_done_channels}/{len(channels)} fully-done channels; "
        f"{remaining} targets remaining"
    )

    for ci, ch in enumerate(channels, 1):
        if STOP:
            break
        if ch.id in cfg.exclude_channels:
            print(f"[export {ci}/{len(channels)}] skip excluded {ch.id}")
            continue

        try:
            msgs = json.loads(ch.msgs_path.read_text())
        except FileNotFoundError:
            print(f"[export {ci}/{len(channels)}] {ch.id}: no messages.json")
            continue

        # Filter to old + not-already-deleted.
        targets: list[str] = []
        ch_skip_done = 0
        for m in msgs:
            mid = str(m["ID"])
            if mid in cfg.state.deleted:
                counters["skip_done"] += 1
                ch_skip_done += 1
                continue
            try:
                ts = parse_export_ts(m["Timestamp"])
            except Exception:
                # Malformed row — best effort, queue for delete.
                targets.append(mid)
                continue
            if ts >= cutoff_local:
                counters["skip_recent"] += 1
                continue
            targets.append(mid)

        prefix = f"[export {ci}/{len(channels)}] {ch.type:10} {ch.name[:50]:50}"
        if not targets:
            # Channel fully done. Print a one-liner so the resume is
            # visible and the log doesn't look like the script jumped
            # from channel 1 straight to channel 4.
            if ch_skip_done > 0:
                print(f"{prefix} {ch_skip_done}/{len(msgs)} already done — skip")
            continue

        if ch_skip_done > 0:
            print(f"{prefix} {len(targets)} to delete ({ch_skip_done}/{len(msgs)} already done)")
        else:
            print(f"{prefix} {len(targets)} to delete")

        for j, mid in enumerate(targets, 1):
            if STOP:
                break
            if cfg.dry_run:
                counters["ok"] += 1
                # Mark even in dry-run so the catchup phase doesn't
                # double-count the same IDs. Real-run state hygiene is
                # protected by the separate-state-file convention.
                cfg.state.mark(mid)
                grand_done += 1
                deletes_since_t0 += 1
                continue

            extra_sleep = 0.0
            while True:
                status, hint = delete_message(s, ch.id, mid)
                if status == "retry":
                    # Quiet log: routine sub-second 429s from bucket
                    # edge are expected and floodful; only print the
                    # noteworthy ones (≥1s) and 5xx retries.
                    if hint >= 1.0:
                        print(f"  rate-limited; sleep {hint:.1f}s", file=sys.stderr)
                    time.sleep(hint)
                    if STOP:
                        break
                    continue
                if status == "ok":
                    extra_sleep = hint
                break

            if status == "ok":
                counters["ok"] += 1
            elif status == "gone":
                counters["gone"] += 1
            elif status == "forbidden":
                counters["forbidden"] += 1

            cfg.state.mark(mid)
            grand_done += 1
            deletes_since_t0 += 1

            if j % 10 == 0:
                cfg.state.save()
                elapsed = time.monotonic() - t0
                rate = deletes_since_t0 / elapsed if elapsed > 0 else 0
                remaining = max(0, grand_total - grand_done)
                eta = remaining / rate if rate > 0 else 0
                pct = 100.0 * grand_done / grand_total if grand_total else 0
                print(
                    f"    {j}/{len(targets)} ok={counters['ok']} "
                    f"gone={counters['gone']} 403={counters['forbidden']} "
                    f"| total: {grand_done}/{grand_total} ({pct:.1f}%) "
                    f"~{rate * 60:.0f}/min ETA {_format_eta(eta)}"
                )
            # max(floor, header-driven hint), NOT sum. DELETE_DELAY is
            # the safety floor against account-level abuse heuristics;
            # extra_sleep is the per-bucket optimal pace. When the
            # bucket has slack, the floor wins; when the bucket is
            # tight, the header wins.
            time.sleep(max(cfg.delete_delay, extra_sleep))

        cfg.state.save()

    if not STOP and not cfg.dry_run:
        # In dry-run we never want to flip this flag — a subsequent
        # real run on the same state file MUST still process the
        # export. (Convention is to use a separate state file for dry
        # runs, but belt-and-braces.)
        cfg.state.export_consumed = True
    cfg.state.save()
    print(f"[export] done: {counters}")


def phase_live_catchup(s: requests.Session, cfg: WipeConfig) -> None:
    """Search-API sweep across every live guild + open DM."""
    cutoff_snowflake = snowflake_at(cfg.cutoff)

    guilds = list_my_guilds(s)
    dms = list_my_dms(s)
    print(
        f"[catchup] {len(guilds)} guilds, {len(dms)} DM channels; "
        f"cutoff={cfg.cutoff.isoformat()} (snowflake={cutoff_snowflake})"
    )

    counters = {"ok": 0, "gone": 0, "forbidden": 0}

    targets: list[tuple[str, str, str]] = []  # (scope, scope_id, label)
    for g in guilds:
        if g["id"] in cfg.exclude_guilds:
            print(f"[catchup] skip excluded guild {g['id']} ({g.get('name')})")
            continue
        targets.append(("guild", g["id"], f"guild:{g.get('name', g['id'])}"))
    for c in dms:
        if c["id"] in cfg.exclude_channels:
            print(f"[catchup] skip excluded channel {c['id']}")
            continue
        kind = c.get("type")
        # 1=DM, 3=GROUP_DM
        label = f"dm:{c['id']}" if kind == 1 else f"groupdm:{c['id']}"
        targets.append(("channel", c["id"], label))

    for ti, (scope, scope_id, label) in enumerate(targets, 1):
        if STOP:
            break
        print(f"[catchup {ti}/{len(targets)}] {label}")

        # Loop: search → delete → wait for index → repeat. Empty page ends scope.
        empty_streak = 0
        while not STOP:
            total, hits, retry = search_messages(
                s,
                scope=scope,
                scope_id=scope_id,
                author_id=cfg.me_id,
                max_id=cutoff_snowflake,
                offset=0,
            )
            if total is None:
                # 429 / 202
                print(f"  rate-limited / index lag; sleep {retry:.1f}s")
                time.sleep(retry or 5)
                continue
            if total == -1:
                print("  no permission to search this scope; skipping")
                break
            if not hits:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                # Index lag: wait one search_delay then try once more.
                time.sleep(cfg.search_delay)
                continue
            empty_streak = 0

            # No-progress guard. Real deletes remove items from search
            # so a fresh query returns new hits; dry-run leaves them in
            # place, and a stale search index can also re-serve already
            # deleted IDs. If every hit on this page is already marked,
            # the scope is effectively done.
            new_in_page = sum(1 for m in hits if str(m["id"]) not in cfg.state.deleted)
            if new_in_page == 0:
                print(f"  page: {len(hits)} hits, all already done; scope finished")
                break

            print(f"  page: {len(hits)} hits ({new_in_page} new, search reports total={total})")

            # Hits are author-filtered + max_id-bounded server-side.
            for m in hits:
                if STOP:
                    break
                mid = str(m["id"])
                if mid in cfg.state.deleted:
                    continue
                cid = str(m["channel_id"])
                if cfg.dry_run:
                    counters["ok"] += 1
                    cfg.state.mark(mid)
                    continue

                extra_sleep = 0.0
                while True:
                    status, hint = delete_message(s, cid, mid)
                    if status == "retry":
                        print(f"    rate-limited; sleep {hint:.1f}s")
                        time.sleep(hint)
                        if STOP:
                            break
                        continue
                    if status == "ok":
                        extra_sleep = hint
                    break

                if status == "ok":
                    counters["ok"] += 1
                elif status == "gone":
                    counters["gone"] += 1
                elif status == "forbidden":
                    counters["forbidden"] += 1

                cfg.state.mark(mid)
                time.sleep(cfg.delete_delay + extra_sleep)

            cfg.state.save()
            # Wait for search index to catch up before re-querying.
            time.sleep(cfg.search_delay)

    print(f"[catchup] done: {counters}")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_verify(args) -> int:
    s = make_session(args.token)
    try:
        me = get_me(s)
    except AuthError as e:
        print(f"FAIL — {e}", file=sys.stderr)
        return 2
    print(f"OK — @{me.get('username', '?')} (id={me['id']})")
    return 0


def cmd_discover(args) -> int:
    s = make_session(args.token)
    me = get_me(s)
    print(f"You: @{me.get('username')} (id={me['id']})\n")

    print("== Live guilds ==")
    for g in list_my_guilds(s):
        print(f"  {g['id']}  {g.get('name', '?')}")
    print()

    print("== Open DM channels ==")
    for c in list_my_dms(s):
        kind = "DM" if c.get("type") == 1 else "GROUP_DM"
        recips = ",".join(r.get("username", r.get("id", "?")) for r in c.get("recipients", []))
        print(f"  {c['id']}  {kind:8} [{recips}]")
    print()

    if args.export_dir.exists():
        print(f"== Export at {args.export_dir} ==")
        chans = read_export(args.export_dir)
        total = 0
        for c in chans:
            try:
                n = len(json.loads(c.msgs_path.read_text()))
            except Exception:
                n = 0
            total += n
            print(f"  {c.id}  {c.type:10}  {n:6}  {c.name[:60]}")
        print(f"-- {len(chans)} channels, {total} messages --")
    else:
        print(f"(no export at {args.export_dir})")

    return 0


def _auth_paused_exit(token_hint: str, reason: str) -> int:
    """Discord rejected our token. Park the container until SIGTERM.

    Critical safety behaviour: do NOT exit non-zero in a tight loop. With
    Docker's `restart: unless-stopped` policy that would re-launch us
    immediately, hitting Discord again with the same dead token — a fast
    track to abuse-flagging the account. Sleep for a long time instead;
    `docker compose up -d` with a new .env will SIGTERM us awake.
    """
    print(
        "\n" + "=" * 72 + "\n"
        "[FATAL] DISCORD TOKEN REJECTED.\n\n"
        f"reason: {reason}\n"
        f"token: {token_hint}\n\n"
        "Discord user tokens have NO refresh flow. Causes:\n"
        "  - You logged out / logged back in (issues a new token).\n"
        "  - You changed your password.\n"
        "  - Discord rotated it (suspected abuse / token-theft scanner).\n\n"
        "To rotate:\n"
        "  1. Grab the new Authorization header from DevTools.\n"
        "  2. Edit /mnt/user/appdata/discord-wipe/.env on servarr.\n"
        "  3. `docker compose up -d` (recreates the container with the\n"
        "     new env, sending us a graceful SIGTERM).\n\n"
        "Sleeping until SIGTERM. Container stays alive but idle so\n"
        "restart-unless-stopped doesn't spin and the dashboard shows\n"
        "a clear cause.\n" + "=" * 72,
        file=sys.stderr,
        flush=True,
    )
    # Sleep in 5s chunks so SIGTERM is responsive.
    while not STOP:
        time.sleep(5)
    return 0


def _token_hint(token: str) -> str:
    """Safe-to-log token fingerprint (never the secret itself)."""
    if not token:
        return "(empty)"
    return f"{token[:6]}...{token[-4:]} (len={len(token)})"


def cmd_run(args) -> int:
    install_signal_handlers()
    s = make_session(args.token)
    try:
        me = get_me(s)
    except AuthError as e:
        return _auth_paused_exit(_token_hint(args.token), str(e))
    print(f"[run] authenticated as @{me.get('username')} (id={me['id']})")

    state = State(args.state)
    print(
        f"[run] state: {args.state} ({len(state.deleted)} IDs already done; "
        f"export_consumed={state.export_consumed})"
    )

    while True:
        # Pre-flight: catch token rotation between passes before doing
        # expensive work. Costs one HTTP call per pass; negligible.
        try:
            get_me(s)
        except AuthError as e:
            return _auth_paused_exit(_token_hint(args.token), str(e))

        cutoff = datetime.now(timezone.utc) - timedelta(days=args.retention_days)
        cfg = WipeConfig(
            token=args.token,
            me_id=me["id"],
            state=state,
            cutoff=cutoff,
            delete_delay=args.delete_delay,
            search_delay=args.search_delay,
            dry_run=args.dry_run,
            exclude_guilds=set(args.exclude_guild or []),
            exclude_channels=set(args.exclude_channel or []),
        )
        print(f"\n[run] === pass start: cutoff={cutoff.isoformat()} ===")
        t0 = time.time()

        try:
            if args.export_dir.exists() and not state.export_consumed:
                phase_export(s, cfg, args.export_dir)
            else:
                if not args.export_dir.exists():
                    print(f"[run] no export at {args.export_dir} — skip export phase")

            if not STOP:
                phase_live_catchup(s, cfg)

        except AuthError as e:
            return _auth_paused_exit(_token_hint(args.token), str(e))

        except requests.HTTPError as e:
            print(f"[run] HTTP error: {e} {getattr(e.response, 'text', '')[:300]}", file=sys.stderr)

        state.last_pass_at = datetime.now(timezone.utc).isoformat()
        state.save()
        elapsed = time.time() - t0
        print(f"[run] === pass complete in {elapsed:.0f}s ===")

        if STOP:
            print("[run] stop signal — exiting")
            return 0
        if not args.watch:
            return 0

        sleep_for = args.interval_hours * 3600
        wake_at = datetime.now(timezone.utc) + timedelta(seconds=sleep_for)
        print(f"[run] sleeping {sleep_for}s; next pass at {wake_at.isoformat()}")
        # Sleep in small chunks so SIGTERM responds quickly.
        slept = 0.0
        while slept < sleep_for and not STOP:
            time.sleep(min(5.0, sleep_for - slept))
            slept += 5.0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="discord-wipe",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--token",
        default=os.environ.get("DISCORD_TOKEN"),
        help="Discord user token (default: $DISCORD_TOKEN)",
    )

    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("verify", help="check the token works")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("discover", help="show live guilds + DMs + export contents")
    p.add_argument("--export-dir", type=pathlib.Path, default=DEFAULT_EXPORT)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("run", help="run wipe pass(es)")
    p.add_argument("--export-dir", type=pathlib.Path, default=DEFAULT_EXPORT)
    p.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    p.add_argument(
        "--retention-days",
        type=float,
        default=float(os.environ.get("RETENTION_DAYS", "7")),
        help="messages older than this are deleted (default 7, env RETENTION_DAYS)",
    )
    p.add_argument(
        "--delete-delay",
        type=float,
        default=float(os.environ.get("DELETE_DELAY", "1.0")),
        help="seconds between DELETE calls (default 1.0)",
    )
    p.add_argument(
        "--search-delay",
        type=float,
        default=float(os.environ.get("SEARCH_DELAY", "30.0")),
        help="seconds between search-API page fetches; "
        "long enough for the search index to refresh (default 30)",
    )
    p.add_argument(
        "--interval-hours",
        type=float,
        default=float(os.environ.get("INTERVAL_HOURS", "24")),
        help="hours between passes when --watch is set (default 24)",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        default=os.environ.get("WATCH", "").lower() in {"1", "true", "yes"},
        help="loop forever instead of single pass (env WATCH=1)",
    )
    p.add_argument("--dry-run", action="store_true", help="don't actually delete; just report")
    p.add_argument("--exclude-guild", action="append", help="guild ID to skip (repeatable)")
    p.add_argument("--exclude-channel", action="append", help="channel/DM ID to skip (repeatable)")
    p.set_defaults(func=cmd_run)

    return ap.parse_args()


def main() -> int:
    args = parse_args()
    if not args.token:
        print("ERROR: no token. Set DISCORD_TOKEN env var or pass --token.", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
