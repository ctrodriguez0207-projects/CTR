import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fdm.adapters import FixtureInstagram
from fdm.engine import poll_reels, queue_candidate, recover_pending, send_one
from fdm.handles import normalize
from fdm.importers import ImportErrorReport, parse_following
from fdm.openers import compose
from fdm.scenarios import build_store, fixture_world, run_scenarios
from fdm.schedule import ET, can_attempt, in_window

UTC = timezone.utc


def et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(UTC)


class Handles(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("  @Jordan.Thomachefski "), "jordan.thomachefski")
        self.assertEqual(normalize("donny._.kimbo"), "donny._.kimbo")
        self.assertNotEqual(normalize("donny.kimbo"), normalize("donny._.kimbo"))
        self.assertIsNone(normalize("not a handle"))


class Schedule(unittest.TestCase):
    def test_window_edges_and_dst(self):
        self.assertFalse(in_window(et(2026, 11, 13, 5, 59)))
        self.assertTrue(in_window(et(2026, 11, 13, 6, 0)))
        self.assertTrue(in_window(et(2026, 11, 13, 22, 59)))
        self.assertFalse(in_window(et(2026, 11, 13, 23, 0)))
        # Summer (EDT, UTC-4) vs winter (EST, UTC-5): 6 a.m. local both times
        self.assertTrue(in_window(datetime(2026, 7, 1, 10, 0, tzinfo=UTC)))
        self.assertFalse(in_window(datetime(2026, 12, 1, 10, 0, tzinfo=UTC)))
        self.assertTrue(in_window(datetime(2026, 12, 1, 11, 0, tzinfo=UTC)))

    def test_hourly_cap_and_spacing(self):
        t0 = et(2026, 11, 13, 9, 0)
        times = [t0 + timedelta(minutes=3 * i) for i in range(5)]
        self.assertEqual(can_attempt(t0 + timedelta(minutes=30), times), (False, "hourly_cap"))
        self.assertEqual(can_attempt(et(2026, 11, 13, 10, 0), times)[0], True)
        # spacing across hour boundary
        self.assertEqual(can_attempt(et(2026, 11, 13, 10, 1), [et(2026, 11, 13, 9, 59)]), (False, "spacing"))
        self.assertTrue(can_attempt(et(2026, 11, 13, 10, 2), [et(2026, 11, 13, 9, 59)])[0])

    def test_daily_cap(self):
        times = []
        for h in range(6, 23):
            times += [et(2026, 11, 13, h, 3 * i) for i in range(5)]
        times = sorted(times)[:80]
        self.assertEqual(can_attempt(et(2026, 11, 13, 22, 50), times), (False, "daily_cap"))
        self.assertTrue(can_attempt(et(2026, 11, 14, 6, 0), times)[0])


class Scenarios(unittest.TestCase):
    def test_all_exclusion_scenarios(self):
        rows, extra, samples = run_scenarios()
        for t, e, a, ok in rows:
            self.assertTrue(ok, f"{t}: expected {e}, got {a}")
        for t, ok in extra:
            self.assertTrue(ok, t)
        self.assertEqual([n for _, n, _ in samples], [1, 2, 3, 4, 1])


class Sending(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = build_store(os.path.join(self.d, "s.db"))
        self.ig = FixtureInstagram(fixture_world())
        self.ig.allow_live_in_tests = True
        self.st.set("mode", "live")
        self.st.set("paused", "0")
        poll_reels(self.st, self.ig)  # baseline
        for h in ["fan_a", "fan_b", "test_cole_follows", "fan_c", "fan_d", "fan_e", "fan_f"]:
            self.ig.d["reels"][0]["likers"].append({"handle": h})
        poll_reels(self.st, self.ig)
        self.t = et(2026, 11, 13, 9, 0)

    def attempts(self):
        return self.st.db.execute("SELECT * FROM attempts ORDER BY id").fetchall()

    def test_sends_rotate_and_skips_do_not_advance(self):
        r1 = send_one(self.st, self.ig, now=self.t)
        r2 = send_one(self.st, self.ig, now=self.t + timedelta(minutes=3))
        self.assertTrue(r1.startswith("sent: @fan_a opener 1"), r1)
        self.assertTrue(r2.startswith("sent: @fan_b opener 2"), r2)
        # test_cole_follows was queued? no: excluded at queue time. Next is fan_c with opener 3
        r3 = send_one(self.st, self.ig, now=self.t + timedelta(minutes=6))
        self.assertTrue(r3.startswith("sent: @fan_c opener 3"), r3)
        self.assertEqual(self.st.get("rotation_pos"), "3")
        a = self.attempts()[0]
        self.assertEqual(a["message"], compose(0, "fan_a")[1])
        self.assertEqual(a["source_reel"], "TEST_REEL_A")
        self.assertTrue(a["ts_et"].endswith("-05:00"))

    def test_spacing_blocks(self):
        send_one(self.st, self.ig, now=self.t)
        self.assertEqual(send_one(self.st, self.ig, now=self.t + timedelta(minutes=2)), "wait: spacing")

    def test_exclusion_rechecked_right_before_send(self):
        self.st.add_to_list("personal_dnc", ["fan_a"], "test")  # no recheck_queue call on purpose
        r = send_one(self.st, self.ig, now=self.t)
        self.assertIn("@fan_b", r)
        row = self.st.db.execute("SELECT status, reason FROM candidates WHERE handle='fan_a'").fetchone()
        self.assertEqual((row["status"], row["reason"]), ("skipped", "3_personal_dnc"))

    def test_uncertain_counts_and_never_retried(self):
        self.ig.d["unconfirmable"] = ["fan_a"]
        r = send_one(self.st, self.ig, now=self.t)
        self.assertTrue(r.startswith("uncertain"), r)
        self.assertEqual(self.st.get("rotation_pos"), "1")
        send_one(self.st, self.ig, now=self.t + timedelta(minutes=3))
        self.assertEqual(sum(1 for a in self.attempts() if a["handle"] == "fan_a"), 1)
        self.assertEqual(len(self.attempts()), 2)

    def test_repeated_unconfirmed_pauses(self):
        self.ig.d["unconfirmable"] = ["fan_a", "fan_b"]
        send_one(self.st, self.ig, now=self.t)
        send_one(self.st, self.ig, now=self.t + timedelta(minutes=3))
        self.assertEqual(self.st.get("paused"), "1")
        self.assertIn("confirm", self.st.get("pause_reason"))
        self.assertTrue(send_one(self.st, self.ig, now=self.t + timedelta(minutes=6)).startswith("paused"))

    def test_warning_pauses(self):
        self.ig.d["warn_on_send"] = ["fan_a"]
        send_one(self.st, self.ig, now=self.t)
        self.assertEqual(self.st.get("paused"), "1")
        self.assertEqual(self.attempts()[0]["outcome"], "uncertain")

    def test_account_mismatch_pauses(self):
        self.ig.d["signed_in_as"] = "coletrodriguez_"
        r = send_one(self.st, self.ig, now=self.t)
        self.assertTrue(r.startswith("paused"))
        self.assertEqual(self.attempts(), [])

    def test_pause_control_and_pause_file(self):
        pf = Path(self.d) / "PAUSE"
        pf.write_text("x")
        self.assertTrue(send_one(self.st, self.ig, now=self.t, pause_file=pf).startswith("paused"))
        self.assertEqual(self.attempts(), [])

    def test_stale_setsmart_pauses(self):
        old = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
        self.st.replace_list("setsmart_dnc", [], "test", exported_at=old)
        self.assertTrue(send_one(self.st, self.ig, now=self.t).startswith("paused"))
        self.assertIn("setsmart_dnc", self.st.get("pause_reason"))

    def test_outside_window_and_dry_run_never_send(self):
        self.assertEqual(send_one(self.st, self.ig, now=et(2026, 11, 13, 23, 0)), "wait: outside_window")
        self.st.set("mode", "dry_run")
        self.assertEqual(send_one(self.st, self.ig, now=self.t), "not_live")
        self.assertEqual(self.ig.sent, [])

    def test_crash_recovery_marks_uncertain(self):
        self.st.db.execute("INSERT INTO attempts (ts_utc, ts_et, candidate_id, handle, opener, message, outcome) "
                           "VALUES (?,?,1,'fan_a',1,'x','pending')", (self.t.isoformat(), self.t.isoformat()))
        self.assertEqual(recover_pending(self.st), 1)
        self.assertEqual(self.attempts()[0]["outcome"], "uncertain")

    def test_test_adapter_cannot_go_live_by_default(self):
        self.ig.allow_live_in_tests = False
        self.assertTrue(send_one(self.st, self.ig, now=self.t).startswith("paused"))


class Importer(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(ImportErrorReport):
            parse_following("/nonexistent/following.txt")

    def test_txt_dedup(self):
        p = Path(tempfile.mkdtemp()) / "f.txt"
        p.write_text("@Alpha\nalpha \n beta.one\n")
        hs, info = parse_following(p)
        self.assertEqual(hs, {"alpha", "beta.one"})
        self.assertEqual(info["unique"], 2)


if __name__ == "__main__":
    unittest.main()
