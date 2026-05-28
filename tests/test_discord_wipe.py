"""Tests for discord_wipe.

Covers:
  - The three safety layers AGENTS.md mandates a test for
    (only-my-messages defence-in-depth).
  - Regression tests for the 9 review issues fixed in this PR.

Mocks the HTTP layer at the helper-function boundary
(delete_message / search_messages / get_me / list_my_guilds /
list_my_dms) so we exercise the real phase_export / phase_live_catchup
control flow without touching Discord.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import discord_wipe as dw

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(
    state: dw.State, *, dry_run: bool = False, delete_delay: float = 0.0
) -> dw.WipeConfig:
    return dw.WipeConfig(
        token="fake",
        me_id="self-id",
        state=state,
        cutoff=datetime(2026, 5, 21, tzinfo=timezone.utc),
        delete_delay=delete_delay,
        search_delay=0.0,
        dry_run=dry_run,
        exclude_guilds=set(),
        exclude_channels=set(),
    )


def _write_export(
    tmp: pathlib.Path, *, channels: list[tuple[str, str, list[dict]]]
) -> pathlib.Path:
    """Build a fake export tree.

    channels = [(channel_id, channel_type, [{"ID": "...", "Timestamp": "..."}])].
    """
    root = tmp / "Messages"
    root.mkdir(parents=True)
    index = {}
    for cid, ctype, msgs in channels:
        cdir = root / f"c{cid}"
        cdir.mkdir()
        (cdir / "channel.json").write_text(json.dumps({"id": cid, "type": ctype}))
        (cdir / "messages.json").write_text(json.dumps(msgs))
        index[cid] = f"name-of-{cid}"
    (root / "index.json").write_text(json.dumps(index))
    return root


def _reset_stop():
    """Clear the module-level STOP flag between tests."""
    dw.STOP = False


# ---------------------------------------------------------------------------
# AGENTS.md mandated safety tests
# ---------------------------------------------------------------------------


class SafetyLayer1_ExportOnlyMyMessages(unittest.TestCase):
    """Layer 1: export phase only ever sees IDs from c<id>/messages.json,
    which Discord itself populated with only-my-messages. We can't test
    Discord's contract — but we CAN test that we read no other source."""

    def test_phase_export_reads_only_messages_json_per_channel(self):
        _reset_stop()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            export = _write_export(
                tmp,
                channels=[
                    ("100", "DM", [{"ID": "9001", "Timestamp": "2026-05-01 00:00:00"}]),
                ],
            )
            state = dw.State(tmp / "state.json")
            cfg = _make_cfg(state)

            with (
                mock.patch.object(dw, "delete_message", return_value=("ok", 0.0)) as dm,
                mock.patch.object(dw.time, "sleep"),
            ):
                dw.phase_export(mock.MagicMock(), cfg, export)

            # Only the one message ID we put in messages.json got deleted.
            ids = [c.kwargs.get("msg_id") or c.args[2] for c in dm.call_args_list]
            self.assertEqual(ids, ["9001"])


class SafetyLayer2_SearchUsesAuthorId(unittest.TestCase):
    """Layer 2: catchup phase MUST pass author_id=<self> on every
    search_messages call. Discord server-side-filters by author."""

    def test_phase_live_catchup_always_passes_author_id_self(self):
        _reset_stop()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            state = dw.State(tmp / "state.json")
            cfg = _make_cfg(state)
            sess = mock.MagicMock()

            # Empty page on every search → scope ends after 2 empty streaks.
            with (
                mock.patch.object(dw, "list_my_guilds", return_value=[{"id": "g1", "name": "G"}]),
                mock.patch.object(
                    dw,
                    "list_my_dms",
                    return_value=[{"id": "d1", "type": 1, "recipients": []}],
                ),
                mock.patch.object(dw, "search_messages", return_value=(0, [], None)) as sm,
                mock.patch.object(dw.time, "sleep"),
            ):
                dw.phase_live_catchup(sess, cfg)

            self.assertGreater(sm.call_count, 0)
            for call in sm.call_args_list:
                self.assertEqual(
                    call.kwargs.get("author_id"),
                    "self-id",
                    f"search call missing author_id=self: {call}",
                )


class SafetyLayer3_403IsTerminalNotRetried(unittest.TestCase):
    """Layer 3: a 403 from DELETE must be classified 'forbidden' and the
    caller must NOT retry it. (If we somehow targeted someone else's
    message, a 403 from a non-admin scope is what protects us.)"""

    def test_delete_message_returns_forbidden_on_403_without_retry(self):
        resp = mock.MagicMock()
        resp.status_code = 403
        sess = mock.MagicMock()
        sess.delete.return_value = resp
        status, hint = dw.delete_message(sess, "chan", "msg")
        self.assertEqual(status, "forbidden")
        self.assertEqual(hint, 0.0)
        # Exactly one HTTP call — no retry loop.
        self.assertEqual(sess.delete.call_count, 1)


# ---------------------------------------------------------------------------
# Bug 1: SIGTERM during retry must NOT mark message as deleted
# ---------------------------------------------------------------------------


class Bug1_StopDuringRetryMustNotMark(unittest.TestCase):
    def test_export_does_not_mark_when_sigterm_fires_during_retry(self):
        _reset_stop()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            export = _write_export(
                tmp,
                channels=[
                    ("100", "DM", [{"ID": "9001", "Timestamp": "2026-05-01 00:00:00"}]),
                ],
            )
            state = dw.State(tmp / "state.json")
            cfg = _make_cfg(state)

            # First call returns retry; sleep is patched to set STOP.
            def trigger_stop(*a, **kw):
                dw.STOP = True

            with (
                mock.patch.object(dw, "delete_message", return_value=("retry", 0.01)),
                mock.patch.object(dw.time, "sleep", side_effect=trigger_stop),
            ):
                dw.phase_export(mock.MagicMock(), cfg, export)

            self.assertNotIn(
                "9001", state.deleted, "STOP fired mid-retry; ID must not be marked as deleted"
            )

    def test_catchup_does_not_mark_when_sigterm_fires_during_retry(self):
        _reset_stop()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            state = dw.State(tmp / "state.json")
            cfg = _make_cfg(state)
            sess = mock.MagicMock()

            def trigger_stop(*a, **kw):
                dw.STOP = True

            # One non-empty search page, then nothing matters because STOP fires.
            search_pages = [
                (1, [{"id": "9002", "channel_id": "c1", "hit": True}], None),
                (0, [], None),
            ]
            with (
                mock.patch.object(dw, "list_my_guilds", return_value=[]),
                mock.patch.object(
                    dw,
                    "list_my_dms",
                    return_value=[{"id": "c1", "type": 1, "recipients": []}],
                ),
                mock.patch.object(dw, "search_messages", side_effect=search_pages),
                mock.patch.object(dw, "delete_message", return_value=("retry", 0.01)),
                mock.patch.object(dw.time, "sleep", side_effect=trigger_stop),
            ):
                dw.phase_live_catchup(sess, cfg)

            self.assertNotIn(
                "9002", state.deleted, "STOP fired mid-retry in catchup; ID must not be marked"
            )


# ---------------------------------------------------------------------------
# Bug 2: ZeroDivisionError on empty export
# ---------------------------------------------------------------------------


class Bug2_EmptyExportNoZeroDivision(unittest.TestCase):
    def test_phase_export_handles_empty_messages_json_without_crashing(self):
        _reset_stop()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            export = _write_export(tmp, channels=[("100", "DM", [])])
            state = dw.State(tmp / "state.json")
            cfg = _make_cfg(state)
            with mock.patch.object(dw.time, "sleep"):
                # Must not raise.
                dw.phase_export(mock.MagicMock(), cfg, export)


# ---------------------------------------------------------------------------
# Bug 3: catchup pacing must use max(), not sum
# ---------------------------------------------------------------------------


class Bug3_CatchupPacingMatchesExport(unittest.TestCase):
    def test_catchup_post_delete_sleep_is_max_floor_or_hint_not_sum(self):
        _reset_stop()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            state = dw.State(tmp / "state.json")
            cfg = _make_cfg(state, delete_delay=1.0)
            sess = mock.MagicMock()

            sleeps: list[float] = []
            search_pages = [
                (1, [{"id": "9003", "channel_id": "c1", "hit": True}], None),
                (0, [], None),
                (0, [], None),
            ]
            # Bucket hint 0.6s, floor 1.0s — expected post-delete sleep is max=1.0.
            with (
                mock.patch.object(dw, "list_my_guilds", return_value=[]),
                mock.patch.object(
                    dw,
                    "list_my_dms",
                    return_value=[{"id": "c1", "type": 1, "recipients": []}],
                ),
                mock.patch.object(dw, "search_messages", side_effect=search_pages),
                mock.patch.object(dw, "delete_message", return_value=("ok", 0.6)),
                mock.patch.object(dw.time, "sleep", side_effect=lambda s: sleeps.append(s)),
            ):
                dw.phase_live_catchup(sess, cfg)

            # Find a 1.0 (max) sleep, NOT a 1.6 (sum). delta = floor+hint=1.6
            self.assertIn(1.0, sleeps, f"expected max(1.0, 0.6)=1.0 sleep, got {sleeps}")
            self.assertNotIn(1.6, sleeps, f"catchup still summing: {sleeps}")


# ---------------------------------------------------------------------------
# Bug 4 (REGRESSION): State must NOT GC by message snowflake age.
# v0.3.0 shipped a State.gc(retention_days) that dropped IDs whose
# snowflake_ts was older than 2x retention. But state.deleted holds
# IDs of messages we DELETED — i.e. messages that were older than the
# retention cutoff in the first place — so the GC swept out the
# entire just-deleted set on the next pass. This guards against
# anyone reintroducing that pattern.
# ---------------------------------------------------------------------------


class Bug4_StateDoesNotGcRecentlyDeletedOldMessages(unittest.TestCase):
    def test_round_trip_preserves_ids_with_old_snowflakes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            sp = tmp / "state.json"
            state = dw.State(sp)

            # Mark IDs whose snowflake_ts is well outside retention — these
            # represent messages we JUST deleted (because they were old).
            now = datetime(2026, 5, 28, tzinfo=timezone.utc)
            ids = [str(dw.snowflake_at(now - timedelta(days=age))) for age in (10, 30, 365)]
            for mid in ids:
                state.mark(mid)
            state.save()

            # Re-load (simulates a container restart between passes).
            reloaded = dw.State(sp)
            for mid in ids:
                self.assertIn(
                    mid,
                    reloaded.deleted,
                    "recently-marked ID with old snowflake_ts was lost on round-trip; "
                    "a snowflake-based GC must NOT be reintroduced (see comment in State)",
                )

    def test_state_has_no_snowflake_based_gc_method(self):
        # If a future change reintroduces State.gc(retention_days=...),
        # this test fires — forcing the author to read the comment in
        # State.mark() explaining why that was wrong.
        self.assertFalse(
            hasattr(dw.State, "gc"),
            "State.gc() was the v0.3.0 footgun — do not reintroduce. "
            "See the comment block above State.mark().",
        )


# ---------------------------------------------------------------------------
# Bug 5: pre-count cache prevents double-parse
# ---------------------------------------------------------------------------


class Bug5_MessagesJsonParsedOncePerPass(unittest.TestCase):
    def test_messages_json_read_once_per_channel_per_pass(self):
        _reset_stop()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            export = _write_export(
                tmp,
                channels=[
                    ("100", "DM", [{"ID": "9001", "Timestamp": "2026-05-01 00:00:00"}]),
                ],
            )
            state = dw.State(tmp / "state.json")
            cfg = _make_cfg(state)

            real_read_text = pathlib.Path.read_text
            read_counts: dict[str, int] = {}

            def counting_read(self, *a, **kw):
                if self.name == "messages.json":
                    read_counts[str(self)] = read_counts.get(str(self), 0) + 1
                return real_read_text(self, *a, **kw)

            with (
                mock.patch.object(pathlib.Path, "read_text", counting_read),
                mock.patch.object(dw, "delete_message", return_value=("ok", 0.0)),
                mock.patch.object(dw.time, "sleep"),
            ):
                dw.phase_export(mock.MagicMock(), cfg, export)

            for path, n in read_counts.items():
                self.assertEqual(n, 1, f"{path} read {n} times; expected 1")


# ---------------------------------------------------------------------------
# Bug 6: corrupt state.json is backed up before reset
# ---------------------------------------------------------------------------


class Bug6_CorruptStateIsBackedUp(unittest.TestCase):
    def test_corrupt_state_json_renamed_to_backup_on_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            sp = tmp / "state.json"
            sp.write_text("{ not json")
            state = dw.State(sp)
            # The corrupt file should have been renamed, not vanished.
            backups = list(tmp.glob("state.json.corrupt-*"))
            self.assertEqual(len(backups), 1, f"expected one backup; got {list(tmp.iterdir())}")
            self.assertEqual(backups[0].read_text(), "{ not json")
            self.assertEqual(state.deleted, set())


# ---------------------------------------------------------------------------
# Bug 7: main loop survives corrupt messages.json (matches pre-count suppress)
# ---------------------------------------------------------------------------


class Bug7_CorruptMessagesJsonDoesNotCrashPass(unittest.TestCase):
    def test_main_loop_skips_channel_with_corrupt_messages_json(self):
        _reset_stop()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            export = _write_export(
                tmp,
                channels=[
                    ("100", "DM", [{"ID": "9001", "Timestamp": "2026-05-01 00:00:00"}]),
                    ("200", "DM", [{"ID": "9002", "Timestamp": "2026-05-01 00:00:00"}]),
                ],
            )
            # Corrupt channel 100's messages.json AFTER tree is built.
            (export / "c100" / "messages.json").write_text("{ broken")
            state = dw.State(tmp / "state.json")
            cfg = _make_cfg(state)

            with (
                mock.patch.object(dw, "delete_message", return_value=("ok", 0.0)),
                mock.patch.object(dw.time, "sleep"),
            ):
                # Must not raise.
                dw.phase_export(mock.MagicMock(), cfg, export)

            # Channel 200 still got its delete; 100 was skipped.
            self.assertIn("9002", state.deleted)
            self.assertNotIn("9001", state.deleted)


# ---------------------------------------------------------------------------
# Bug 8: pre-flight refreshes me_id; bails on identity change
# ---------------------------------------------------------------------------


class Bug8_PreflightRefreshesIdentity(unittest.TestCase):
    def test_cmd_run_bails_when_token_swap_changes_user_id(self):
        _reset_stop()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            state_path = tmp / "state.json"
            export_dir = tmp / "no-export"  # doesn't exist; export phase skipped
            args = argparse.Namespace(
                token="fake",
                state=state_path,
                export_dir=export_dir,
                retention_days=7.0,
                delete_delay=0.0,
                search_delay=0.0,
                interval_hours=24.0,
                watch=False,
                dry_run=True,
                exclude_guild=[],
                exclude_channel=[],
            )

            # First get_me returns user A; SECOND (pre-flight) returns user B.
            call_count = {"n": 0}

            def fake_get_me(_sess):
                call_count["n"] += 1
                return (
                    {"id": "user-A", "username": "a"}
                    if call_count["n"] == 1
                    else {"id": "user-B", "username": "b"}
                )

            # _auth_paused_exit spins on `while not STOP: time.sleep(5)`,
            # so the patched sleep must flip STOP to exit. Pre-flight
            # itself doesn't sleep, so the first sleep call IS the
            # paused-exit one — setting STOP there is correct.
            def sleep_then_stop(*_a, **_kw):
                dw.STOP = True

            # If pre-flight does NOT detect the swap, the loop reaches
            # phase_live_catchup which calls list_my_guilds. We assert
            # that never happens.
            with (
                mock.patch.object(dw, "get_me", side_effect=fake_get_me),
                mock.patch.object(dw, "list_my_guilds", return_value=[]) as gl,
                mock.patch.object(dw, "list_my_dms", return_value=[]) as dl,
                mock.patch.object(dw.time, "sleep", side_effect=sleep_then_stop),
            ):
                rc = dw.cmd_run(args)

            self.assertEqual(rc, 0)  # paused-exit returns 0
            self.assertEqual(
                gl.call_count,
                0,
                "identity swap was not detected; phase_live_catchup ran under stale me_id",
            )
            self.assertEqual(dl.call_count, 0)
            self.assertGreaterEqual(call_count["n"], 2)


# ---------------------------------------------------------------------------
# Bug 9: pacing must persist across messages and refresh from 404 + 429.
#
# When a stream of DELETEs lands on already-deleted IDs (404), the catchup
# loop must NOT settle at "every call 429s". Pre-v0.3.2 behaviour:
#   - delete_message read X-RateLimit-* headers ONLY on 204.
#   - extra_sleep was re-initialised to 0.0 on every for-loop iteration.
#   - So a 404 path → extra_sleep stays 0.0 → post-sleep = floor (1.0s)
#     → next call hits 429 → retry 2.3s → 404 → repeat.
# Symptoms in prod: rate dropped from ~30/min on 204s to ~18/min on 404s.
#
# Fix: read headers on 204 + 404 + 429, hoist extra_sleep out of the
# for-loop, treat a 429's retry_after as the new persistent pacing floor.
# ---------------------------------------------------------------------------


class Bug9_PacingPersistsAcrossMessagesAndRefreshesFrom404And429(unittest.TestCase):
    def test_429_then_204_propagates_pacing_to_next_message(self):
        """After a 429 (retry_after=2.3s) the NEXT message's post-mark sleep
        must be at least 2.3s, not the 1.0s floor."""
        _reset_stop()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            state = dw.State(tmp / "state.json")
            cfg = _make_cfg(state, delete_delay=1.0)
            sess = mock.MagicMock()

            sleeps: list[float] = []
            # delete_message returns a sequence:
            #   msg-1 attempt 1: 429 (retry_after=2.3s)
            #   msg-1 attempt 2: 404 (gone)
            #   msg-2 attempt 1: 404 (gone)
            #   msg-3+: doesn't matter (search ends)
            delete_returns = [
                ("retry", 2.3),
                ("gone", 0.0),
                ("gone", 0.0),
                ("gone", 0.0),
            ]
            search_pages = [
                (
                    2,
                    [
                        {"id": "msg-1", "channel_id": "c1", "hit": True},
                        {"id": "msg-2", "channel_id": "c1", "hit": True},
                    ],
                    None,
                ),
                (0, [], None),
                (0, [], None),
            ]
            with (
                mock.patch.object(dw, "list_my_guilds", return_value=[]),
                mock.patch.object(
                    dw,
                    "list_my_dms",
                    return_value=[{"id": "c1", "type": 1, "recipients": []}],
                ),
                mock.patch.object(dw, "search_messages", side_effect=search_pages),
                mock.patch.object(dw, "delete_message", side_effect=delete_returns),
                mock.patch.object(dw.time, "sleep", side_effect=lambda s: sleeps.append(s)),
            ):
                dw.phase_live_catchup(sess, cfg)

            # We expect:
            #   - One 2.3s sleep from the inner 429 retry.
            #   - msg-1 post-mark sleep ≥ 2.3s (pacing floor inherited from 429).
            #   - msg-2 post-mark sleep ≥ 2.3s (pacing floor persists across messages).
            sleeps_geq_floor = [s for s in sleeps if s >= 2.3]
            self.assertGreaterEqual(
                len(sleeps_geq_floor),
                3,
                f"expected ≥3 sleeps >= 2.3s (1 retry + 2 post-mark with persistent "
                f"floor); got sleeps={sleeps}",
            )
            # AND msg-2's post-mark sleep specifically must NOT be the 1.0 floor.
            non_retry_sleeps = [s for s in sleeps if s != 2.3]
            self.assertFalse(
                any(0.99 <= s <= 1.01 for s in non_retry_sleeps),
                f"found a 1.0s post-mark sleep — pacing floor wasn't propagated. sleeps={sleeps}",
            )

    def test_bucket_hint_helper_extracts_from_any_response(self):
        """_bucket_hint() must read rate-limit headers regardless of status."""
        for status in (204, 404, 429):
            r = mock.MagicMock()
            r.status_code = status
            r.headers = {"X-RateLimit-Remaining": "4", "X-RateLimit-Reset-After": "8.0"}
            self.assertEqual(
                dw._bucket_hint(r),
                2.0,
                f"_bucket_hint failed on {status}: expected 8/4 = 2.0",
            )
        # No headers → 0.0 (fall back to floor).
        r = mock.MagicMock()
        r.headers = {}
        self.assertEqual(dw._bucket_hint(r), 0.0)

    def test_delete_message_returns_hint_on_404_when_headers_present(self):
        sess = mock.MagicMock()
        resp = mock.MagicMock()
        resp.status_code = 404
        resp.headers = {"X-RateLimit-Remaining": "5", "X-RateLimit-Reset-After": "10.0"}
        sess.delete.return_value = resp
        status, hint = dw.delete_message(sess, "c1", "m1")
        self.assertEqual(status, "gone")
        self.assertEqual(hint, 2.0)  # 10/5


if __name__ == "__main__":
    unittest.main()
