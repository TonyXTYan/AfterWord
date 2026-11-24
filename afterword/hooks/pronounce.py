"""Pronunciation list: rewrite words before they are spoken.

    [pronounce]                 # every line
    "SQLite" = "sequel lite"
    "uv" = "you vee"

    [profiles.tutor.pronounce]  # this profile only; overrides global keys
    "GIF" = "jif"

Matching rules (docs/design-v2.md, section D):

- Longest key first, one pass: a replacement is never matched again.
- A key whose first character is a word character only matches where the
  character before it is not one, and likewise at the end, so `uv` does not
  hit `uvicorn` and `C++` does not hit `ABC++`. Keys that begin and end with
  punctuation (`->`) or CJK characters match anywhere. CJK characters do not
  count as word characters, so `uv` still matches in `用uv安装`.
- Smart case: an all-lowercase key matches case-insensitively; a key with any
  capital letter matches exactly. At the same length an exact key wins over a
  case-insensitive one, then profile entries win over global ones.

Used by the daemon (before synthesis and in lab previews) and by the hook's
`say` fallback, so it is stdlib-only and Python 3.9-compatible. Nothing here
raises on odd input: bad entries are skipped.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["MAX_ENTRIES", "MAX_KEY", "MAX_VALUE", "clean_table", "merge", "pairs_from_config", "apply", "is_word_char"]

MAX_ENTRIES = 500   # per table
MAX_KEY = 100
MAX_VALUE = 300

# Scripts written without spaces between words: their characters are not
# "word characters" for the boundary rule.
_CJK = (
    "ᄀ-ᇿ"      # Hangul Jamo
    "⺀-⿟"      # CJK radicals
    "々-〇"      # 々 〆 〇
    "぀-ヿ"      # Hiragana, Katakana
    "㄰-㆏"      # Hangul compatibility Jamo
    "ㇰ-ㇿ"      # Katakana extensions
    "㐀-䶿"      # CJK extension A
    "一-鿿"      # CJK unified
    "가-힯"      # Hangul syllables
    "豈-﫿"      # CJK compatibility
    "ｦ-ﾟ"      # half-width Katakana
    "\U00020000-\U0003ffff"  # CJK extensions B and later
)
_W = "[^\\W" + _CJK + "]"
_W_RE = re.compile(_W)

Pairs = Tuple[Tuple[str, str], ...]


def is_word_char(ch: str) -> bool:
    return bool(ch) and _W_RE.match(ch) is not None


def clean_table(table: Any, errors: Optional[List[str]] = None, where: str = "pronounce") -> Pairs:
    """The usable (key, value) pairs of one parsed TOML table, in order.
    Non-string values, empty keys and over-long entries are skipped (and
    described in `errors` when a list is given)."""
    if not isinstance(table, dict):
        if table is not None and errors is not None:
            errors.append("%s: expected a table of \"word\" = \"spoken as\" pairs" % where)
        return ()
    out = []
    for key, value in table.items():
        if not isinstance(key, str) or not key.strip():
            if errors is not None:
                errors.append("%s: empty key skipped" % where)
            continue
        label = "%s.%s" % (where, key if len(key) <= 40 else key[:40] + "…")
        if not isinstance(value, str):
            if errors is not None:
                errors.append("%s: expected a string, got %r" % (label, value if not isinstance(value, dict) else "a table"))
            continue
        if len(key) > MAX_KEY:
            if errors is not None:
                errors.append("%s: key longer than %d characters" % (label, MAX_KEY))
            continue
        if len(value) > MAX_VALUE:
            if errors is not None:
                errors.append("%s: value longer than %d characters" % (label, MAX_VALUE))
            continue
        if len(out) >= MAX_ENTRIES:
            if errors is not None:
                errors.append("%s: more than %d entries; the rest are ignored" % (where, MAX_ENTRIES))
            break
        out.append((key, value))
    return tuple(out)


def merge(global_pairs: Sequence[Tuple[str, str]], profile_pairs: Sequence[Tuple[str, str]]) -> Pairs:
    """Profile entries first (they win ties), then global ones they do not
    override."""
    seen = set(k for k, _ in profile_pairs)
    return tuple(profile_pairs) + tuple((k, v) for k, v in global_pairs if k not in seen)


def pairs_from_config(cfg: Any, profile: Optional[str]) -> Pairs:
    """The merged table for `profile` from a raw parsed config dict (the
    hook's view: tomllib or the mini parser). Like the daemon, an unknown
    profile name borrows the first profile's entries."""
    if not isinstance(cfg, dict):
        return ()
    glob = clean_table(cfg.get("pronounce"))
    prof: Pairs = ()
    profiles = cfg.get("profiles")
    if isinstance(profiles, dict) and profiles:
        table = profiles.get(profile) if profile in profiles else next(iter(profiles.values()))
        if isinstance(table, dict):
            prof = clean_table(table.get("pronounce"))
    return merge(glob, prof)


# Matching is a scan, not one big regex: Python's re tries every alternative
# at every position (no trie), and a CJK-aware boundary lookaround costs about
# 1 ms to compile per key, so a few hundred keys would stall every new line.
# Keys are bucketed by their case-folded first character; at each position
# only that bucket is tried, in priority order, and the word-boundary rule is
# checked in Python.
_CACHE: Dict[Pairs, Any] = {}
_CACHE_MAX = 32
_KEY_RE: Dict[str, Any] = {}
_KEY_RE_MAX = 4096


def _folds(ch: str) -> Tuple[str, ...]:
    """Bucket labels a text character can match under smart case."""
    return tuple(set((ch, ch.lower(), ch.casefold()[:1], ch.upper()[:1].lower())))


def _index(pairs: Pairs):
    hit = _CACHE.get(pairs)
    if hit is not None:
        return hit
    order = sorted(
        (i for i in range(len(pairs)) if pairs[i][0]),
        key=lambda i: (-len(pairs[i][0]), pairs[i][0] == pairs[i][0].lower(), i),
    )
    entries = []  # priority order: (key, value, case-insensitive)
    buckets: Dict[str, List[int]] = {}
    for rank, i in enumerate(order):
        key, value = pairs[i]
        fold = key == key.lower()
        entries.append((key, value, fold))
        labels = set([key[0]])
        if fold:
            labels.update(_folds(key[0]))
        for label in labels:
            buckets.setdefault(label, []).append(rank)
    entry = (entries, buckets)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[pairs] = entry
    return entry


def _match_end(text: str, pos: int, key: str, fold: bool) -> int:
    """End of `key` matched at `pos`, or -1."""
    if not fold:
        return pos + len(key) if text.startswith(key, pos) else -1
    rx = _KEY_RE.get(key)
    if rx is None:
        if len(_KEY_RE) >= _KEY_RE_MAX:
            _KEY_RE.clear()
        rx = _KEY_RE[key] = re.compile(re.escape(key), re.IGNORECASE)
    m = rx.match(text, pos)
    return m.end() if m is not None else -1


def _replace(text: str, entries, buckets) -> str:
    out = []
    n = len(text)
    done = 0   # text[:done] is already in `out`
    pos = 0
    cands: Dict[str, List[int]] = {}  # per distinct character: candidate ranks
    prev_word = False                 # is text[pos - 1] a word character
    while pos < n:
        ch = text[pos]
        ranks = cands.get(ch)
        if ranks is None:
            seen = set()
            for label in _folds(ch):
                seen.update(buckets.get(label, ()))
            ranks = cands[ch] = sorted(seen)
        hit = None
        for rank in ranks:
            key, value, fold = entries[rank]
            end = _match_end(text, pos, key, fold)
            if end < 0:
                continue
            if prev_word and is_word_char(key[0]):
                continue
            if end < n and is_word_char(key[-1]) and is_word_char(text[end]):
                continue
            hit = (value, end)
            break
        if hit is None:
            prev_word = is_word_char(ch)
            pos += 1
            continue
        out.append(text[done:pos])
        out.append(hit[0])
        prev_word = is_word_char(text[hit[1] - 1])
        done = pos = hit[1]
    out.append(text[done:])
    return "".join(out)


def apply(text: str, pairs: Sequence[Tuple[str, str]]) -> str:
    """`text` with every listed word replaced by how it should be spoken.
    Returns `text` unchanged when the table is empty or anything goes wrong."""
    if not text or not pairs:
        return text
    try:
        # Only keys that occur in the text are indexed: cheap substring tests
        # keep the per-position candidate lists short.
        lowered = text.lower()
        present = tuple(
            (str(k), str(v)) for k, v in pairs
            if k and (k in text if k != k.lower() else k in lowered)
        )
        if not present:
            return text
        entries, buckets = _index(present)
        if not entries:
            return text
        return _replace(text, entries, buckets)
    except Exception:
        return text
