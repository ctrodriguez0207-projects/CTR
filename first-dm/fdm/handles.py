import re

_VALID = re.compile(r"^[a-z0-9._]{1,30}$")


def normalize(raw):
    """Trim whitespace and one leading @, lowercase. Periods/underscores kept.
    Returns None for anything that is not a plausible Instagram handle."""
    if raw is None:
        return None
    h = str(raw).strip()
    if h.startswith("@"):
        h = h[1:].strip()
    h = h.lower()
    return h if _VALID.match(h) else None
