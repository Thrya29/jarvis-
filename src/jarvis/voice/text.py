"""Text helpers for speech: cleanup for TTS, sentence chunking, intent words."""

from __future__ import annotations

import re

_WAKE_PREFIX = re.compile(r"^\s*(?:hey|hi|ok|okay)?[\s,]*jarvis\b[\s,.!?:-]*", re.IGNORECASE)
_WIN_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s\"'<>|]+\\)*([^\\\s\"'<>|]+)")
_URL = re.compile(r"https?://\S+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")

YES = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "go ahead", "do it", "please do",
    "confirm", "confirmed", "approve", "approved", "allow", "affirmative", "go for it",
}  # fmt: skip
NO = {
    "no", "nope", "nah", "don't", "do not", "stop", "cancel", "deny", "denied", "negative",
    "never mind", "nevermind", "not now", "wait",
}  # fmt: skip
STOP_PHRASES = {
    "stop", "stop it", "stop that", "cancel", "cancel that", "never mind", "nevermind",
    "hold on", "wait", "shut up", "be quiet", "quiet", "abort", "that's enough", "enough",
}  # fmt: skip
BACKCHANNELS = {
    "ok", "okay", "yeah", "yes", "uh huh", "mhm", "mm", "hmm", "right", "sure", "got it",
    "alright", "all right", "cool", "nice", "great", "thanks", "thank you", "i see",
}  # fmt: skip


def normalize(text: str) -> str:
    """Lower-case, punctuation removed, single spaces."""
    return " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())


def strip_wake_word(text: str) -> str:
    return _WAKE_PREFIX.sub("", text, count=1).strip()


def is_stop(text: str) -> bool:
    return normalize(text) in STOP_PHRASES


def is_backchannel(text: str) -> bool:
    return normalize(text) in BACKCHANNELS


def parse_yes_no(text: str) -> bool | None:
    """True/False for a clear yes/no answer, None if ambiguous."""
    t = normalize(text)
    if not t:
        return None
    words = set(t.split())
    has_no = any(p in t if " " in p else p in words for p in NO)
    has_yes = any(p in t if " " in p else p in words for p in YES)
    if has_no and not has_yes:
        return False
    if has_yes and not has_no:
        return True
    return None


def for_speech(text: str) -> str:
    """Make agent text pleasant to hear: no markdown, short path and link mentions."""
    t = re.sub(r"```.*?```", " (code shown on screen) ", text, flags=re.DOTALL)
    t = _URL.sub("a link", t)
    t = _WIN_PATH.sub(lambda m: m.group(1), t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*\d+[.)]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", t)
    t = re.sub(r"\|", ", ", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"\.\s*\.", ".", t)
    return re.sub(r"\s{2,}", " ", t).strip(" .") + ("." if t.strip() else "")


def sentences(text: str, max_chars: int = 220) -> list[str]:
    """Split into speakable chunks so the first one can start playing quickly."""
    out: list[str] = []
    for s in _SENTENCE.split(text.strip()):
        s = s.strip()
        while len(s) > max_chars:
            cut = s.rfind(", ", 0, max_chars)
            cut = cut + 1 if cut > 40 else max_chars
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            out.append(s)
    return out
