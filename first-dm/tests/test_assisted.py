import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fdm import cli
from fdm.adapters import ManualInstagram
from fdm.engine import assist_next, assist_reserve, assist_result, ingest_likers
from fdm.scenarios import build_store
from fdm.schedule import ET

UTC = timezone.utc
T = datetime(2026, 11, 13, 9, 0, tzinfo=ET).astimezone(UTC)
OK = {"signed_in_as_sender": True, "intended_recipient": True, "follows": False, "dm_thread": False, "can_message": True}


def seed(path):
    st = build_store(path)
    st.set("mode", "live")
    st.set("paused", "0")
    ig = ManualInstagram()
    ingest_likers(st, ig, "REEL1", [("test_old_liker", None)])            # baseline
    ingest_likers(st, ig, "REEL1", [("test_old_liker", None), ("fan_a", None), ("fan_b", None),
                                    ("test_other_following_only", None), ("fan_c", None)])
    ingest_likers(st, ig, "REEL2", [("fan_a", None)])                       # baseline of REEL2
    return st


class Assisted(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = seed(os.path.join(self.d, "s.db"))

    def cands(self):
        return {r["handle"]: (r["status"], r["reason"]) for r in self.st.db.execute("SELECT * FROM candidates")}

    def n_attempts(self):
        return self.st.db.execute("SELECT COUNT(*) c FROM attempts").fetchone()["c"]

    def test_queueing_defers_instagram_checks_but_applies_lists(self):
        c = self.cands()
        self.assertNotIn("test_old_liker", c)           # baseline never queued
        self.assertEqual(c["fan_a"][0], "queued")
        self.assertEqual(c["test_other_following_only"], ("skipped", "2_other_account_following"))

    def test_next_does_not_count_or_rotate(self):
        c, opener, text = assist_next(self.st, now=T)
        self.assertEqual((c["handle"], opener), ("fan_a", 1))
        self.assertEqual(self.n_attempts(), 0)
        self.assertEqual(self.st.get("rotation_pos"), "0")

    def test_cole_answers_exclude_without_advancing(self):
        c, _, _ = assist_next(self.st, now=T)
        r = assist_reserve(self.st, c["id"], dict(OK, follows=True), now=T)
        self.assertEqual(r, ("skipped", "1_sender_follows"))
        c, _, _ = assist_next(self.st, now=T)
        self.assertEqual(assist_reserve(self.st, c["id"], dict(OK, dm_thread=True), now=T),
                         ("skipped", "5_prior_dm_instagram"))
        c, _, _ = assist_next(self.st, now=T)
        self.assertEqual(assist_reserve(self.st, c["id"], dict(OK, can_message=None), now=T),
                         ("skipped", "unverifiable:7_cannot_message"))
        self.assertEqual(self.st.get("rotation_pos"), "0")
        self.assertEqual(self.n_attempts(), 0)

    def test_wrong_or_unclear_profile_skips(self):
        c, _, _ = assist_next(self.st, now=T)
        self.assertEqual(assist_reserve(self.st, c["id"], dict(OK, intended_recipient=None), now=T),
                         ("skipped", "unverifiable:intended_recipient"))

    def test_account_not_confirmed_pauses(self):
        c, _, _ = assist_next(self.st, now=T)
        r = assist_reserve(self.st, c["id"], dict(OK, signed_in_as_sender=False), now=T)
        self.assertEqual(r[0], "paused")
        self.assertEqual(self.n_attempts(), 0)
        self.assertTrue(assist_next(self.st, now=T)[1].startswith("paused"))

    def test_reserve_counts_then_sent_and_spacing(self):
        c, _, _ = assist_next(self.st, now=T)
        st, aid, opener, text = assist_reserve(self.st, c["id"], OK, now=T)
        self.assertEqual((st, opener), ("reserved", 1))
        self.assertEqual(self.st.get("rotation_pos"), "1")
        row = self.st.db.execute("SELECT * FROM attempts WHERE id=?", (aid,)).fetchone()
        self.assertEqual((row["outcome"], row["message"], row["source_reel"]), ("pending", text, "REEL1"))
        self.assertEqual(assist_result(self.st, aid, "sent"), "sent")
        self.assertEqual(assist_next(self.st, now=T + timedelta(minutes=2)), (None, "wait: spacing"))
        c2, opener2, _ = assist_next(self.st, now=T + timedelta(minutes=3))
        self.assertEqual((c2["handle"], opener2), ("fan_b", 2))

    def test_not_sent_still_counted_never_retried(self):
        c, _, _ = assist_next(self.st, now=T)
        _, aid, _, _ = assist_reserve(self.st, c["id"], OK, now=T)
        assist_result(self.st, aid, "not_sent")
        nxt = assist_next(self.st, now=T + timedelta(minutes=3))[0]
        self.assertEqual(nxt["handle"], "fan_b")
        self.assertEqual(self.cands()["fan_a"][0], "not_sent")

    def test_two_uncertain_pause_and_warning_pauses(self):
        for i, expect_paused in ((0, "0"), (3, "1")):
            c, _, _ = assist_next(self.st, now=T + timedelta(minutes=i))
            _, aid, _, _ = assist_reserve(self.st, c["id"], OK, now=T + timedelta(minutes=i))
            assist_result(self.st, aid, "uncertain")
            self.assertEqual(self.st.get("paused"), expect_paused)
        self.st.set("paused", "0")
        c, _, _ = assist_next(self.st, now=T + timedelta(minutes=6))
        _, aid, _, _ = assist_reserve(self.st, c["id"], OK, now=T + timedelta(minutes=6))
        assist_result(self.st, aid, "warning")
        self.assertEqual(self.st.get("paused"), "1")
        self.assertIn("warning", self.st.get("pause_reason"))

    def test_stale_setsmart_24h_pauses(self):
        self.st.replace_list("setsmart_history", [], "t", exported_at=(datetime.now(UTC) - timedelta(hours=25)).isoformat())
        r = assist_next(self.st, now=T)
        self.assertIsNone(r[0])
        self.assertIn("setsmart_history", r[1])

    def test_list_change_between_next_and_reserve(self):
        c, _, _ = assist_next(self.st, now=T)
        self.st.add_to_list("personal_dnc", ["fan_a"], "t")
        self.assertEqual(assist_reserve(self.st, c["id"], OK, now=T), ("skipped", "3_personal_dnc"))


class Parsing(unittest.TestCase):
    def test_liker_paste(self):
        hs, ign = cli.parse_liker_lines(["@Fan_A", "Fan A Name", "Follow", "following", "fan.b", "", "fan_a"])
        self.assertEqual(hs, ["fan_a", "fan.b"])
        self.assertEqual(ign, ["Fan A Name", "Follow", "following"])

    def test_reel_url(self):
        self.assertEqual(cli.parse_reel_id("https://www.instagram.com/reel/DAbc_12-x/?igsh=1"), "DAbc_12-x")


class AssistCLI(unittest.TestCase):
    def test_interactive_walkthrough(self):
        d = Path(tempfile.mkdtemp())
        seed(str(d / "state.db"))
        answers = iter(["y", "y", "n", "n", "y", "y"])   # account, profile, follows, thread, can msg, appeared
        with mock.patch.object(cli, "DATA", d), mock.patch.object(cli, "PAUSE_FILE", d / "PAUSE"), \
                mock.patch("builtins.input", lambda *_: next(answers)), \
                mock.patch("fdm.engine.utcnow", return_value=T), mock.patch("fdm.cli.utcnow", return_value=T):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.main(["assist"])
        text = out.getvalue()
        self.assertEqual(rc, 0, text)
        self.assertIn("Send this exact message (opener #1) to @fan_a", text)
        self.assertIn("Recorded: sent.", text)
        self.assertIn("wait: spacing", text)
        self.assertIn("Today: 1 confirmed", text)


if __name__ == "__main__":
    unittest.main()


class DroppedSetSmart(unittest.TestCase):
    def test_runs_without_setsmart_but_old_matches_still_exclude(self):
        st = seed(os.path.join(tempfile.mkdtemp(), "s.db"))
        st.replace_list("setsmart_dnc", [("fan_a", None)], "t",
                        exported_at=(datetime.now(UTC) - timedelta(days=5)).isoformat())   # stale
        self.assertTrue(assist_next(st, now=T)[1].startswith("paused"))
        st.set("paused", "0")
        st.set("setsmart_required", "0")
        st.set("setsmart_dropped_reason", "test")
        c, _, _ = assist_next(st, now=T)
        self.assertEqual(c["handle"], "fan_b")   # fan_a still excluded by the old import
        st.db.execute("DELETE FROM list_meta WHERE list_name LIKE 'setsmart%'")
        st.db.execute("DELETE FROM list_entries WHERE list_name LIKE 'setsmart%'")
        self.assertEqual(assist_next(st, now=T)[0]["handle"], "fan_b")
