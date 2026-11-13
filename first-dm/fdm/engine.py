"""Core workflow: baseline/monitor Reels, queue candidates, exclusions,
gated live sending with reservation, confirmation and pause handling."""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import SENDER
from .adapters import AccountWarning, Deferred, ManualInstagram, Unavailable, Unverifiable
from .handles import normalize
from .openers import compose
from .schedule import can_attempt, to_et, utcnow
from .store import now_iso

EXPECTED_OTHER_FOLLOWING = 362

# Workflow-wide requirements: (name, description)
REQUIREMENTS = [
    ("ig_session", "Active Instagram session verified as @colethomasrod"),
    ("ig_reels", "List Reels owned by @colethomasrod and their visible likers"),
    ("check1_sender_follows", "Check 1: whether @colethomasrod follows a person"),
    ("check2_other_following", "Check 2: @coletrodriguez_ Following export loaded and count verified"),
    ("check3_personal_dnc", "Check 3: personal do-not-contact list loaded"),
    ("check4_setsmart_dnc", "Check 4: current SetSmart do-not-contact list (fresh)"),
    ("check5_ig_dm_history", "Check 5: Instagram DM history / existing threads for @colethomasrod"),
    ("check5_setsmart_history", "Check 5: SetSmart outreach history + queued first messages (fresh)"),
    ("check6_attempt_log", "Check 6: this workflow's attempt log"),
    ("check7_can_message", "Check 7: whether Instagram allows messaging the person"),
    ("ig_send_confirm", "Send a DM and confirm it appears in the intended conversation"),
]


def _cap(ig, name):
    caps = ig.capabilities()
    ok, why = caps.get(name, (False, "not offered"))
    return ok, why


def list_status(store, name, now=None):
    """(ok, detail) for a required list."""
    now = now or datetime.now(timezone.utc)
    meta = store.list_meta(name)
    if name == "personal_dnc":
        n = store.list_count(name)
        return (meta is not None, f"{n} handles" if meta else "never loaded")
    if name.startswith("setsmart") and store.get("setsmart_required") == "0":
        return True, ("NOT REQUIRED — " + store.get("setsmart_dropped_reason")
                      + (f"; an older import of {meta['count']} handles is still applied" if meta else ""))
    if not meta:
        return False, "not loaded"
    if name == "other_following":
        if meta["note"] != "count_verified":
            return False, f"count not verified ({meta['note']})"
        return True, f"{meta['count']} handles, export sha {meta['sha256'][:10]}"
    # SetSmart lists: freshness
    max_age = timedelta(hours=float(store.get("setsmart_max_age_hours")))
    exported = datetime.fromisoformat(meta["exported_at"])
    age = now - exported
    if age > max_age:
        return False, f"stale: exported {exported.isoformat()} ({age.total_seconds()/3600:.1f} h old > {max_age.total_seconds()/3600:.0f} h)"
    return True, f"{meta['count']} handles, exported {age.total_seconds()/3600:.1f} h ago"


def requirement_status(store, ig, now=None):
    """Returns ordered list of (name, desc, ok, detail)."""
    out = []
    test = getattr(ig, "is_test_data", False)

    def add(name, ok, detail):
        desc = dict(REQUIREMENTS)[name]
        out.append((name, desc, ok, detail + (" [TEST DATA]" if test and name.startswith(("ig_", "check1", "check5_ig", "check7")) else "")))

    ok, why = _cap(ig, "whoami")
    if getattr(ig, "manual", False):
        add("ig_session", True, "Cole confirms he is signed into @colethomasrod before each send")
    elif ok:
        try:
            who = normalize(ig.whoami())
            add("ig_session", who == SENDER, f"signed in as @{who}")
        except (Unavailable, AccountWarning) as e:
            add("ig_session", False, str(e))
    else:
        add("ig_session", False, why)
    a, w1 = _cap(ig, "list_owned_reels")
    b, w2 = _cap(ig, "list_reel_likers")
    add("ig_reels", a and b, w1 if not a else w2)
    add("check1_sender_follows", *_cap(ig, "sender_follows"))
    add("check2_other_following", *list_status(store, "other_following", now))
    add("check3_personal_dnc", *list_status(store, "personal_dnc", now))
    add("check4_setsmart_dnc", *list_status(store, "setsmart_dnc", now))
    add("check5_ig_dm_history", *_cap(ig, "dm_history"))
    add("check5_setsmart_history", *list_status(store, "setsmart_history", now))
    add("check6_attempt_log", True, f"{store.db.execute('SELECT COUNT(*) c FROM attempts').fetchone()['c']} attempts logged")
    add("check7_can_message", *_cap(ig, "can_message"))
    s, w3 = _cap(ig, "send_dm")
    c, w4 = _cap(ig, "confirm_dm")
    add("ig_send_confirm", s and c, w3 if not s else w4)
    return out


def blockers(store, ig, now=None):
    return [(n, d) for n, _, ok, d in requirement_status(store, ig, now) if not ok]


# ---------------------------------------------------------------- exclusions

def evaluate(store, ig, handle, account_id=None, strict=True):
    """Run every exclusion. Returns (verdict, reason, trace).
    verdict: 'eligible' | 'excluded' | 'unverifiable' | 'unavailable'.
    strict=True raises Unavailable if a workflow-wide check is down (sending path).
    strict=False records it and still applies every check that *is* available
    (dry run, recheck after a list change). AccountWarning always propagates."""
    trace = []

    def rec(check, status, detail=""):
        trace.append((check, status, detail))

    # 6: this workflow already attempted (any outcome) or another candidate row for same person
    if store.attempted(handle, account_id):
        rec("6_already_attempted", "excluded")
    else:
        rec("6_already_attempted", "pass")
    for name, check in (("personal_dnc", "3_personal_dnc"), ("other_following", "2_other_account_following"),
                        ("setsmart_dnc", "4_setsmart_dnc")):
        _list_check(store, rec, strict, name, check, handle, account_id)
    _list_check(store, rec, strict, "setsmart_history", "5_prior_dm_setsmart", handle, account_id)

    for check, fn, bad_if in (("1_sender_follows", ig.sender_follows, True),
                              ("5_prior_dm_instagram", ig.dm_thread_exists, True),
                              ("7_cannot_message", ig.can_message, False)):
        try:
            v = bool(fn(handle, account_id))
            rec(check, "excluded" if v == bad_if else "pass")
        except Unverifiable as e:
            rec(check, "unverifiable", str(e))
        except Deferred as e:
            rec(check, "manual_pending", str(e))
        except Unavailable as e:
            if strict:
                raise
            rec(check, "unavailable", str(e))

    order = ["1_sender_follows", "2_other_account_following", "3_personal_dnc", "4_setsmart_dnc",
             "5_prior_dm_instagram", "5_prior_dm_setsmart", "6_already_attempted", "7_cannot_message"]
    st = {c: s for c, s, _ in trace}
    for c in order:
        if st.get(c) == "excluded":
            return "excluded", c, trace
    for c in order:
        if st.get(c) == "unverifiable":
            return "unverifiable", "unverifiable:" + c, trace
    for c in order:
        if st.get(c) == "unavailable":
            return "unavailable", "unavailable:" + c, trace
    return "eligible", None, trace


def _list_check(store, rec, strict, name, check, handle, account_id):
    if name.startswith("setsmart") and store.get("setsmart_required") == "0":
        # Dropped by Cole; any previously imported SetSmart entries still exclude.
        if store.list_meta(name) is not None and store.in_list(name, handle, account_id):
            rec(check, "excluded", "matched earlier SetSmart import")
        else:
            rec(check, "not_used", "SetSmart not required")
        return
    ok, why = list_status(store, name)
    if store.list_meta(name) is None or (not ok and name != "other_following"):
        # Missing or stale list: cannot rely on it.
        if strict:
            raise Unavailable(f"{name}: {why}")
        # A stale list still proves a match; it just can't prove absence.
        if store.list_meta(name) is not None and store.in_list(name, handle, account_id):
            rec(check, "excluded", "matched stale list")
        else:
            rec(check, "unavailable", why)
        return
    if not ok and strict:
        raise Unavailable(f"{name}: {why}")
    rec(check, "excluded" if store.in_list(name, handle, account_id) else "pass")


# ------------------------------------------------------------ candidates

def find_candidate(store, handle, account_id):
    if account_id:
        r = store.db.execute("SELECT * FROM candidates WHERE account_id=?", (account_id,)).fetchone()
        if r:
            if r["handle"] != handle:  # username change: keep one record, track new handle
                store.db.execute("UPDATE candidates SET handle=? WHERE id=?", (handle, r["id"]))
                store.event("handle_changed", account_id=account_id, old=r["handle"], new=handle)
            return r
    return store.db.execute("SELECT * FROM candidates WHERE handle=?", (handle,)).fetchone()


def poll_reels(store, ig):
    """Baseline new Reels; queue likers newly observed after baseline."""
    stats = Counter()
    for reel in ig.list_owned_reels():
        if normalize(reel["owner"]) != SENDER:
            stats["reel_not_owned_ignored"] += 1
            continue
        rid = reel["reel_id"]
        ingest_likers(store, ig, rid, ig.list_reel_likers(rid), stats)
    return stats


def ingest_likers(store, ig, rid, raw_likers, stats=None):
    """First time a Reel is seen: record visible likers as baseline (queue nobody).
    Later: queue only likers not seen on that Reel before."""
    stats = stats if stats is not None else Counter()
    likers = [(normalize(h), aid) for h, aid in raw_likers]
    likers = [(h, aid) for h, aid in likers if h]
    known = store.db.execute("SELECT 1 FROM reels WHERE reel_id=?", (rid,)).fetchone()
    if not known:
        with store.tx():
            store.db.execute("INSERT INTO reels VALUES (?,?,?,?)", (rid, SENDER, now_iso(), len(likers)))
            for h, aid in likers:
                store.db.execute("INSERT OR IGNORE INTO reel_likers VALUES (?,?,?,?,1)", (rid, h, aid, now_iso()))
        store.event("reel_baselined", reel=rid, likers=len(likers))
        stats["baseline_likers"] += len(likers)
        return stats
    for h, aid in likers:
        cur = store.db.execute("INSERT OR IGNORE INTO reel_likers VALUES (?,?,?,?,0)", (rid, h, aid, now_iso()))
        if not cur.rowcount:
            continue
        stats["new_likes"] += 1
        if find_candidate(store, h, aid):
            stats["duplicate_person"] += 1   # liked several Reels: one candidate
            continue
        queue_candidate(store, ig, h, aid, rid, stats)
    return stats


def queue_candidate(store, ig, handle, account_id, reel_id, stats=None):
    verdict, reason, _ = evaluate(store, ig, handle, account_id, strict=False)
    # 'unavailable' = a workflow-wide check is down; sending is paused anyway and
    # every check runs again (strictly) immediately before any send.
    status = "queued" if verdict in ("eligible", "unavailable") else "skipped"
    store.db.execute(
        "INSERT INTO candidates (handle, account_id, source_reel, first_seen, status, reason, checked_at) VALUES (?,?,?,?,?,?,?)",
        (handle, account_id, reel_id, now_iso(), status, reason, now_iso()))
    if stats is not None:
        stats["queued" if status == "queued" else "skipped:" + reason] += 1
    return status, reason


def recheck_queue(store, ig):
    """Re-run all exclusions on queued candidates (after any list change)."""
    stats = Counter()
    for c in store.db.execute("SELECT * FROM candidates WHERE status='queued'").fetchall():
        verdict, reason, _ = evaluate(store, ig, c["handle"], c["account_id"], strict=False)
        if verdict == "unavailable":
            stats["still_queued_pending_unavailable_checks"] += 1
            continue
        if verdict != "eligible":
            store.db.execute("UPDATE candidates SET status='skipped', reason=?, checked_at=? WHERE id=?",
                             (reason, now_iso(), c["id"]))
            stats["newly_skipped:" + reason] += 1
        else:
            store.db.execute("UPDATE candidates SET checked_at=? WHERE id=?", (now_iso(), c["id"]))
            stats["still_queued"] += 1
    store.event("queue_rechecked", **{k: v for k, v in stats.items()})
    return stats


# ------------------------------------------------------------ pause / live

def pause(store, reason):
    store.set("paused", "1")
    store.set("pause_reason", reason)
    store.event("paused", reason=reason)


def is_paused(store, pause_file=None):
    if pause_file is not None and pause_file.exists():
        return True, "pause file present: " + str(pause_file)
    if store.get("paused") == "1":
        return True, store.get("pause_reason")
    return False, None


def recover_pending(store):
    """After a crash/restart: any reserved-but-unresolved attempt is uncertain."""
    n = 0
    for a in store.db.execute("SELECT * FROM attempts WHERE outcome='pending'").fetchall():
        store.db.execute("UPDATE attempts SET outcome='uncertain', detail='process stopped mid-attempt' WHERE id=?", (a["id"],))
        store.db.execute("UPDATE candidates SET status='uncertain' WHERE id=?", (a["candidate_id"],))
        n += 1
    if n:
        store.event("recovered_pending", count=n)
    return n


def send_one(store, ig, now=None, pause_file=None):
    """One sender tick. Returns a short status string. Never sends unless mode=live,
    not paused, and every gate passes."""
    now = now or utcnow()
    if store.get("mode") != "live":
        return "not_live"
    p, why = is_paused(store, pause_file)
    if p:
        return "paused: " + why
    bl = blockers(store, ig, now)
    if bl:
        pause(store, "required check unavailable: " + "; ".join(f"{n} ({d})" for n, d in bl))
        return "paused: blockers"
    if getattr(ig, "is_test_data", False) and not getattr(ig, "allow_live_in_tests", False):
        pause(store, "test-data adapter cannot send live")
        return "paused: test adapter"
    try:
        who = normalize(ig.whoami())
    except AccountWarning as e:
        pause(store, f"Instagram warning/challenge: {e}")
        return "paused: warning"
    if who != SENDER:
        pause(store, f"account mismatch: session is @{who}, expected @{SENDER}")
        return "paused: account mismatch"
    ok, why = can_attempt(now, store.counted_attempt_times())
    if not ok:
        return "wait: " + why

    for c in store.db.execute("SELECT * FROM candidates WHERE status='queued' ORDER BY id").fetchall():
        try:
            verdict, reason, _ = evaluate(store, ig, c["handle"], c["account_id"])
        except Unavailable as e:
            pause(store, f"required check unavailable: {e}")
            return "paused: unavailable"
        except AccountWarning as e:
            pause(store, f"Instagram warning/challenge: {e}")
            return "paused: warning"
        if verdict != "eligible":
            store.db.execute("UPDATE candidates SET status='skipped', reason=?, checked_at=? WHERE id=?",
                             (reason, now_iso(), c["id"]))
            continue
        # Reserve + count before sending (atomic; honors a pause that landed meanwhile).
        with store.tx():
            if store.get("paused") == "1" or (pause_file is not None and pause_file.exists()):
                return "paused: just before send"
            pos = int(store.get("rotation_pos"))
            opener, text = compose(pos, c["handle"])
            et = to_et(now)
            cur = store.db.execute(
                "INSERT INTO attempts (ts_utc, ts_et, candidate_id, handle, account_id, source_reel, opener, message, outcome) VALUES (?,?,?,?,?,?,?,?, 'pending')",
                (now.isoformat(timespec="seconds"), et.isoformat(timespec="seconds"), c["id"], c["handle"],
                 c["account_id"], c["source_reel"], opener, text))
            aid = cur.lastrowid
            store.set("rotation_pos", pos + 1)
            store.db.execute("UPDATE candidates SET status='attempting' WHERE id=?", (c["id"],))
        outcome, detail = "uncertain", ""
        try:
            ig.send_dm(c["handle"], text)
            outcome = "sent" if ig.confirm_dm(c["handle"], text) else "uncertain"
            detail = "" if outcome == "sent" else "could not confirm message in conversation"
        except AccountWarning as e:
            detail = f"Instagram warning: {e}"
            pause(store, detail)
        except Exception as e:  # noqa: BLE001 - any failure = unclear outcome, never retried
            detail = f"{type(e).__name__}: {e}"
        store.db.execute("UPDATE attempts SET outcome=?, detail=? WHERE id=?", (outcome, detail, aid))
        store.db.execute("UPDATE candidates SET status=? WHERE id=?", (outcome, c["id"]))
        n = 0 if outcome == "sent" else int(store.get("consecutive_unconfirmed")) + 1
        store.set("consecutive_unconfirmed", n)
        if n >= 2 and store.get("paused") != "1":
            pause(store, "repeated inability to confirm sends")
        return f"{outcome}: @{c['handle']} opener {opener}"
    return "idle: no eligible candidates"


# ------------------------------------------------------------ assisted mode

def _gate(store, ig, now, pause_file):
    if store.get("mode") != "live":
        return "not_live: run dry-run, review it, then enable-live --approve <ID>"
    p, why = is_paused(store, pause_file)
    if p:
        return "paused: " + why
    bl = blockers(store, ig, now)
    if bl:
        pause(store, "required check unavailable: " + "; ".join(f"{n} ({d})" for n, d in bl))
        return "paused: " + store.get("pause_reason")
    ok, why = can_attempt(now, store.counted_attempt_times())
    if not ok:
        return "wait: " + why
    return None


def assist_next(store, now=None, pause_file=None):
    """Pick the next candidate whose list-based checks all pass. Nothing is
    reserved or counted here. Returns (candidate_row, opener, text) or (None, reason)."""
    now = now or utcnow()
    ig = ManualInstagram()
    g = _gate(store, ig, now, pause_file)
    if g:
        return None, g
    for c in store.db.execute("SELECT * FROM candidates WHERE status='queued' ORDER BY id").fetchall():
        try:
            verdict, reason, _ = evaluate(store, ig, c["handle"], c["account_id"])
        except Unavailable as e:
            pause(store, f"required check unavailable: {e}")
            return None, "paused: " + store.get("pause_reason")
        if verdict != "eligible":
            store.db.execute("UPDATE candidates SET status='skipped', reason=?, checked_at=? WHERE id=?",
                             (reason, now_iso(), c["id"]))
            continue
        opener, text = compose(int(store.get("rotation_pos")), c["handle"])
        return c, opener, text
    return None, "idle: no eligible candidates in the queue"


def assist_reserve(store, candidate_id, answers, now=None, pause_file=None):
    """Cole's answers for one person, given right before sending:
    signed_in_as_sender, intended_recipient, follows, dm_thread, can_message
    (True/False, or None = can't tell). Re-runs every exclusion and every gate.
    If all pass: reserves the person, counts the attempt, advances the rotation
    and returns ("reserved", attempt_id, opener, text). Otherwise ("skipped"|"paused"|"wait", reason)."""
    now = now or utcnow()
    c = store.db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not c or c["status"] != "queued":
        return "skipped", "candidate is no longer queued"
    if answers.get("signed_in_as_sender") is not True:
        pause(store, "account mismatch or unconfirmed: Cole did not confirm the session is @colethomasrod")
        return "paused", store.get("pause_reason")
    ig = ManualInstagram(answers)
    g = _gate(store, ig, now, pause_file)
    if g:
        return g.split(":")[0], g
    if answers.get("intended_recipient") is not True:
        reason = "unverifiable:intended_recipient" if answers.get("intended_recipient") is None else "wrong_profile"
        store.db.execute("UPDATE candidates SET status='skipped', reason=?, checked_at=? WHERE id=?",
                         (reason, now_iso(), c["id"]))
        return "skipped", reason
    try:
        verdict, reason, trace = evaluate(store, ig, c["handle"], c["account_id"])
    except Unavailable as e:
        pause(store, f"required check unavailable: {e}")
        return "paused", store.get("pause_reason")
    if verdict != "eligible":
        store.db.execute("UPDATE candidates SET status='skipped', reason=?, checked_at=? WHERE id=?",
                         (reason, now_iso(), c["id"]))
        return "skipped", reason
    with store.tx():
        if store.get("paused") == "1" or (pause_file is not None and pause_file.exists()):
            return "paused", "paused just before reserving"
        pos = int(store.get("rotation_pos"))
        opener, text = compose(pos, c["handle"])
        cur = store.db.execute(
            "INSERT INTO attempts (ts_utc, ts_et, candidate_id, handle, account_id, source_reel, opener, message, outcome, detail) VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
            (now.isoformat(timespec="seconds"), to_et(now).isoformat(timespec="seconds"), c["id"], c["handle"],
             c["account_id"], c["source_reel"], opener, text,
             json.dumps({"cole_confirmed": {k: answers.get(k) for k in
                         ("signed_in_as_sender", "intended_recipient", "follows", "dm_thread", "can_message")}})))
        store.set("rotation_pos", pos + 1)
        store.db.execute("UPDATE candidates SET status='attempting' WHERE id=?", (c["id"],))
    return "reserved", cur.lastrowid, opener, text


ASSIST_OUTCOMES = {"sent", "uncertain", "not_sent", "warning"}


def assist_result(store, attempt_id, outcome, note=""):
    """sent: Cole saw the message in the right conversation.
    uncertain: not sure. not_sent: Cole didn't send it (still counted, never retried).
    warning: Instagram showed a warning/block/challenge (recorded uncertain; pauses)."""
    if outcome not in ASSIST_OUTCOMES:
        raise ValueError(outcome)
    a = store.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if not a or a["outcome"] != "pending":
        raise ValueError("attempt is not pending")
    final = "uncertain" if outcome == "warning" else outcome
    old = json.loads(a["detail"] or "{}")
    old["note"] = note or ("Instagram warning/block/challenge" if outcome == "warning" else "")
    store.db.execute("UPDATE attempts SET outcome=?, detail=? WHERE id=?", (final, json.dumps(old), attempt_id))
    store.db.execute("UPDATE candidates SET status=? WHERE id=?", (final, a["candidate_id"]))
    if outcome == "warning":
        pause(store, "Instagram warning, action block or login challenge reported by Cole")
    n = 0 if final == "sent" else int(store.get("consecutive_unconfirmed")) + (final == "uncertain")
    store.set("consecutive_unconfirmed", n)
    if n >= 2 and store.get("paused") != "1":
        pause(store, "repeated inability to confirm sends")
    return final


def pending_attempts(store):
    return store.db.execute("SELECT * FROM attempts WHERE outcome='pending' ORDER BY id").fetchall()


def next_allowed_at(store, now=None):
    """Earliest time (UTC) the spacing/hour/day/window rules allow another attempt, scanning by minute."""
    now = now or utcnow()
    times = store.counted_attempt_times()
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 48):
        if can_attempt(t, times)[0]:
            return t
        t += timedelta(minutes=1)
    return None
