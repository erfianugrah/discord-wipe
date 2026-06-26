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
  search    Search your messages with optional content filter and preview
            channel + timestamp + content before deciding to delete.
  purge     One-shot targeted wipe of specific guilds/channels.
  run       The rolling-retention wipe loop. --watch keeps it running forever.
  status    Read state.json and print a summary (no API calls).
  seed-from-export  Rebuild state.deleted from the export (recovery; no API).
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import os
import pathlib
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

__version__ = "0.6.0"  # bump on every behaviour change; tag releases as vX.Y.Z

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

# Restart-burst guard (v0.4.0+). If cmd_run starts >RESTART_BURST_MAX times
# within RESTART_BURST_WINDOW seconds, park instead of letting docker's
# `restart: unless-stopped` spin us forever against Discord.
RESTART_BURST_MAX = 5
RESTART_BURST_WINDOW = 600  # 10 minutes

# Transient-network retry (v0.4.1+). Discord-bound HTTP calls retry on
# connection-level failures — DNS resolution, connection reset, read
# timeout — before giving up. The canonical trigger is a host reboot:
# every container starts at once, often before the Docker network /
# upstream resolver is ready, so discord.com fails to resolve for a few
# seconds. Without retry that ConnectionError crashes the daemon on its
# very first call (get_me); docker's `restart: unless-stopped` respawns
# it; six crashes inside RESTART_BURST_WINDOW trip the restart-burst
# guard and PARK the daemon on what was a self-healing 30-second blip.
# Bounded exponential backoff rides the blip out so the process never
# crashes. Defaults: 8 attempts, 2s base, 30s cap → ~2min ride-out/call.
NET_RETRY_MAX = int(os.environ.get("NET_RETRY_MAX", "8") or 8)
NET_RETRY_BASE = float(os.environ.get("NET_RETRY_BASE", "2.0") or 2.0)
NET_RETRY_CAP = float(os.environ.get("NET_RETRY_CAP", "30.0") or 30.0)

# Metrics server bind (v0.4.0+). Default 0.0.0.0:9090 inside the container;
# compose maps it to 127.0.0.1:9090 on the host so only same-host scrapers
# (Prometheus on the homelab) can reach it.
METRICS_BIND = os.environ.get("METRICS_BIND", "0.0.0.0:9090")
METRICS_ENABLED = os.environ.get("METRICS_ENABLED", "1").lower() in {"1", "true", "yes"}

# Notify-on-park (v0.4.0+). If set, POST a notification to this URL on
# every park event (401, identity-change, state-unwritable, restart-burst).
# ntfy.sh-compatible: bare URL like https://ntfy.sh/<topic> works.
NTFY_URL = os.environ.get("NTFY_URL", "").strip()

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
    """JSON-on-disk state. Tracks deleted IDs + export-consumed flag
    + restart-burst counters (v0.4.0+)."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StateUnwritableError(
                f"state directory {self.path.parent} is unwritable: {e}"
            ) from e
        self.deleted: set[str] = set()
        self.export_consumed: bool = False
        self.last_pass_at: Optional[str] = None
        # Restart-burst guard (v0.4.0+). If the container crashes >5 times
        # within RESTART_BURST_WINDOW seconds, park instead of letting
        # docker keep restarting us into a hot loop against Discord.
        self.last_started_at: Optional[str] = None
        self.restart_burst: int = 0
        # Heartbeat path (next to state.json, also in the bind-mount).
        # HEALTHCHECK reads its mtime; state.save() touches it.
        self.heartbeat_path = self.path.with_name("heartbeat")
        # Last-known-good snapshot. save() rotates the current good
        # state.json here before swapping in the new one, and _load()
        # falls back to it when state.json is missing/empty/corrupt. This
        # is what makes a single bad write non-fatal (see Bug12).
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        self._load()

    def _quarantine_corrupt(self, candidate: pathlib.Path, err: Exception) -> None:
        """Move an unparseable state file aside (auditable, not silent)."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = candidate.with_name(f"{candidate.name}.corrupt-{ts}")
        try:
            candidate.rename(backup)
            print(
                f"[state] WARN: {candidate} is corrupt ({err}); moved to {backup}",
                file=sys.stderr,
            )
        except OSError as rename_err:
            print(
                f"[state] WARN: {candidate} is corrupt ({err}); could not back up ({rename_err})",
                file=sys.stderr,
            )

    def _load(self) -> None:
        # Try the live file first, then the last-known-good .bak. Any
        # candidate that exists but won't parse — e.g. a 0-byte file left
        # by a non-durable write on a prior version — is quarantined and
        # we fall through. Starting fresh is safe but slow (every ID we
        # forget is re-issued, 404s, is classified 'gone', and re-marked),
        # so the .bak fallback exists to avoid that whenever possible.
        for candidate, is_backup in ((self.path, False), (self.backup_path, True)):
            if not candidate.exists():
                continue
            try:
                raw = candidate.read_text()
            except OSError:
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError as e:
                self._quarantine_corrupt(candidate, e)
                continue
            self.deleted = set(d.get("deleted", []))
            self.export_consumed = bool(d.get("export_consumed", False))
            self.last_pass_at = d.get("last_pass_at")
            self.last_started_at = d.get("last_started_at")
            self.restart_burst = int(d.get("restart_burst", 0) or 0)
            if is_backup:
                print(
                    f"[state] recovered {len(self.deleted)} IDs from {candidate.name}; "
                    f"live state.json was missing/empty/corrupt",
                    file=sys.stderr,
                )
            return
        # Neither file usable — fresh start (deleted stays empty).

    def save(self) -> None:
        """Durably persist state: atomic write + fsync + last-good backup.

        A plain write_text()+rename is NOT crash-safe on Unraid's
        /mnt/user shfs FUSE overlay. The rename metadata journals but the
        data pages may never flush if the container is SIGKILLed (stop-
        grace overrun, host reboot, OOM) inside the writeback window,
        leaving a 0-byte state.json. That exact failure erased a
        107k-ID *completed* wipe twice on 2026-06-08, forcing a from-
        scratch re-grind of ~105k already-deleted messages against the
        punishing old-message DELETE rate limit (~16/min). See Bug12.

        Durability recipe:
          1. write tmp, flush, fsync(fd)        -> data is on disk
          2. rotate current good state.json -> .bak (atomic rename)
          3. rename tmp -> state.json           (atomic)
          4. fsync the parent directory         -> the renames are durable
        _load() falls back to .bak when state.json is missing/empty/
        corrupt, so even a torn write loses at most the last increment.
        """
        tmp = self.path.with_suffix(".json.tmp")
        payload = json.dumps(
            {
                "deleted": sorted(self.deleted),
                "export_consumed": self.export_consumed,
                "last_pass_at": self.last_pass_at,
                "last_started_at": self.last_started_at,
                "restart_burst": self.restart_burst,
            }
        )
        try:
            with open(tmp, "w") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            # Rotate the previous good copy to .bak (atomic, metadata-only
            # — no data copy) before swapping in the new one.
            if self.path.exists():
                with contextlib.suppress(OSError):
                    os.replace(self.path, self.backup_path)
            os.replace(tmp, self.path)
            # Commit the rename(s) themselves so a crash can't resurrect
            # the old directory entry pointing at freed/zero data.
            with contextlib.suppress(OSError):
                dfd = os.open(self.path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
        except OSError as e:
            raise StateUnwritableError(f"could not write state file {self.path}: {e}") from e
        # Heartbeat: a separate file whose mtime is what the HEALTHCHECK
        # inspects. Best-effort — if it fails we don't crash the save.
        with contextlib.suppress(OSError):
            self.heartbeat_path.write_text(self.last_pass_at or "")

    def touch_heartbeat(self) -> None:
        """Bump heartbeat mtime without rewriting state.json. Called from
        the long sleep loop between passes so HEALTHCHECK stays green."""
        with contextlib.suppress(OSError):
            self.heartbeat_path.touch(exist_ok=True)

    def record_start(self, now: Optional[datetime] = None) -> None:
        """Update last_started_at + restart_burst on cmd_run startup.

        If we started <RESTART_BURST_WINDOW seconds ago, increment the
        burst counter; else reset to 1. The caller checks the counter
        afterwards and parks if it exceeds the threshold.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        prev_iso = self.last_started_at
        if prev_iso:
            try:
                prev = datetime.fromisoformat(prev_iso)
                if (now - prev).total_seconds() < RESTART_BURST_WINDOW:
                    self.restart_burst += 1
                else:
                    self.restart_burst = 1
            except ValueError:
                self.restart_burst = 1
        else:
            self.restart_burst = 1
        self.last_started_at = now.isoformat()

    def mark(self, msg_id: str) -> None:
        self.deleted.add(str(msg_id))

    # DO NOT add a snowflake-timestamp-based gc() here.
    #
    # IDs in self.deleted are IDs of messages we DELETED — i.e. messages
    # that were OLDER than the retention cutoff. Their snowflake
    # timestamps are therefore OLD by definition. Any GC that drops
    # "IDs older than X" will sweep out the IDs we just successfully
    # processed, causing the next pass to re-attempt 100% of them
    # against Discord (each one returning 404 “gone”). Past commit
    # fc1b289 (v0.3.0) shipped exactly that bug and burned ~8h of
    # API calls on a live wipe. See AGENTS.md "Hard safety rules".
    #
    # If unbounded growth becomes a real problem (typical user: <100
    # IDs/day in steady state, ~2MB/year), the correct fix is to track
    # MARK-TIME per ID (timestamp the script learned about the
    # deletion), not the message's own snowflake. That needs a state
    # schema change. Until then, growth is bounded by activity.


# ---------------------------------------------------------------------------
# Metrics (Prometheus exposition format, stdlib http.server)
# ---------------------------------------------------------------------------


@dataclass
class Metrics:
    """Prometheus-format metrics exposed on /metrics. Thread-safe enough
    for our use (CPython dict updates are atomic under the GIL).

    Created at process start; counters incremented inline by the delete
    loops; gauges refreshed on demand from the live State.
    """

    deletes: dict[str, int] = field(default_factory=lambda: {"ok": 0, "gone": 0, "forbidden": 0})
    state: Optional["State"] = None
    parked: bool = False
    park_reason: str = ""
    last_pass_start: Optional[float] = None
    last_pass_end: Optional[float] = None
    extra_sleep_export: float = 0.0
    extra_sleep_catchup: float = 0.0

    def inc(self, outcome: str) -> None:
        self.deletes[outcome] = self.deletes.get(outcome, 0) + 1

    def format_prom(self) -> str:
        lines: list[str] = []
        lines.append("# HELP discord_wipe_deletes_total Delete operations by outcome")
        lines.append("# TYPE discord_wipe_deletes_total counter")
        for outcome, n in self.deletes.items():
            lines.append(f'discord_wipe_deletes_total{{outcome="{outcome}"}} {n}')

        lines.append("# HELP discord_wipe_state_deleted_count IDs tracked in state.deleted")
        lines.append("# TYPE discord_wipe_state_deleted_count gauge")
        n_deleted = len(self.state.deleted) if self.state else 0
        lines.append(f"discord_wipe_state_deleted_count {n_deleted}")

        lines.append("# HELP discord_wipe_export_consumed 1 if export phase has run successfully")
        lines.append("# TYPE discord_wipe_export_consumed gauge")
        consumed = 1 if (self.state and self.state.export_consumed) else 0
        lines.append(f"discord_wipe_export_consumed {consumed}")

        lines.append("# HELP discord_wipe_parked 1 if daemon is parked (401, identity, state)")
        lines.append("# TYPE discord_wipe_parked gauge")
        lines.append(f"discord_wipe_parked {1 if self.parked else 0}")

        if self.last_pass_start:
            lines.append("# HELP discord_wipe_last_pass_start_seconds Unix ts of last pass start")
            lines.append("# TYPE discord_wipe_last_pass_start_seconds gauge")
            lines.append(f"discord_wipe_last_pass_start_seconds {self.last_pass_start:.0f}")
        if self.last_pass_end:
            lines.append("# HELP discord_wipe_last_pass_end_seconds Unix ts of last pass end")
            lines.append("# TYPE discord_wipe_last_pass_end_seconds gauge")
            lines.append(f"discord_wipe_last_pass_end_seconds {self.last_pass_end:.0f}")

        lines.append("# HELP discord_wipe_extra_sleep_seconds Current pacing floor in each phase")
        lines.append("# TYPE discord_wipe_extra_sleep_seconds gauge")
        lines.append(
            f'discord_wipe_extra_sleep_seconds{{phase="export"}} {self.extra_sleep_export:.3f}'
        )
        lines.append(
            f'discord_wipe_extra_sleep_seconds{{phase="catchup"}} {self.extra_sleep_catchup:.3f}'
        )

        return "\n".join(lines) + "\n"


METRICS = Metrics()


def _start_metrics_server(metrics: Metrics, bind: str) -> Optional[http.server.HTTPServer]:
    """Spawn a daemon thread serving /metrics. Returns the server (so the
    caller can shutdown), or None if binding failed (port in use, etc)."""
    host, _, port_s = bind.rpartition(":")
    if not host:
        host = "0.0.0.0"
    try:
        port = int(port_s)
    except ValueError:
        print(f"[metrics] invalid bind {bind!r}; disabled", file=sys.stderr)
        return None

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            body = metrics.format_prom().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence the default access log
            return

    try:
        httpd = http.server.HTTPServer((host, port), _Handler)
    except OSError as e:
        print(f"[metrics] could not bind {host}:{port}: {e}; disabled", file=sys.stderr)
        return None
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="metrics")
    t.start()
    print(f"[metrics] listening on http://{host}:{port}/metrics", file=sys.stderr)
    return httpd


# ---------------------------------------------------------------------------
# Notify-on-park (env-gated webhook)
# ---------------------------------------------------------------------------


def _notify_park(reason: str, banner: str) -> None:
    """POST a notification to NTFY_URL if set. Best-effort, never raises.

    Compatible with ntfy.sh: bare URL with the body as the message,
    headers for title/priority/tags.
    """
    if not NTFY_URL:
        return
    try:
        requests.post(
            NTFY_URL,
            data=banner.encode("utf-8")[:3000],  # ntfy.sh caps at 4KB
            headers={
                "Title": f"discord-wipe parked: {reason}",
                "Priority": "high",
                "Tags": "warning,robot",
            },
            timeout=10,
        )
        print(f"[notify] sent park notification to {NTFY_URL.split('/')[-1]}", file=sys.stderr)
    except Exception as e:
        print(f"[notify] failed to send park notification: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# HTTP client + Discord API wrappers
# ---------------------------------------------------------------------------


class AuthError(RuntimeError):
    """Token rejected by Discord (401). Caller must stop and ask the human
    to rotate the token — there is no refresh flow for user tokens."""


class StateUnwritableError(RuntimeError):
    """State directory or file can't be written.

    Treated as a terminal condition by cmd_run — we park instead of
    crash-looping, because docker's `restart: unless-stopped` would
    otherwise spin us forever against Discord with no checkpointing.
    """


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


def _net_sleep(seconds: float) -> None:
    """Sleep up to `seconds` in <=1s slices, waking early when STOP is set."""
    slept = 0.0
    while slept < seconds and not STOP:
        time.sleep(min(1.0, seconds - slept))
        slept += 1.0


def _request(s: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """HTTP with bounded retry on transient connection failures.

    Dispatches to s.get / s.delete so the per-verb session method stays
    the boundary the tests mock. Retries ONLY connection-level errors
    (DNS resolution failure, connection reset, read timeout). An HTTP
    response of ANY status is returned untouched, because 4xx/5xx carry
    semantics the callers decode (401->AuthError, 429->retry hint,
    403->forbidden) and must not be swallowed here. Re-raises once the
    retry budget is spent or STOP is set, so a sustained outage still
    surfaces. See NET_RETRY_* for the host-reboot DNS-not-ready rationale.
    """
    fn = s.get if method == "GET" else s.delete
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if STOP or attempt >= NET_RETRY_MAX:
                raise
            delay = min(NET_RETRY_CAP, NET_RETRY_BASE * (2 ** (attempt - 1)))
            print(
                f"[net] transient {type(e).__name__} on {method} {url} "
                f"(attempt {attempt}/{NET_RETRY_MAX}); retrying in {delay:.0f}s",
                file=sys.stderr,
            )
            _net_sleep(delay)


def get_me(s: requests.Session) -> dict:
    r = _request(s, "GET", f"{API}/users/@me", timeout=30)
    _check_auth(r)
    r.raise_for_status()
    return r.json()


def list_my_guilds(s: requests.Session) -> list[dict]:
    r = _request(s, "GET", f"{API}/users/@me/guilds", timeout=30)
    _check_auth(r)
    r.raise_for_status()
    return r.json()


def list_my_dms(s: requests.Session) -> list[dict]:
    r = _request(s, "GET", f"{API}/users/@me/channels", timeout=30)
    _check_auth(r)
    r.raise_for_status()
    return r.json()


def search_messages(
    s: requests.Session,
    *,
    scope: str,  # "guild" or "channel"
    scope_id: str,
    author_id: str,
    max_id: int = 0,
    min_id: int = 0,
    offset: int = 0,
    content: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> tuple[Optional[int], list[dict], Optional[float]]:
    """Search returning (total_results, hit_messages, retry_after_secs).

    On 429, returns (None, [], retry_after). On 403/404, returns (-1, [], None) — caller skips scope.
    Otherwise raises on hard errors.

    Optional params:
      content:     text to search for (Discord's undocumented content filter)
      channel_id:  limit guild search to a specific channel
      min_id:      snowflake lower bound (messages after this)
      max_id:      snowflake upper bound (messages before this; default 0 = unbounded)
    """
    if scope == "guild":
        url = f"{API}/guilds/{scope_id}/messages/search"
    else:
        url = f"{API}/channels/{scope_id}/messages/search"

    params: dict[str, str] = {
        "author_id": author_id,
        "offset": str(offset),
        "include_nsfw": "true",
    }
    if max_id:
        params["max_id"] = str(max_id)
    if min_id:
        params["min_id"] = str(min_id)
    if content:
        params["content"] = content
    if channel_id and scope == "guild":
        params["channel_id"] = channel_id
    r = _request(s, "GET", url, params=params, timeout=60)
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


def _bucket_hint(r: requests.Response) -> float:
    """Optimal per-call sleep from Discord's per-bucket rate-limit headers.

    Discord publishes the same headers on EVERY response that hit a
    rate-limited route (204, 404, 429 all carry them when applicable):
      X-RateLimit-Limit         max requests in this bucket window
      X-RateLimit-Remaining     requests left in current window
      X-RateLimit-Reset-After   seconds until the bucket refills
      X-RateLimit-Bucket        opaque bucket id

    Optimal pacing = Reset-After / Remaining — spreads the quota evenly
    so we never tip into a 429. If Remaining is 0 we wait the full
    window. The caller takes max(DELETE_DELAY, hint) so DELETE_DELAY
    remains a safety floor against account-level abuse detection
    (which is independent of per-route buckets).

    Returns 0.0 if no headers present — caller falls back to its floor.
    """
    rem_s = r.headers.get("X-RateLimit-Remaining", "")
    reset = float(r.headers.get("X-RateLimit-Reset-After", "0") or 0)
    try:
        rem = int(rem_s) if rem_s != "" else -1
    except ValueError:
        rem = -1
    if rem == 0 and reset > 0:
        return reset
    if rem > 0 and reset > 0:
        return reset / rem
    return 0.0


def delete_message(s: requests.Session, channel_id: str, msg_id: str) -> tuple[str, float]:
    """Delete one message.

    Returns (status, sleep_hint_seconds):
      'ok'        — deleted (204). hint = bucket-derived optimal pace.
      'gone'      — already deleted (404). hint = bucket-derived pace too —
                    Discord bills 404 against the same bucket, so a
                    stream of 404s (e.g. re-running a wipe whose state
                    was lost) gets the same pacing signal as 204s.
      'forbidden' — system message / not yours / channel revoked (403).
      'retry'     — caller should sleep `sleep_hint` and try again,
                    AND treat sleep_hint as a persistent pacing floor
                    until a 204/404 refreshes the bucket estimate.
    """
    url = f"{API}/channels/{channel_id}/messages/{msg_id}"
    r = _request(s, "DELETE", url, timeout=30)
    _check_auth(r)

    if r.status_code == 204:
        return "ok", _bucket_hint(r)

    if r.status_code == 404:
        return "gone", _bucket_hint(r)

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


def _format_duration(seconds: float) -> str:
    """Human-friendly duration string for logs (e.g. '3d15h8m', '22h13m',
    '4m12s'). Used for both ETA progress lines and the pass-complete
    elapsed readout, so it must stay readable from seconds up to days —
    the initial full-history drain can legitimately run multiple days."""
    if seconds < 0 or seconds != seconds:  # NaN-safe
        return "?"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds // 60:.0f}m{seconds % 60:.0f}s"
    if seconds < 86400:
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        return f"{hours:.0f}h{mins:.0f}m"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    mins = (seconds % 3600) // 60
    return f"{days:.0f}d{hours:.0f}h{mins:.0f}m"


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
    # Parse every channel's messages.json ONCE up-front. Used both for
    # the resume summary / ETA pre-count below and for the main loop —
    # no double-parse, no inconsistent error handling between phases.
    parsed: dict[str, list[dict]] = {}
    for ch in channels:
        try:
            parsed[ch.id] = json.loads(ch.msgs_path.read_text())
        except FileNotFoundError:
            print(f"[export] {ch.id}: no messages.json — skip", file=sys.stderr)
        except json.JSONDecodeError as e:
            print(f"[export] {ch.id}: corrupt messages.json ({e}) — skip", file=sys.stderr)

    # Resume summary: how much was already done, how much is still
    # left, and how many channels are fully done so the resume is
    # auditable from the logs alone.
    grand_total = 0
    already_done_total = 0
    fully_done_channels = 0
    for ch in channels:
        msgs = parsed.get(ch.id)
        if msgs is None:
            continue
        grand_total += len(msgs)
        try:
            ch_done = sum(1 for m in msgs if str(m["ID"]) in cfg.state.deleted)
        except (KeyError, TypeError):
            ch_done = 0
        already_done_total += ch_done
        if msgs and ch_done == len(msgs):
            fully_done_channels += 1
    grand_done = already_done_total
    remaining = grand_total - already_done_total
    t0 = time.monotonic()
    deletes_since_t0 = 0
    resume_pct = (100.0 * already_done_total / grand_total) if grand_total else 0.0
    print(
        f"[export] resume: {already_done_total}/{grand_total} "
        f"({resume_pct:.1f}%) already done "
        f"across {fully_done_channels}/{len(channels)} fully-done channels; "
        f"{remaining} targets remaining"
    )

    for ci, ch in enumerate(channels, 1):
        if STOP:
            break
        if ch.id in cfg.exclude_channels:
            print(f"[export {ci}/{len(channels)}] skip excluded {ch.id}")
            continue

        msgs = parsed.get(ch.id)
        if msgs is None:
            # Already logged above during pre-parse.
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

        # extra_sleep persists across messages so a 429's retry_after
        # becomes a sustained pacing floor until a 204/404 with fresh
        # bucket headers refreshes the estimate. Pre-v0.3.2 reset it
        # to 0.0 per message, which caused a 404-heavy stream (e.g.
        # re-running a wipe whose state was lost) to cycle
        # 429→retry→404→floor→429 forever at ~18/min.
        #
        # floor_429 (v0.4.3) is the stickier of the two: the freshest
        # 429 retry_after. A clean 204/404 carries the NORMAL per-channel
        # delete bucket headers (which have plenty of headroom → a tiny
        # hint), but the 429s we actually hit come from Discord's SEPARATE,
        # stricter old-message (>14d) delete sub-limit. Letting the small
        # normal-bucket hint overwrite extra_sleep collapsed the pace back
        # to ~delete_delay, so the very next delete re-tripped the old-msg
        # limit — a 429 on EVERY message (~16/min, observed live
        # 2026-06-08). Clamping clean hints to >= floor_429 keeps the pace
        # at the old-msg interval so we stop storming 429s.
        extra_sleep = 0.0
        floor_429 = 0.0
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

            status = "retry"
            while True:
                status, hint = delete_message(s, ch.id, mid)
                if status == "retry":
                    # Quiet log: routine sub-second 429s from bucket
                    # edge are expected and floodful; only print the
                    # noteworthy ones (≥1s) and 5xx retries.
                    if hint >= 1.0:
                        print(f"  rate-limited; sleep {hint:.1f}s", file=sys.stderr)
                    # 429's retry_after IS Discord's freshest bucket
                    # signal — overwrite the floor with it, do NOT max.
                    # max() ratcheted the floor monotonically upward:
                    # a transient 2.3s retry early on trapped pacing
                    # at 2.3s forever even when subsequent 429s reported
                    # the bucket had relaxed to 1.0s. Observed cost in
                    # 2026-05-28 v0.3.2 deploy: ~10/min throughput loss.
                    # floor_429 tracks the same value but is sticky across
                    # clean responses (see below); a smaller 429 relaxes
                    # both, preserving the v0.3.3 relax-down behaviour.
                    extra_sleep = hint
                    floor_429 = hint
                    time.sleep(hint)
                    if STOP:
                        break
                    continue
                if status in ("ok", "gone") and hint > 0:
                    # Header-derived pacing CAN relax the floor when the
                    # normal bucket has refilled, but never below the
                    # freshest old-message 429 interval — otherwise the
                    # next delete re-trips the old-msg sub-limit and we
                    # 429 on every message. When hint == 0 (no headers on
                    # this response) KEEP the prior floor.
                    extra_sleep = max(hint, floor_429)
                break

            if status == "retry":
                # SIGTERM fired mid-backoff. Do NOT mark — the next
                # pass's catchup phase will pick this ID up again
                # via messages/search. Marking now would silently
                # leak the message (state says done, Discord still
                # has it). Break the outer loop too; STOP is set.
                break

            if status == "ok":
                counters["ok"] += 1
                METRICS.inc("ok")
            elif status == "gone":
                counters["gone"] += 1
                METRICS.inc("gone")
            elif status == "forbidden":
                counters["forbidden"] += 1
                METRICS.inc("forbidden")
            METRICS.extra_sleep_export = extra_sleep

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
                    f"~{rate * 60:.0f}/min ETA {_format_duration(eta)}"
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


def phase_live_catchup(
    s: requests.Session,
    cfg: WipeConfig,
    targets_override: list[tuple[str, str, str]] | None = None,
) -> None:
    """Search-API sweep across every live guild + open DM.

    When *targets_override* is provided the guild/DM enumeration is
    skipped entirely and the caller's target list is used directly.
    Each entry is ``(scope, scope_id, label)`` where *scope* is
    ``'guild'`` or ``'channel'``.
    """
    cutoff_snowflake = snowflake_at(cfg.cutoff)

    if targets_override is not None:
        targets: list[tuple[str, str, str]] = targets_override
        print(
            f"[catchup] {len(targets)} targeted scope(s); "
            f"cutoff={cfg.cutoff.isoformat()} (snowflake={cutoff_snowflake})"
        )
    else:
        guilds = list_my_guilds(s)
        dms = list_my_dms(s)
        print(
            f"[catchup] {len(guilds)} guilds, {len(dms)} DM channels; "
            f"cutoff={cfg.cutoff.isoformat()} (snowflake={cutoff_snowflake})"
        )
        targets = []
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

    counters = {"ok": 0, "gone": 0, "forbidden": 0}

    for ti, (scope, scope_id, label) in enumerate(targets, 1):
        if STOP:
            break
        print(f"[catchup {ti}/{len(targets)}] {label}")

        # extra_sleep is hoisted to scope-level so the pacing floor
        # survives across pages within a scope. See phase_export for
        # the full rationale (incl. the v0.4.3 floor_429 clamp that
        # stops the old-message sub-limit from 429ing every message).
        # Reset per-scope because Discord may bucket scopes independently.
        extra_sleep = 0.0
        floor_429 = 0.0
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

                status = "retry"
                while True:
                    status, hint = delete_message(s, cid, mid)
                    if status == "retry":
                        print(f"    rate-limited; sleep {hint:.1f}s")
                        # Overwrite, not max — see phase_export for the
                        # v0.3.2→v0.3.3 rationale (max trapped the floor
                        # at historical high-water mark). floor_429 is the
                        # sticky old-msg interval clean hints can't undercut.
                        extra_sleep = hint
                        floor_429 = hint
                        time.sleep(hint)
                        if STOP:
                            break
                        continue
                    if status in ("ok", "gone") and hint > 0:
                        # Relax toward the normal bucket but never below
                        # the freshest old-message 429 interval — see
                        # phase_export. hint == 0 keeps the prior floor.
                        extra_sleep = max(hint, floor_429)
                    break

                if status == "retry":
                    # SIGTERM mid-backoff. Same reasoning as the
                    # export phase: do NOT mark. The next pass will
                    # find the message again via search.
                    break

                if status == "ok":
                    counters["ok"] += 1
                    METRICS.inc("ok")
                elif status == "gone":
                    counters["gone"] += 1
                    METRICS.inc("gone")
                elif status == "forbidden":
                    counters["forbidden"] += 1
                    METRICS.inc("forbidden")
                METRICS.extra_sleep_catchup = extra_sleep

                cfg.state.mark(mid)
                # max(floor, hint), NOT sum — same rationale as the
                # export phase. DELETE_DELAY is the safety floor;
                # extra_sleep is the per-bucket optimal pace.
                time.sleep(max(cfg.delete_delay, extra_sleep))

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


def _format_age(iso: Optional[str]) -> str:
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s ago"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h ago"


def cmd_status(args) -> int:
    """Print a summary of the on-disk state file.

    Doesn't touch Discord — safe to run while a wipe is in progress.
    Reads the same state.json the running container writes; the
    `atomic .tmp + rename` save pattern means we always see a
    consistent snapshot.
    """
    path = args.state
    if not path.exists():
        print(f"no state file at {path}")
        return 1
    size_kb = path.stat().st_size / 1024
    try:
        d = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"state.json is unreadable: {e}")
        backups = sorted(path.parent.glob(f"{path.name}.corrupt-*"))
        if backups:
            print(f"  backup files present: {[b.name for b in backups]}")
        return 2

    n_deleted = len(d.get("deleted", []))
    consumed = bool(d.get("export_consumed", False))
    last_pass = d.get("last_pass_at")
    last_start = d.get("last_started_at")
    burst = int(d.get("restart_burst", 0) or 0)

    print(f"state file:       {path} ({size_kb:.1f} KB)")
    print(f"deleted IDs:      {n_deleted:,}")
    print(f"export consumed:  {consumed}")
    print(f"last pass:        {last_pass or 'never'} ({_format_age(last_pass)})")
    print(f"last start:       {last_start or 'never'} ({_format_age(last_start)})")
    print(f"restart_burst:    {burst}")

    hb = path.with_name("heartbeat")
    if hb.exists():
        hb_age = time.time() - hb.stat().st_mtime
        print(f"heartbeat:        {hb} ({hb_age:.0f}s ago)")
    else:
        print("heartbeat:        (none)")

    backups = sorted(path.parent.glob(f"{path.name}.corrupt-*"))
    if backups:
        print(f"corrupt backups:  {len(backups)} ({backups[-1].name} most recent)")

    if n_deleted:
        sample = sorted(d.get("deleted", []))[:3]
        print(f"sample IDs:       {sample}")
    return 0


def cmd_seed_from_export(args) -> int:
    """Mark every export message OLDER than the cutoff as already-deleted.

    Recovery / fast-forward tool — no Discord API calls, purely local.

    After a state loss (e.g. the pre-0.4.3 0-byte `state.json`
    truncation), a from-scratch `run` re-issues DELETE on every message
    in the export. If a prior pass already deleted them, each one comes
    back 404 'gone' — but Discord still bills it against the punishing
    old-message (>14d) delete rate limit (~16/min, ETA days for a 100k
    backlog). When you KNOW a previous run completed the wipe, this
    seeds `state.deleted` with the export's old-message IDs and flips
    `export_consumed=True`, so the daemon skips that pointless re-grind.

    Safety: only messages strictly OLDER than the cutoff are seeded;
    anything newer stays unmarked, so the live catchup phase still
    deletes it once it ages past retention. This tool does NOT verify
    the messages are actually gone — only run it when you're confident a
    prior pass deleted them (e.g. the logs showed a completed wipe
    before the state was lost). The existing deleted set + restart
    counters are preserved (union, not replace).
    """
    state = State(args.state)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.retention_days)
    before = len(state.deleted)
    print(
        f"[seed] state={args.state}: {before} IDs already marked, "
        f"export_consumed={state.export_consumed}"
    )
    print(f"[seed] cutoff={cutoff.isoformat()} (retention={args.retention_days}d)")

    channels = read_export(args.export_dir)
    print(f"[seed] {len(channels)} channels in export")

    seeded = 0
    skipped_recent = 0
    channels_seen = 0
    for ch in channels:
        try:
            msgs = json.loads(ch.msgs_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"[seed] {ch.id}: skip ({e})", file=sys.stderr)
            continue
        channels_seen += 1
        for m in msgs:
            if not isinstance(m, dict):
                continue
            try:
                mid = str(m["ID"])
            except (KeyError, TypeError):
                continue
            ts_raw = m.get("Timestamp")
            old = True
            if ts_raw is not None:
                try:
                    # Same rule phase_export uses: a parseable timestamp
                    # >= cutoff is 'recent' and must NOT be seeded; an
                    # unparseable one is treated as a delete target (old).
                    old = parse_export_ts(ts_raw) < cutoff
                except Exception:
                    old = True
            if not old:
                skipped_recent += 1
                continue
            if mid not in state.deleted:
                state.mark(mid)
                seeded += 1

    state.export_consumed = True
    state.save()
    print(
        f"[seed] channels={channels_seen}; seeded {seeded} new IDs "
        f"(was {before:,}, now {len(state.deleted):,}); "
        f"left {skipped_recent:,} recent (>= cutoff) unmarked; export_consumed=True"
    )
    print(
        "[seed] done — the next pass skips the export grind; live catchup "
        "still deletes anything that has since aged past retention."
    )
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


def _park_until_sigterm(banner: str, reason: str) -> int:
    """Print FATAL banner, fire the notify-on-park webhook if configured,
    flag METRICS as parked, and sleep in 5s chunks until SIGTERM.

    Shared base for every "park" exit path (401 / identity / state /
    restart-burst). Returns 0 — docker treats that as a clean exit AND
    `restart: unless-stopped` does NOT re-spawn on clean exits, so the
    container stays as it is until the operator intervenes.
    """
    print("\n" + banner + "\n" + "=" * 72, file=sys.stderr, flush=True)
    METRICS.parked = True
    METRICS.park_reason = reason
    _notify_park(reason, banner)
    while not STOP:
        time.sleep(5)
    return 0


def _auth_paused_exit(token_hint: str, reason: str) -> int:
    """Discord rejected our token (401) or the identity changed.

    Critical safety: do NOT exit non-zero in a tight loop. With Docker's
    `restart: unless-stopped` policy that would re-launch us immediately,
    hitting Discord again with the same dead token — a fast track to
    abuse-flagging the account. Park indefinitely; `docker compose up -d`
    with a new .env sends us SIGTERM.
    """
    banner = (
        "=" * 72 + "\n"
        "[FATAL] DISCORD TOKEN REJECTED.\n\n"
        f"reason: {reason}\n"
        f"token: {token_hint}\n\n"
        "Discord user tokens have NO refresh flow. Causes:\n"
        "  - You logged out / logged back in (issues a new token).\n"
        "  - You changed your password.\n"
        "  - Discord rotated it (suspected abuse / token-theft scanner).\n\n"
        "To rotate:\n"
        "  1. Grab the new Authorization header from DevTools.\n"
        "  2. Edit /mnt/user/composer/stacks/discord-wipe/.env on servarr.\n"
        "  3. `docker compose up -d` (recreates the container with the\n"
        "     new env, sending us a graceful SIGTERM).\n\n"
        "Sleeping until SIGTERM. Container stays alive but idle so\n"
        "restart-unless-stopped doesn't spin and the dashboard shows\n"
        "a clear cause."
    )
    return _park_until_sigterm(banner, "token-rejected")


def _state_unwritable_exit(detail: str) -> int:
    """State directory or file can't be written. Park until SIGTERM.

    Without this, an unwritable state path (FS full, mount frozen, perms
    wrong) would crash the script every pass and `restart: unless-stopped`
    would respawn us into a hot loop. Park so the operator notices.
    """
    banner = (
        "=" * 72 + "\n"
        "[FATAL] STATE FILE UNWRITABLE.\n\n"
        f"reason: {detail}\n\n"
        "Common causes:\n"
        "  - The bind-mount target doesn't exist on the host.\n"
        "  - The host disk hosting /mnt/user/discord-wipe/state is full.\n"
        "  - The filesystem has frozen (Unraid shfs / array degraded).\n"
        "  - Permissions are wrong (state dir should be 99:100 on Unraid).\n\n"
        "To recover:\n"
        "  1. ssh servarr 'ls -la /mnt/user/discord-wipe/state/'\n"
        "  2. Fix ownership / free space / unfreeze the filesystem.\n"
        "  3. `docker compose up -d` to recreate the container.\n\n"
        "Sleeping until SIGTERM."
    )
    return _park_until_sigterm(banner, "state-unwritable")


def _restart_burst_exit(count: int) -> int:
    """Container has restarted >RESTART_BURST_MAX times in the last
    RESTART_BURST_WINDOW seconds. Park instead of looping further.

    A broken `:main` image or a config-level bug would otherwise spin
    docker forever; this guard catches it and surfaces the cause.
    """
    banner = (
        "=" * 72 + "\n"
        "[FATAL] RESTART BURST DETECTED.\n\n"
        f"This container has started {count} times within the last "
        f"{RESTART_BURST_WINDOW}s. Something is crashing it on startup "
        f"and `restart: unless-stopped` keeps respawning it.\n\n"
        "Recent crashes are visible via:\n"
        "  ssh servarr 'docker logs --tail 200 discord-wipe'\n\n"
        "Common causes:\n"
        "  - A broken release on :main — pin DISCORD_WIPE_TAG=<previous-sha>.\n"
        "  - Token missing / file empty.\n"
        "  - Required path doesn't exist (export/ or state/).\n\n"
        "Sleeping until SIGTERM. Reset the burst counter by editing\n"
        "`restart_burst: 0` in state.json after fixing the cause."
    )
    return _park_until_sigterm(banner, "restart-burst")


def _token_hint(token: str) -> str:
    """Safe-to-log token fingerprint (never the secret itself)."""
    if not token:
        return "(empty)"
    return f"{token[:6]}...{token[-4:]} (len={len(token)})"


def cmd_run(args) -> int:
    install_signal_handlers()
    print(f"[run] discord-wipe v{__version__}")

    # Construct State first — it can raise StateUnwritableError if the
    # bind-mount is missing or unwritable. Catch and park instead of
    # crashing into the restart loop.
    try:
        state = State(args.state)
    except StateUnwritableError as e:
        return _state_unwritable_exit(str(e))

    # Restart-burst guard. If we've started >RESTART_BURST_MAX times in
    # the last RESTART_BURST_WINDOW seconds, something is crashing us on
    # startup. Park rather than keep spinning.
    state.record_start()
    if state.restart_burst > RESTART_BURST_MAX:
        return _restart_burst_exit(state.restart_burst)
    try:
        state.save()
    except StateUnwritableError as e:
        return _state_unwritable_exit(str(e))

    # Wire metrics: State for gauges, server for /metrics.
    METRICS.state = state
    if METRICS_ENABLED:
        _start_metrics_server(METRICS, METRICS_BIND)

    s = make_session(args.token)
    try:
        me = get_me(s)
    except AuthError as e:
        return _auth_paused_exit(_token_hint(args.token), str(e))
    print(f"[run] authenticated as @{me.get('username')} (id={me['id']})")

    # We authenticated — network, token, and identity are all healthy,
    # which rules out every crash-loop cause the restart-burst guard
    # defends against (all of them manifest before this line). Clear the
    # counter so a past transient blip doesn't leave the guard primed to
    # false-fire on the next unrelated restart.
    if state.restart_burst:
        state.restart_burst = 0
        with contextlib.suppress(StateUnwritableError):
            state.save()

    print(
        f"[run] state: {args.state} ({len(state.deleted)} IDs already done; "
        f"export_consumed={state.export_consumed}; restart_burst={state.restart_burst})"
    )

    while True:
        # Pre-flight: catch token rotation between passes before doing
        # expensive work. Costs one HTTP call per pass; negligible.
        # Also detect identity change — a rotation that lands on a
        # DIFFERENT account would silently search for the original
        # user's messages under the new user's permissions. Bail.
        try:
            fresh = get_me(s)
        except AuthError as e:
            return _auth_paused_exit(_token_hint(args.token), str(e))
        if fresh["id"] != me["id"]:
            return _auth_paused_exit(
                _token_hint(args.token),
                f"identity changed mid-loop: was @{me.get('username')} "
                f"(id={me['id']}), now @{fresh.get('username')} (id={fresh['id']})",
            )

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
        METRICS.last_pass_start = t0
        METRICS.parked = False

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

        except StateUnwritableError as e:
            return _state_unwritable_exit(str(e))

        except requests.HTTPError as e:
            print(f"[run] HTTP error: {e} {getattr(e.response, 'text', '')[:300]}", file=sys.stderr)

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            # A sustained outage outlasted the per-call retry budget. End
            # the pass cleanly; the between-pass sleep rides it out and the
            # next pass re-attempts. Crashing here would feed docker's
            # restart loop / burst guard for a self-healing condition.
            print(f"[run] network error (outage outlasted retries): {e}", file=sys.stderr)

        state.last_pass_at = datetime.now(timezone.utc).isoformat()
        try:
            state.save()
        except StateUnwritableError as e:
            return _state_unwritable_exit(str(e))
        elapsed = time.time() - t0
        METRICS.last_pass_end = time.time()
        # Human-readable up front (the initial drain can run for days),
        # raw seconds in parens so the line stays greppable/parseable.
        print(f"[run] === pass complete in {_format_duration(elapsed)} ({elapsed:.0f}s) ===")

        if STOP:
            print("[run] stop signal — exiting")
            return 0
        if not args.watch:
            return 0

        sleep_for = args.interval_hours * 3600
        wake_at = datetime.now(timezone.utc) + timedelta(seconds=sleep_for)
        print(f"[run] sleeping {sleep_for}s; next pass at {wake_at.isoformat()}")
        # Sleep in small chunks so SIGTERM responds quickly. Touch the
        # heartbeat every minute so HEALTHCHECK stays green across the
        # long inter-pass sleep.
        slept = 0.0
        last_heartbeat = time.time()
        while slept < sleep_for and not STOP:
            time.sleep(min(5.0, sleep_for - slept))
            slept += 5.0
            if time.time() - last_heartbeat >= 60:
                state.touch_heartbeat()
                last_heartbeat = time.time()


def cmd_purge(args) -> int:
    """One-shot targeted wipe — delete all your messages in specific guilds/channels.

    Unlike ``run``, this command:
    - Requires explicit ``--guild`` / ``--channel`` targets (no accidental
      full-account wipe).
    - Defaults ``--retention-days`` to **0** so ALL messages (including recent
      ones) in the target scope are deleted.
    - Skips the export phase entirely; only the live search-API sweep runs.
    - Never loops (``--watch`` is not available).

    Examples::

        # Delete everything you ever posted in server 123456789012345678
        discord-wipe purge --guild 123456789012345678

        # Dry-run first — see what would be deleted
        discord-wipe purge --guild 123456789012345678 --dry-run

        # Wipe two servers at once
        discord-wipe purge --guild 111 --guild 222

        # Wipe a single channel/DM thread
        discord-wipe purge --channel 987654321098765432

        # Keep messages newer than 7 days
        discord-wipe purge --guild 123456789012345678 --retention-days 7
    """
    install_signal_handlers()
    print(f"[purge] discord-wipe v{__version__}")

    guilds: list[str] = args.guild or []
    channels: list[str] = args.channel or []
    if not guilds and not channels:
        print(
            "ERROR: specify at least one --guild GUILD_ID or --channel CHANNEL_ID",
            file=sys.stderr,
        )
        return 2

    try:
        state = State(args.state)
    except StateUnwritableError as e:
        return _state_unwritable_exit(str(e))

    s = make_session(args.token)
    try:
        me = get_me(s)
    except AuthError as e:
        return _auth_paused_exit(_token_hint(args.token), str(e))
    print(f"[purge] authenticated as @{me.get('username')} (id={me['id']})")
    print(
        f"[purge] state: {args.state} ({len(state.deleted)} IDs already done; "
        f"export_consumed={state.export_consumed})"
    )

    # retention_days=0 → cutoff=now → delete everything in scope
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.retention_days)
    cfg = WipeConfig(
        token=args.token,
        me_id=me["id"],
        state=state,
        cutoff=cutoff,
        delete_delay=args.delete_delay,
        search_delay=args.search_delay,
        dry_run=args.dry_run,
        exclude_guilds=set(),
        exclude_channels=set(),
    )

    targets: list[tuple[str, str, str]] = []
    for gid in guilds:
        targets.append(("guild", gid, f"guild:{gid}"))
    for cid in channels:
        targets.append(("channel", cid, f"channel:{cid}"))

    print(
        f"[purge] targets: {[label for _, _, label in targets]}\n"
        f"[purge] cutoff: {cutoff.isoformat()} "
        f"(retention_days={args.retention_days})" + (" — DRY RUN" if args.dry_run else "")
    )

    t0 = time.time()
    try:
        phase_live_catchup(s, cfg, targets_override=targets)
    except AuthError as e:
        return _auth_paused_exit(_token_hint(args.token), str(e))
    except StateUnwritableError as e:
        return _state_unwritable_exit(str(e))
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        print(f"[purge] network error (outage outlasted retries): {e}", file=sys.stderr)

    state.last_pass_at = datetime.now(timezone.utc).isoformat()
    try:
        state.save()
    except StateUnwritableError as e:
        return _state_unwritable_exit(str(e))
    elapsed = time.time() - t0
    print(f"[purge] === done in {_format_duration(elapsed)} ({elapsed:.0f}s) ===")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_search(args) -> int:
    """Search your messages across servers/DMs and preview what you'd delete.

    Designed as the discovery step before ``purge`` — search for messages
    matching a keyword (or just browse recent history), see channel + timestamp
    + content preview, then decide whether to nuke the scope.

    Examples::

        # Find messages containing "bad phrase" in a server
        discord-wipe search --guild 123456789 --content "bad phrase"

        # Browse recent messages in a DM
        discord-wipe search --channel 987654321

        # Search everywhere for a keyword
        discord-wipe search --content "old project name"

        # Narrow by date range
        discord-wipe search --guild 123456789 --before 2024-06-01 --after 2024-01-01
    """
    install_signal_handlers()
    s = make_session(args.token)
    try:
        me = get_me(s)
    except AuthError as e:
        print(f"FAIL — {e}", file=sys.stderr)
        return 2

    guilds: list[str] = args.guild or []
    channels: list[str] = args.channel or []

    # Build search scope list: (scope_type, scope_id, label, channel_filter)
    scopes: list[tuple[str, str, str, Optional[str]]] = []

    if channels or guilds:
        # Resolve DM recipient names for channel scopes
        dms: dict[str, dict] = {}
        if channels:
            try:
                dms = {c["id"]: c for c in list_my_dms(s)}
            except Exception:
                pass
        for cid in channels:
            dm = dms.get(cid)
            if dm:
                recips = ",".join(r.get("username", "?") for r in dm.get("recipients", []))
                label = f"DM:{recips}"
            else:
                label = f"channel:{cid}"
            scopes.append(("channel", cid, label, None))
        for gid in guilds:
            try:
                r = _request(s, "GET", f"{API}/guilds/{gid}", timeout=10)
                r.raise_for_status()
                gname = r.json().get("name", gid)
            except Exception:
                gname = gid
            scopes.append(("guild", gid, f"{gname}", args.channel_filter))
    else:
        # Search everywhere — enumerate guilds + DMs
        all_guilds: list[dict] = []
        all_dms: list[dict] = []
        try:
            all_guilds = list_my_guilds(s)
        except Exception as e:
            print(f"[warn] could not list guilds: {e}", file=sys.stderr)
        try:
            all_dms = list_my_dms(s)
        except Exception as e:
            print(f"[warn] could not list DMs: {e}", file=sys.stderr)
        for g in all_guilds:
            scopes.append(("guild", g["id"], g.get("name", g["id"]), None))
        for c in all_dms:
            recips = ",".join(r.get("username", "?") for r in c.get("recipients", []))
            scopes.append(("channel", c["id"], f"DM:{recips}", None))

    if not scopes:
        print("no scopes to search — are you in any servers or DMs?")
        return 1

    # Prefetch guild channels for name resolution
    guild_channels: dict[str, dict[str, str]] = {}  # guild_id -> {channel_id: name}
    for scope_type, scope_id, label, _ in scopes:
        if scope_type == "guild" and scope_id not in guild_channels:
            try:
                r = _request(s, "GET", f"{API}/guilds/{scope_id}/channels", timeout=15)
                r.raise_for_status()
                guild_channels[scope_id] = {
                    c["id"]: f"#{c.get('name', c['id'])}" for c in r.json()
                }
            except Exception:
                guild_channels[scope_id] = {}

    # Compute snowflake bounds from date args.
    # max_id=0 means "Discord epoch" (2015-01-01) which returns nothing —
    # default to "now" so the search isn't silently empty when --before is omitted.
    max_id = snowflake_at(args.before) if args.before else snowflake_at(datetime.now(timezone.utc))
    min_id = snowflake_at(args.after) if args.after else 0

    # Header
    parts = []
    if args.content:
        parts.append(f'content="{args.content}"')
    else:
        parts.append("recent messages")
    if guilds:
        parts.append(f"{len(guilds)} server(s)")
    elif channels:
        parts.append(f"{len(channels)} channel(s)")
    else:
        parts.append(f"{len(scopes)} scopes")
    print(f"Search: {'; '.join(parts)}")
    print(f"  authenticated as @{me.get('username')}")
    if args.before:
        print(f"  before: {args.before.isoformat()}")
    if args.after:
        print(f"  after:  {args.after.isoformat()}")
    print()

    grand_total = 0
    for scope_type, scope_id, label, channel_filter in scopes:
        if STOP:
            break

        total, hits, retry = search_messages(
            s,
            scope=scope_type,
            scope_id=scope_id,
            author_id=me["id"],
            max_id=max_id,
            min_id=min_id,
            content=args.content,
            channel_id=channel_filter,
            offset=0,
        )

        status_icon = ""
        if total is None:
            if retry:
                print(f"[{label}] ⏳ rate-limited (retry in {retry:.0f}s)\n")
            else:
                print(f"[{label}] ⏳ search index not ready\n")
            continue
        if total == -1:
            print(f"[{label}] 🔒 no permission\n")
            continue
        if not hits:
            print(f"[{label}] (no matches)\n")
            continue

        if total is not None and total > 0:
            status_icon = f" ({len(hits)} shown of {total} total)"

        print(f"[{label}]{status_icon}")

        channels_map = guild_channels.get(scope_id, {}) if scope_type == "guild" else {}
        for m in hits:
            mid = str(m["id"])
            cid = str(m["channel_id"])
            ch_name = channels_map.get(cid, cid)

            # Extract content. Discord search returns content as a string
            # on the hit object, but handle edge cases defensively.
            raw_content = m.get("content", "")
            if isinstance(raw_content, list):
                raw_content = " ".join(
                    part.get("content", "") if isinstance(part, dict) else str(part)
                    for part in raw_content
                )
            content = str(raw_content).replace("\n", "\\n")
            if len(content) > 140:
                content = content[:137] + "..."
            if not content:
                content = "[attachment / embed]"

            ts = snowflake_to_dt(int(mid))
            ts_str = ts.strftime("%Y-%m-%d %H:%M UTC")

            print(f"  [{ch_name}] {ts_str}")
            print(f"    {content}")

        grand_total += len(hits)
        print()

    print(f"--- {grand_total} matches shown ---")
    if guilds:
        ids = " ".join(f"--guild {g}" for g in guilds)
        print(f"\nTo delete all:  discord-wipe purge {ids}")
    elif channels:
        ids = " ".join(f"--channel {c}" for c in channels)
        print(f"\nTo delete all:  discord-wipe purge {ids}")
    else:
        print("\nTo delete from a server:  discord-wipe purge --guild <ID>")
        print("To delete from a channel: discord-wipe purge --channel <ID>")

    return 0


def _parse_date_arg(s: str) -> datetime:
    """Parse a date string like '2024-06-01' into a UTC datetime.

    Accepts ISO date (YYYY-MM-DD) or ISO datetime with optional timezone.
    Returns a timezone-aware UTC datetime.
    """
    s = s.strip()
    # Try full ISO datetime first
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    # Try date-only
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date '{s}': expected YYYY-MM-DD or ISO datetime"
        )


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

    p = sub.add_parser("status", help="read state.json and print a summary (no API calls)")
    p.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "seed-from-export",
        help="mark export messages older than the cutoff as already-deleted "
        "and set export_consumed=True (recovery; no API calls)",
    )
    p.add_argument("--export-dir", type=pathlib.Path, default=DEFAULT_EXPORT)
    p.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    p.add_argument(
        "--retention-days",
        type=float,
        default=float(os.environ.get("RETENTION_DAYS", "14")),
        help="messages older than this are seeded as deleted (default 14, env RETENTION_DAYS)",
    )
    p.set_defaults(func=cmd_seed_from_export)

    p = sub.add_parser("run", help="run wipe pass(es)")
    p.add_argument("--export-dir", type=pathlib.Path, default=DEFAULT_EXPORT)
    p.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    p.add_argument(
        "--retention-days",
        type=float,
        default=float(os.environ.get("RETENTION_DAYS", "14")),
        help="messages older than this are deleted (default 14, env RETENTION_DAYS)",
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
        default=float(os.environ.get("SEARCH_DELAY", "15.0")),
        help="seconds between search-API page fetches; long enough for "
        "the search index to refresh between deletes (default 15; "
        "v0.3.x default was 30, halved in v0.4.0 — Discord's index "
        "refreshes in ~5-10s in practice)",
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

    p = sub.add_parser(
        "purge",
        help="one-shot targeted wipe: delete all your messages in specific guilds/channels",
        description=(
            "Delete all your messages in one or more servers or channels without "
            "touching the rest of your account. Retention defaults to 0 — "
            "everything in the target scope is deleted regardless of age."
        ),
    )
    p.add_argument(
        "--guild",
        action="append",
        metavar="GUILD_ID",
        help="server (guild) to wipe your messages from (repeatable)",
    )
    p.add_argument(
        "--channel",
        action="append",
        metavar="CHANNEL_ID",
        help="channel or DM thread to wipe (repeatable)",
    )
    p.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    p.add_argument(
        "--retention-days",
        type=float,
        default=0.0,
        help="only delete messages older than this many days (default 0 = delete all)",
    )
    p.add_argument(
        "--delete-delay",
        type=float,
        default=float(os.environ.get("DELETE_DELAY", "1.0")),
        help="seconds between DELETE calls (default 1.0, env DELETE_DELAY)",
    )
    p.add_argument(
        "--search-delay",
        type=float,
        default=float(os.environ.get("SEARCH_DELAY", "15.0")),
        help="seconds between search-page fetches (default 15.0, env SEARCH_DELAY)",
    )
    p.add_argument("--dry-run", action="store_true", help="report without deleting")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser(
        "search",
        help="search your messages across servers/DMs and preview results",
        description=(
            "Search your messages with an optional content filter and preview "
            "channel + timestamp + content before deciding to delete. "
            "Use this as the discovery step before `purge`."
        ),
    )
    p.add_argument(
        "--guild",
        action="append",
        metavar="GUILD_ID",
        help="server to search within (repeatable; omit to search everywhere)",
    )
    p.add_argument(
        "--channel",
        action="append",
        metavar="CHANNEL_ID",
        help="channel or DM to search within (repeatable)",
    )
    p.add_argument(
        "--channel-filter",
        metavar="CHANNEL_ID",
        help="when searching a guild, limit to this channel (only with --guild)",
    )
    p.add_argument(
        "--content",
        help="text to search for (omit to see recent messages)",
    )
    p.add_argument(
        "--before",
        type=_parse_date_arg,
        metavar="DATE",
        help="only show messages before this date (YYYY-MM-DD or ISO datetime)",
    )
    p.add_argument(
        "--after",
        type=_parse_date_arg,
        metavar="DATE",
        help="only show messages after this date (YYYY-MM-DD or ISO datetime)",
    )
    p.set_defaults(func=cmd_search)

    return ap.parse_args()


def main() -> int:
    args = parse_args()
    # `status` and `seed-from-export` are the token-less subcommands — they
    # only touch local files (state.json + the export), never Discord.
    if args.cmd not in ("status", "seed-from-export") and not args.token:
        print("ERROR: no token. Set DISCORD_TOKEN env var or pass --token.", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
