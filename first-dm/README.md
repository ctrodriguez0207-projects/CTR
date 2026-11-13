# First-DM workflow — @colethomasrod (FORMED Coaching)

Sends one first DM to people who newly like @colethomasrod's Reels, after a
reviewed dry run and one explicit approval. Replies and follow-ups stay manual.
It needs only Python 3.9+ and the standard library.

> **Privacy:** this repository is public. Handle lists, the queue, logs and
> exports live in `first-dm/data/`, which is gitignored. Never commit that folder.

## How it works: assisted mode

There is no supported way to automate the Instagram side for @colethomasrod.
The official API can't list Reel likers or follows, and it can't start a
conversation. Automating the app or website breaks Instagram's terms. So you
act as the Instagram link, and the workflow does everything else.

1. **Record likers.** Run `python3 -m fdm likers <reel URL>` and paste the
   Reel's visible likers, one handle per line. You can also pass `--file`. The
   first paste for a Reel is the baseline, and nobody from it is queued. Later
   pastes queue only new likers. Display names and button text such as "Follow"
   are ignored.
2. **Run `python3 -m fdm assist`.** For each person it:
   - runs every list check: the other account's Following export, your personal
     list, the SetSmart do-not-contact list and history (each no more than 24
     hours old), and this workflow's log
   - checks the 6 a.m.–11 p.m. Eastern window, the 5/hour and 80/day caps, and
     the 3-minute spacing
   - asks you to confirm on Instagram that:
     - you're signed in as @colethomasrod
     - the profile is the person who liked the Reel
     - you don't follow them
     - there's no existing DM conversation of any kind
     - Instagram lets you message them

   Answering "?" (can't tell) skips the person. Answering "no" to the
   signed-in question pauses everything.
3. **Send it by hand.** Once you've confirmed, the attempt is reserved and
   counted, and the rotation advances. The tool shows the exact message to paste.
   You send it yourself, then report one of:
   - **y:** it appeared in their conversation, so it's logged as sent
   - **u:** you're unsure, so it's logged as uncertain
   - **n:** you didn't send it; it still counts toward the caps
   - **w:** Instagram showed a warning, block or challenge; this pauses everything

   Whatever you answer, that person is never offered again.
4. **Unfinished attempts.** If you quit mid-attempt, the next `assist` asks how
   it ended before offering anyone else.

`python3 -m fdm run` (automatic sending) refuses to run because no supported
Instagram integration exists. If one ever does, implement
`fdm/adapters.py::InstagramAdapter` for it.

## Commands (run inside `first-dm/`)

| Task | Command |
|---|---|
| See mode, pause state and every required check | `python3 -m fdm status` |
| Produce the dry-run report (no sends) | `python3 -m fdm dry-run` |
| Approve the dry run and go live (one approval) | `python3 -m fdm enable-live --approve <DRY_RUN_ID>` |
| Record a Reel's visible likers | `python3 -m fdm likers https://www.instagram.com/reel/XXXX/` (then paste) |
| Send the next first DMs, guided | `python3 -m fdm assist` (`--once` for a single one) |
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
- A list counts as stale once it is more than **24 hours** old. Every `assist`
  step checks freshness and pauses if either list is stale or missing, so
  re-export both each day before sending.

## What must keep running

- **Nothing runs in the background.** Run `assist` whenever you want to send.
  Closing it, Claude Code or a browser loses nothing.
- **No catch-up batches.** The caps and spacing are rebuilt from the log every
  time, and slots you don't use expire.
- **State:** everything lives in `data/state.db` (SQLite): the queue, the lists,
  the attempts, the counters and the opener rotation. Back up `data/`.

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
