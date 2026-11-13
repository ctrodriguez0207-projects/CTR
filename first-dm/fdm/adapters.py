"""Instagram access is behind an interface so every check reports whether it
is actually verifiable. Only adapters whose capabilities have been verified
can make live sending possible."""
import json
from pathlib import Path


class Unavailable(Exception):
    """A capability cannot be verified right now (workflow-wide)."""


class Unverifiable(Exception):
    """A check could not be verified for this one person."""


class AccountWarning(Exception):
    """Instagram warning, action block, login challenge, or rate limit. Pauses everything."""


CAPABILITIES = [
    "whoami",             # which account the session is signed into
    "list_owned_reels",
    "list_reel_likers",
    "sender_follows",     # check 1
    "dm_history",         # check 5: any thread / any prior DM, incl. manual
    "can_message",        # check 7
    "send_dm",
    "confirm_dm",
]


class InstagramAdapter:
    name = "base"

    def capabilities(self):
        """{capability: (available: bool, how/why: str)}"""
        raise NotImplementedError

    def whoami(self): raise Unavailable("whoami")
    def list_owned_reels(self): raise Unavailable("list_owned_reels")
    def list_reel_likers(self, reel_id): raise Unavailable("list_reel_likers")
    def sender_follows(self, handle, account_id=None): raise Unavailable("sender_follows")
    def dm_thread_exists(self, handle, account_id=None): raise Unavailable("dm_history")
    def can_message(self, handle, account_id=None): raise Unavailable("can_message")
    def send_dm(self, handle, text): raise Unavailable("send_dm")
    def confirm_dm(self, handle, text): raise Unavailable("confirm_dm")


class NoInstagramAccess(InstagramAdapter):
    """What this environment actually has: nothing verified."""
    name = "none"

    def __init__(self, why):
        self.why = why

    def capabilities(self):
        return {c: (False, self.why) for c in CAPABILITIES}


class FixtureInstagram(InstagramAdapter):
    """Test data only. Never used for live sending (enable-live refuses it)."""
    name = "fixture"
    is_test_data = True

    def __init__(self, path_or_dict):
        d = path_or_dict if isinstance(path_or_dict, dict) else json.loads(Path(path_or_dict).read_text())
        self.d = d
        self.sent = []

    def capabilities(self):
        missing = set(self.d.get("unavailable_capabilities", []))
        return {c: (c not in missing, "TEST DATA fixture") for c in CAPABILITIES}

    def _req(self, cap):
        if cap in self.d.get("unavailable_capabilities", []):
            raise Unavailable(cap)

    def whoami(self):
        self._req("whoami")
        return self.d["signed_in_as"]

    def list_owned_reels(self):
        self._req("list_owned_reels")
        return [{"reel_id": r["reel_id"], "owner": r["owner"]} for r in self.d["reels"]]

    def list_reel_likers(self, reel_id):
        self._req("list_reel_likers")
        r = next(r for r in self.d["reels"] if r["reel_id"] == reel_id)
        return [(l["handle"], l.get("account_id")) for l in r["likers"]]

    def _per_person(self, key, handle):
        v = self.d.get(key, {})
        if handle in self.d.get("unverifiable", {}).get(key, []):
            raise Unverifiable(f"{key} unverifiable for {handle}")
        return handle in v if isinstance(v, list) else v.get(handle, False)

    def sender_follows(self, handle, account_id=None):
        self._req("sender_follows")
        return self._per_person("sender_following", handle)

    def dm_thread_exists(self, handle, account_id=None):
        self._req("dm_history")
        return self._per_person("dm_threads", handle)

    def can_message(self, handle, account_id=None):
        self._req("can_message")
        return not self._per_person("cannot_message", handle)

    def send_dm(self, handle, text):
        self._req("send_dm")
        if handle in self.d.get("warn_on_send", []):
            raise AccountWarning("action block (test)")
        self.sent.append((handle, text))

    def confirm_dm(self, handle, text):
        self._req("confirm_dm")
        if handle in self.d.get("unconfirmable", []):
            return False
        return (handle, text) in self.sent
