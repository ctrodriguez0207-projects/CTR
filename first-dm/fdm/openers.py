"""Four-opener rotation. Each opener has light, pre-approved wording variants
that keep the same thank-you + the same single question. No slang beyond the
originals, no compliments, no links, no pitches."""
import hashlib

OPENERS = {
    1: [
        "Hey man, appreciate you liking the vid! Where are you based?",
        "Hey man, appreciate you liking the vid! Where are you based out of?",
        "Hey man, appreciate the like on the vid! Where are you based?",
    ],
    2: [
        "Hey bro, thanks for liking the vid! Do you lift too or did the video just catch your eye?",
        "Hey bro, thanks for liking the vid! You lift too or did the video just catch your eye?",
    ],
    3: [
        "Hey man, appreciate the like! What would you wanna see more of on my page?",
        "Hey man, appreciate the like! What'd you wanna see more of on my page?",
    ],
    4: [
        "Hey bro, thanks for liking the video! Any fitness topics you’d want me to cover?",
        "Hey bro, thanks for liking the video! Any fitness topics you’d like me to cover?",
    ],
}


def opener_number(rotation_pos):
    """rotation_pos counts recorded send attempts (0-based)."""
    return (rotation_pos % 4) + 1


def compose(rotation_pos, handle):
    n = opener_number(rotation_pos)
    variants = OPENERS[n]
    # Deterministic per person so a dry run and the live send agree.
    idx = int(hashlib.sha256(handle.encode()).hexdigest(), 16) % len(variants)
    return n, variants[idx]
