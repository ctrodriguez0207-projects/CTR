# First-DM workflow — @colethomasrod (FORMED Coaching)

Sends one first DM to people who newly like @colethomasrod's Reels, after a
reviewed dry run and one explicit approval. Replies and follow-ups stay manual.
It needs only Python 3.9+ and the standard library.

> **Privacy:** this repository is public. Handle lists, the queue, logs and
> exports live in `first-dm/data/`, which is gitignored. Never commit that folder.

## Current status

Live sending is **blocked**. No supported way exists to read Reel likers,
check follows or DM history, or send first DMs for @colethomasrod:

- Instagram's official messaging API only lets the account message people who
  messaged it first. It does not list Reel likers or who the account follows.
- Driving the signed-in Instagram app or website with automation breaks
  Instagram's terms. It also risks action blocks on the account, so this
  workflow does not do it.

Everything else is built and tested: lists, exclusions, the queue, the opener
rotation, the schedule and caps, reservation and confirmation, pause and resume,
the logs and the summaries. If a supported Instagram integration becomes
available, implement `fdm/adapters.py::InstagramAdapter` for it. Every
capability must report as verified before `enable-live` will accept an approval.

## Commands (run inside `first-dm/`)

| Task | Command |
|---|---|
| See mode, pause state and every required check | `python3 -m fdm status` |
| Produce the dry-run report (no sends) | `python3 -m fdm dry-run` |
| Approve the dry run and go live (one approval) | `python3 -m fdm enable-live --approve <DRY_RUN_ID>` |
| Start the single sender | `python3 -m fdm run` |
| Pause, which blocks the next send | `python3 -m fdm pause "reason"`, or create the file `data/PAUSE` |
| Resume after a pause | `python3 -m fdm resume` (refuses while any required check is unavailable) |
| Add to the personal do-not-contact list | `python3 -m fdm personal add handle1 @handle2` or `--file list.txt` |
| Remove from it, or show it | `python3 -m fdm personal remove handle` / `python3 -m fdm personal list` |
| Replace the @coletrodriguez_ Following export | `python3 -m fdm import-following path/to/export.zip` (also takes .html, .json or .txt; add `--expect N` to verify a count) |
| Refresh the SetSmart do-not-contact list | `python3 -m fdm setsmart dnc export.csv --exported-at 2026-11-14T08:05-05:00` |
| Refresh SetSmart outreach history and queued messages | `python3 -m fdm setsmart history export.csv --exported-at ...` |
| Set the SetSmart freshness limit | `python3 -m fdm config --setsmart-max-age-hours 24` |
| Daily summary (also saved to `data/summaries/`) | `python3 -m fdm summary [--date YYYY-MM-DD]` |
| Tests | `python3 -m unittest discover -s tests -t .` |

Every list change automatically rechecks the candidates already in the queue.
Between Following exports, add new @coletrodriguez_ follows with `personal add`.

## SetSmart freshness rule (proposed)

- Export the do-not-contact list and the outreach history (including queued
  first messages) as CSV. Each needs a `handle` or `username` column; an
  `account_id` column and a `status` column are optional.
- Import both with `--exported-at` set to the time you exported them.
- A list counts as stale once it is more than **24 hours** old. Before every
  send, the sender checks freshness and pauses if either list is stale or missing.

## What must keep running

- `python3 -m fdm run` must stay running on a machine that is always on. It does
  not depend on Claude Code or a browser.
- If the process stops, nothing sends. On restart it does **not** send catch-up
  batches, because the caps and 3-minute spacing are rebuilt from the log.
- Any attempt that was interrupted mid-send is marked uncertain and never retried.
- All state lives in `data/state.db` (SQLite): the queue, the lists, the
  attempts, the counters and the opener rotation.

## Rules enforced

- **Window:** 6:00 a.m. to 11:00 p.m. America/New_York, end exclusive, with
  daylight saving time handled.
- **Caps:** at most 5 attempts per local clock hour and 80 per local day, with at
  least 3 minutes between attempts.
- **Attempt order:** each attempt is reserved and counted before it is sent. It
  is marked sent only after the message is confirmed in the right conversation.
  Otherwise it is marked uncertain, still counts toward both caps, and is never
  retried.
- **Automatic pause:** sending pauses on any of these:
  - an Instagram warning, action block or login challenge
  - an account mismatch
  - a stale or missing required list
  - two sends in a row that can't be confirmed
- **Exclusions:** all 7 run when someone is queued and again right before the
  send. A check that can't be verified for one person skips that person. A check
  that is unavailable for the whole workflow pauses sending.
- **Openers:** four, in rotation, each with light wording variants. The rotation
  advances once per recorded attempt and does not advance on skips.
