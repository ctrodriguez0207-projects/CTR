import hashlib
from collections import Counter

from .engine import requirement_status
from .scenarios import run_scenarios
from .schedule import to_et, utcnow

LISTS = [("personal_dnc", "Personal do-not-contact list"),
         ("other_following", "@coletrodriguez_ Following export"),
         ("setsmart_dnc", "SetSmart do-not-contact list"),
         ("setsmart_history", "SetSmart outreach history + queued first messages")]


def dry_run_report(st, ig):
    L = [f"# First-DM dry run — {to_et(utcnow()):%Y-%m-%d %I:%M %p %Z}", "",
         f"Mode: **{st.get('mode')}**. Messages sent by this workflow: "
         f"**{st.db.execute('SELECT COUNT(*) c FROM attempts').fetchone()['c']}**.", "",
         "## 1. Capabilities and required checks", "", "| Requirement | Status | Detail |", "|---|---|---|"]
    reqs = requirement_status(st, ig)
    for _, desc, ok, detail in reqs:
        L.append(f"| {desc} | {'verified' if ok else '**UNAVAILABLE**'} | {detail} |")
    blocked = [d for _, d, ok, _ in reqs if not ok]
    if any("see note" in d for _, _, ok, d in reqs if not ok):
        from .cli import NO_IG_NOTE
        L += ["", "Note: " + NO_IG_NOTE]
    L += ["", f"**Live sending: {'BLOCKED — ' + str(len(blocked)) + ' required check(s) unavailable' if blocked else 'all required checks available'}.**", ""]

    L += ["## 2. Lists loaded (unique handles)", "", "| List | Unique handles | Loaded / exported | Note |", "|---|---|---|---|"]
    for name, label in LISTS:
        m = st.list_meta(name)
        if m:
            L.append(f"| {label} | {m['count']} | loaded {m['loaded_at']}; exported {m['exported_at'] or '—'} | {m['note'] or ''} |")
        else:
            L.append(f"| {label} | — | **not loaded** | |")

    L += ["", "## 3. Real candidates (live Instagram data)", ""]
    c = Counter(r["status"] for r in st.db.execute("SELECT status FROM candidates"))
    sk = Counter(r["reason"] for r in st.db.execute("SELECT reason FROM candidates WHERE status='skipped'"))
    reels = st.db.execute("SELECT COUNT(*) c FROM reels").fetchone()["c"]
    L.append(f"Reels baselined: {reels}. Candidates: {dict(c) or 'none'}. Skips by reason: {dict(sk) or 'none'}.")
    if not reels:
        L.append("No Reels could be monitored, so no real candidates exist yet.")

    rows, extra, samples = run_scenarios()
    L += ["", "## 4. Independent exclusion tests (TEST DATA, isolated database)", "",
          "| Scenario | Expected | Actual | Result |", "|---|---|---|---|"]
    for t, e, a, ok in rows:
        L.append(f"| {t} | {e} | {a} | {'PASS' if ok else '**FAIL**'} |")
    L += ["", "| Behaviour | Result |", "|---|---|"]
    for t, ok in extra:
        L.append(f"| {t} | {'PASS' if ok else '**FAIL**'} |")
    skips = Counter(a.split(" (")[1].rstrip(")") for _, _, a, _ in rows if "(" in a)
    L += ["", f"Test skips by reason: {dict(skips)}"]

    L += ["", "## 5. Sample eligible candidates and their opener (TEST DATA)", "",
          "| Candidate | Opener | Exact message |", "|---|---|---|"]
    for h, n, text in samples:
        L.append(f"| @{h} | #{n} | {text} |")
    text = "\n".join(L)
    rid = hashlib.sha256(text.encode()).hexdigest()[:10]
    return text, rid
