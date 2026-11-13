"""Persistent state in one SQLite file (WAL). Survives restarts."""
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
-- list_name: personal_dnc | other_following | setsmart_dnc | setsmart_history
CREATE TABLE IF NOT EXISTS list_entries (
  list_name TEXT, handle TEXT, account_id TEXT, added_at TEXT, source TEXT,
  PRIMARY KEY (list_name, handle));
CREATE TABLE IF NOT EXISTS list_meta (
  list_name TEXT PRIMARY KEY, loaded_at TEXT, exported_at TEXT,
  source TEXT, count INTEGER, sha256 TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS reels (
  reel_id TEXT PRIMARY KEY, owner TEXT, baseline_at TEXT, baseline_count INTEGER);
CREATE TABLE IF NOT EXISTS reel_likers (
  reel_id TEXT, handle TEXT, account_id TEXT, first_seen TEXT, is_baseline INTEGER,
  PRIMARY KEY (reel_id, handle));
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  handle TEXT UNIQUE, account_id TEXT UNIQUE, source_reel TEXT,
  first_seen TEXT, status TEXT, reason TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT, ts_et TEXT,
  candidate_id INTEGER, handle TEXT, account_id TEXT, source_reel TEXT,
  opener INTEGER, message TEXT, outcome TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT, kind TEXT, detail TEXT);
"""

DEFAULTS = {
    "mode": "dry_run",          # dry_run | live
    "paused": "1",              # sending starts paused
    "pause_reason": "initial state: dry run not yet approved",
    "rotation_pos": "0",
    "setsmart_max_age_hours": "24",
    "approved_dry_run_id": "",
    "consecutive_unconfirmed": "0",
    "setsmart_required": "1",
    "setsmart_dropped_reason": "",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for k, v in DEFAULTS.items():
            self.db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, v))

    # settings
    def get(self, key):
        r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))

    def event(self, kind, **detail):
        self.db.execute("INSERT INTO events (ts_utc, kind, detail) VALUES (?,?,?)",
                        (now_iso(), kind, json.dumps(detail)))

    # lists
    def replace_list(self, name, entries, source, exported_at=None, sha256=None, note=None):
        """entries: iterable of (handle, account_id_or_None)."""
        entries = list(entries)
        with self.tx():
            self.db.execute("DELETE FROM list_entries WHERE list_name=?", (name,))
            for h, aid in entries:
                self.db.execute("INSERT OR IGNORE INTO list_entries VALUES (?,?,?,?,?)",
                                (name, h, aid, now_iso(), source))
            self._touch_meta(name, source, exported_at, sha256, note)
        self.event("list_replaced", list=name, count=self.list_count(name), source=source)

    def add_to_list(self, name, handles, source):
        added = 0
        with self.tx():
            for h in handles:
                cur = self.db.execute("INSERT OR IGNORE INTO list_entries VALUES (?,?,?,?,?)",
                                      (name, h, None, now_iso(), source))
                added += cur.rowcount
            self._touch_meta(name, source, None, None, None, keep_export=True)
        self.event("list_added", list=name, added=added, source=source)
        return added

    def remove_from_list(self, name, handles):
        n = 0
        for h in handles:
            n += self.db.execute("DELETE FROM list_entries WHERE list_name=? AND handle=?",
                                 (name, h)).rowcount
        self.event("list_removed", list=name, removed=n)
        return n

    def _touch_meta(self, name, source, exported_at, sha256, note, keep_export=False):
        cnt = self.list_count(name)
        old = self.list_meta(name)
        if keep_export and old:
            exported_at, sha256, note = old["exported_at"], old["sha256"], old["note"]
            source = old["source"]
        self.db.execute("INSERT OR REPLACE INTO list_meta VALUES (?,?,?,?,?,?,?)",
                        (name, now_iso(), exported_at, source, cnt, sha256, note))

    def list_count(self, name):
        return self.db.execute("SELECT COUNT(*) c FROM list_entries WHERE list_name=?",
                               (name,)).fetchone()["c"]

    def list_meta(self, name):
        return self.db.execute("SELECT * FROM list_meta WHERE list_name=?", (name,)).fetchone()

    def in_list(self, name, handle, account_id=None):
        r = self.db.execute(
            "SELECT 1 FROM list_entries WHERE list_name=? AND (handle=? OR (account_id IS NOT NULL AND account_id=?))",
            (name, handle, account_id or "\0")).fetchone()
        return r is not None

    # transactions
    def tx(self):
        store = self

        class _Tx:
            def __enter__(self):
                store.db.execute("BEGIN IMMEDIATE")

            def __exit__(self, et, ev, tb):
                store.db.execute("ROLLBACK" if et else "COMMIT")
        return _Tx()

    # attempts
    def counted_attempt_times(self):
        rows = self.db.execute("SELECT ts_utc FROM attempts").fetchall()
        return [datetime.fromisoformat(r["ts_utc"]) for r in rows]

    def attempted(self, handle, account_id=None):
        r = self.db.execute(
            "SELECT 1 FROM attempts WHERE handle=? OR (account_id IS NOT NULL AND account_id=?)",
            (handle, account_id or "\0")).fetchone()
        return r is not None
