"""Independent exclusion tests on TEST DATA, in a throwaway database.
Used by the dry-run report and by the unit tests."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

from .adapters import FixtureInstagram
from .engine import evaluate, find_candidate, poll_reels, queue_candidate, recheck_queue
from .handles import normalize
from .openers import compose
from .store import Store, now_iso

NOW = datetime.now(timezone.utc)


def fixture_world():
    return {
        "signed_in_as": "colethomasrod",
        "reels": [
            {"reel_id": "TEST_REEL_A", "owner": "colethomasrod",
             "likers": [{"handle": "test_baseline_liker", "account_id": "9100"}]},
            {"reel_id": "TEST_REEL_B", "owner": "colethomasrod", "likers": []},
            {"reel_id": "TEST_SOMEONE_ELSES_REEL", "owner": "not_cole",
             "likers": [{"handle": "test_on_other_reel"}]},
        ],
        "sender_following": ["test_cole_follows"],
        "dm_threads": ["test_manual_prior_dm"],
        "cannot_message": ["test_cannot_message"],
        "unverifiable": {"dm_threads": ["test_dm_unverifiable"]},
    }


def build_store(path):
    st = Store(path)
    old = (NOW - timedelta(hours=1)).isoformat()
    st.replace_list("personal_dnc", [("test_personal_dnc", None)], "test")
    st.replace_list("other_following", [("test_other_following_only", None)], "test", note="count_verified", sha256="0" * 64)
    st.replace_list("setsmart_dnc", [("test_setsmart_only", None)], "test", exported_at=old)
    st.replace_list("setsmart_history", [("test_setsmart_history", None), ("test_setsmart_queued", None)], "test", exported_at=old)
    return st


SCENARIOS = [
    # (title, handle as it arrives, account_id, expected verdict, expected reason)
    ("New liker, no exclusions", "test_new_fan", None, "eligible", None),
    ("They follow Cole, Cole doesn't follow them", "test_follows_cole_only", None, "eligible", None),
    ("1. @colethomasrod follows them", "test_cole_follows", None, "excluded", "1_sender_follows"),
    ("2. Only on @coletrodriguez_ Following export", "test_other_following_only", None, "excluded", "2_other_account_following"),
    ("3. Personal list (input ' @Test_Personal_DNC ')", " @Test_Personal_DNC ", None, "excluded", "3_personal_dnc"),
    ("4. Only on SetSmart do-not-contact", "test_setsmart_only", None, "excluded", "4_setsmart_dnc"),
    ("5. Manual prior DM (Instagram thread only)", "test_manual_prior_dm", None, "excluded", "5_prior_dm_instagram"),
    ("5. Prior outreach in SetSmart history", "test_setsmart_history", None, "excluded", "5_prior_dm_setsmart"),
    ("5. First DM already queued in SetSmart", "test_setsmart_queued", None, "excluded", "5_prior_dm_setsmart"),
    ("5. Prior-DM history unverifiable for this person", "test_dm_unverifiable", None, "unverifiable", "unverifiable:5_prior_dm_instagram"),
    ("6. Earlier attempt with uncertain outcome", "test_prior_uncertain", None, "excluded", "6_already_attempted"),
    ("7. Instagram won't allow messaging them", "test_cannot_message", None, "excluded", "7_cannot_message"),
]


def run_scenarios():
    """Returns (rows, extra_checks, samples). rows: (title, expected, actual, passed)."""
    d = tempfile.mkdtemp(prefix="fdm-scen-")
    st = build_store(os.path.join(d, "s.db"))
    ig = FixtureInstagram(fixture_world())
    st.db.execute("INSERT INTO attempts (ts_utc, ts_et, handle, opener, message, outcome) VALUES (?,?,?,?,?,?)",
                  (now_iso(), now_iso(), "test_prior_uncertain", 1, "x", "uncertain"))
    rows = []
    for title, raw, aid, exp_v, exp_r in SCENARIOS:
        h = normalize(raw)
        v, r, _ = evaluate(st, ig, h, aid)
        rows.append((title, f"{exp_v}" + (f" ({exp_r})" if exp_r else ""), f"{v}" + (f" ({r})" if r else ""),
                     v == exp_v and r == exp_r))

    extra = []
    # Baseline: first observation records existing likers, queues nobody.
    s1 = poll_reels(st, ig)
    q = st.db.execute("SELECT COUNT(*) c FROM candidates").fetchone()["c"]
    extra.append(("Baseline: existing likers recorded, none queued; other people's Reels ignored",
                  q == 0 and s1["baseline_likers"] == 1 and s1["reel_not_owned_ignored"] == 1))
    # New likers after baseline; one person liking two Reels = one candidate.
    w = ig.d["reels"]
    w[0]["likers"] += [{"handle": "test_multi_reel", "account_id": "9200"}, {"handle": "test_late_dnc"},
                       {"handle": "test_oldname", "account_id": "9300"}]
    w[1]["likers"] += [{"handle": "test_multi_reel", "account_id": "9200"}]
    poll_reels(st, ig)
    n_multi = st.db.execute("SELECT COUNT(*) c FROM candidates WHERE handle='test_multi_reel'").fetchone()["c"]
    extra.append(("Person who likes several Reels is one candidate", n_multi == 1))
    # Username change: same stable account ID, new handle -> same record.
    w[1]["likers"] += [{"handle": "test_newname", "account_id": "9300"}]
    poll_reels(st, ig)
    rows_9300 = st.db.execute("SELECT handle FROM candidates WHERE account_id='9300'").fetchall()
    extra.append(("Username change with same account ID stays one candidate (handle updated)",
                  len(rows_9300) == 1 and rows_9300[0]["handle"] == "test_newname"))
    # Recheck queue after a list change.
    st.add_to_list("personal_dnc", ["test_late_dnc"], "test")
    recheck_queue(st, ig)
    r = st.db.execute("SELECT status, reason FROM candidates WHERE handle='test_late_dnc'").fetchone()
    extra.append(("Queued person added to personal list later is dropped on recheck",
                  r["status"] == "skipped" and r["reason"] == "3_personal_dnc"))
    # Stale SetSmart list blocks sending path.
    st.replace_list("setsmart_dnc", [("test_setsmart_only", None)], "test",
                    exported_at=(NOW - timedelta(hours=48)).isoformat())
    from .adapters import Unavailable
    try:
        evaluate(st, ig, "test_new_fan")
        stale_ok = False
    except Unavailable:
        stale_ok = True
    extra.append(("Stale SetSmart list (48 h old) makes the send path stop", stale_ok))

    samples = []
    for i, h in enumerate(["test_new_fan", "test_follows_cole_only", "test_fan_three", "test_fan_four", "test_fan_five"]):
        n, text = compose(i, h)
        samples.append((h, n, text))
    return rows, extra, samples
