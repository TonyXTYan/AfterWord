"""Split a line into per-language parts by script, for profiles that have a
voice per language ([profiles.x.langs.*]).

    split("Not quite. 别急嘛，用 Python 再试一次。 Try again.", "en")
    -> [("en", "Not quite."), ("zh", "别急嘛，用 Python 再试一次。"), ("en", "Try again.")]

Rules (docs/design-v2.md, section B):

- Han -> zh, kana -> ja (a sentence with kana is Japanese, kanji and all;
  a Han-only sentence next to a Japanese one is Japanese too), Hangul -> ko,
  everything else -> the primary language.
- Digits, punctuation, spaces, and Latin runs of at most 3 words inside a
  CJK run stay with it ("用 Python 写" stays Chinese).
- The text is cut at sentence and clause boundaries only; the one exception
  is a Latin run of 4+ words with CJK on both sides in the same clause,
  which is cut out ("我说 this is a long phrase 然后…").
- A clause that mixes scripts goes to whichever has more: CJK letters
  against Latin words, a tie going to the sentence as a whole, then to the
  primary language. So "The word 你好 means hello." stays English and
  "我喜欢 Python。" stays Chinese.
- A Latin-only clause of at most 3 words ("OK，…") takes the CJK language
  when its sentence is mostly CJK; otherwise it is the primary language
  ("Next word: 苹果, which means apple." -> en / zh / en).
- Parts with fewer than 2 letters join a neighbour (the one in the same
  sentence first); neighbouring parts with the same language merge.

Every part is a stripped substring of the input, in order. Stdlib-only and
Python 3.9-compatible: the hook's `say` fallback uses it too.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

__all__ = ["split", "script_of", "say_voice_for", "CJK_LANGS", "CJK_SAY_VOICES", "MAX_PARTS"]

CJK_LANGS = ("zh", "ja", "ko")
LATIN_MAX_WORDS = 3  # Latin runs this short stay inside a CJK sentence
MIN_LETTERS = 2      # parts with fewer letters join a neighbour
MAX_PARTS = 12       # beyond this, the rest is merged into the last part

# A sentence ends at . ! ? … followed by whitespace or the end, or at CJK
# terminal punctuation; closing quotes and brackets stay with it.
_CLOSERS = "\"'”’」』）)\\]"
_SENT_END_RE = re.compile(
    r"[.!?…]+[" + _CLOSERS + r"]*(?=\s|$)|[。！？]+[" + _CLOSERS + r"]*"
)
_CLAUSE_END_RE = re.compile(r"[,;:](?=\s)|[，、；：]")
_WORD_JOINERS = "'’-_"


# macOS voices for CJK parts whose language sets no say_voice of its own.
# The profile's say voice is for its primary language, and an English voice
# such as Samantha renders Chinese or Japanese text as silence (exit 0).
CJK_SAY_VOICES = {"zh": "Tingting", "ja": "Kyoko", "ko": "Yuna"}


def say_voice_for(lang: Optional[str], primary: Optional[str], lang_say_voice: object = None,
                  lang_voice: object = None, profile_say_voice: object = None) -> Optional[str]:
    """The macOS `say` voice for a part in `lang`: the language's own
    say_voice, else the voice of a `say:<Voice>` language voice, else (for
    zh/ja/ko when that is not the profile's primary language) a built-in
    voice for the language, else the profile's say_voice. None: the system
    voice."""
    def clean(v):
        return v.strip() if isinstance(v, str) and v.strip() else None

    own = clean(lang_say_voice)
    if own:
        return own
    spec = clean(lang_voice)
    if spec and spec.lower().startswith("say:") and clean(spec[4:]):
        return clean(spec[4:])
    if lang in CJK_SAY_VOICES and lang != primary:
        return CJK_SAY_VOICES[lang]
    return clean(profile_say_voice)


def script_of(ch: str) -> Optional[str]:
    """'zh', 'ja' or 'ko' for a CJK letter, 'L' for any other letter, None
    for digits, punctuation and spaces."""
    o = ord(ch)
    if o == 0x30FB:  # katakana middle dot, also used in Chinese
        return None
    if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF or 0xFF66 <= o <= 0xFF9F:
        return "ja"
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F or 0xA960 <= o <= 0xA97F:
        return "ko"
    if (
        0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF
        or 0x20000 <= o <= 0x3134F or o in (0x3005, 0x3007)
    ):
        return "zh"
    if ch.isalpha():
        return "L"
    return None


class _Piece:
    __slots__ = ("start", "end", "sent", "cjk", "latin", "words", "cut", "lang")

    def __init__(self, text: str, start: int, end: int, sent: int, cut: bool = False):
        self.start = start
        self.end = end
        self.sent = sent
        self.cut = cut  # a long Latin run cut out of a CJK clause
        self.cjk = {"zh": 0, "ja": 0, "ko": 0}
        self.latin = 0
        for i in range(start, end):
            s = script_of(text[i])
            if s == "L":
                self.latin += 1
            elif s is not None:
                self.cjk[s] += 1
        self.words = sum(r[2] for r in _latin_runs(text, start, end))
        self.lang: Optional[str] = None

    @property
    def is_cjk(self) -> bool:
        return sum(self.cjk.values()) > 0

    @property
    def letters(self) -> int:
        return sum(self.cjk.values()) + self.latin


def _spans(text: str, start: int, end: int, regex) -> List[Tuple[int, int]]:
    """[start, end) cut after each match of `regex`."""
    spans = []
    pos = start
    for m in regex.finditer(text, start, end):
        if m.end() > pos:
            spans.append((pos, m.end()))
            pos = m.end()
    if pos < end:
        spans.append((pos, end))
    return spans


def _latin_runs(text: str, start: int, end: int) -> List[Tuple[int, int, int]]:
    """(start, end, words) of each stretch of non-CJK words in [start, end).
    Spaces, digits and punctuation do not end a stretch; a CJK letter does."""
    runs = []
    cur = -1
    last = -1
    words = 0
    i = start
    while i < end:
        s = script_of(text[i])
        if s == "L":
            j = i + 1
            while j < end and (
                script_of(text[j]) == "L" or text[j].isdigit()
                or (text[j] in _WORD_JOINERS and j + 1 < end and script_of(text[j + 1]) == "L")
            ):
                j += 1
            if cur < 0:
                cur = i
            last = j
            words += 1
            i = j
        elif s is None:
            i += 1
        else:
            if cur >= 0:
                runs.append((cur, last, words))
            cur, words = -1, 0
            i += 1
    if cur >= 0:
        runs.append((cur, last, words))
    return runs


def _pieces(text: str) -> List[_Piece]:
    pieces: List[_Piece] = []
    for sent, (s_start, s_end) in enumerate(_spans(text, 0, len(text), _SENT_END_RE)):
        for a, b in _spans(text, s_start, s_end, _CLAUSE_END_RE):
            clause = _Piece(text, a, b, sent)
            if not clause.is_cjk:
                pieces.append(clause)
                continue
            # A CJK clause: cut out Latin runs longer than LATIN_MAX_WORDS
            # that sit between CJK letters (there is no punctuation to cut
            # at). A long run at the edge of the clause is not cut: the
            # clause is then decided as a whole (see _split).
            cjk_at = [i for i in range(a, b) if script_of(text[i]) in CJK_LANGS]
            first, last = cjk_at[0], cjk_at[-1]
            pos = a
            for r_start, r_end, words in _latin_runs(text, a, b):
                if words <= LATIN_MAX_WORDS or r_start < first or r_end > last:
                    continue
                if r_start > pos:
                    pieces.append(_Piece(text, pos, r_start, sent))
                pieces.append(_Piece(text, r_start, r_end, sent, cut=True))
                pos = r_end
            if pos < b:
                pieces.append(_Piece(text, pos, b, sent))
    return pieces


def _sentence_langs(pieces: List[_Piece]) -> dict:
    """The CJK language of each sentence with CJK letters."""
    totals: dict = {}
    for p in pieces:
        if p.is_cjk:
            tot = totals.setdefault(p.sent, {"zh": 0, "ja": 0, "ko": 0})
            for k, v in p.cjk.items():
                tot[k] += v
    langs = {s: ("ja" if t["ja"] else ("ko" if t["ko"] else "zh")) for s, t in totals.items()}
    for s in sorted(langs):
        if langs[s] == "zh" and "ja" in (langs.get(s - 1), langs.get(s + 1)):
            langs[s] = "ja"  # a kanji-only sentence among Japanese ones
    return langs


def split(text: str, primary: Optional[str], other: Optional[str] = None) -> List[Tuple[Optional[str], str]]:
    """[(lang, part)] in order. `primary` is the language of non-CJK text
    (None or "auto": unknown, returned as None). When `primary` is itself
    zh/ja/ko, `other` (if given) is used for non-CJK text instead. Never
    raises: on any problem the whole text comes back as one part."""
    if not isinstance(text, str) or not text.strip():
        return []
    latin_lang = primary if primary not in (None, "", "auto") else None
    if latin_lang in CJK_LANGS and other:
        latin_lang = other
    try:
        return _split(text, latin_lang)
    except Exception:
        return [(latin_lang, text.strip())]


def _split(text: str, latin_lang: Optional[str]) -> List[Tuple[Optional[str], str]]:
    parts = [p for p in _pieces(text) if text[p.start:p.end].strip()]
    sent_lang = _sentence_langs(parts)
    # How CJK each sentence is: CJK letters against Latin words, leaving out
    # long runs that were cut out (they are spoken apart anyway).
    weight: dict = {}
    for p in parts:
        if not p.cut:
            w = weight.setdefault(p.sent, [0, 0])
            w[0] += sum(p.cjk.values())
            w[1] += p.words
    mostly_cjk = {s: w[0] > w[1] for s, w in weight.items()}
    for p in parts:
        cjk = sum(p.cjk.values())
        if p.cut or p.sent not in sent_lang:
            p.lang = latin_lang
        elif p.is_cjk:
            wins = cjk > p.words or (cjk == p.words and mostly_cjk.get(p.sent, False))
            p.lang = sent_lang[p.sent] if wins else latin_lang
        elif p.latin and p.words <= LATIN_MAX_WORDS and mostly_cjk.get(p.sent, False):
            p.lang = sent_lang[p.sent]  # a short Latin clause in a CJK sentence
        else:
            p.lang = latin_lang
    real = [i for i, p in enumerate(parts) if p.letters >= MIN_LETTERS]
    if not real:
        lang = next((p.lang for p in parts if p.letters), latin_lang)
        return [(lang, text.strip())] if text.strip() else []
    # Each tiny part joins a real one: the previous in its sentence, else the
    # next in its sentence, else the previous, else the next.
    owner = {}
    for i, p in enumerate(parts):
        if p.letters >= MIN_LETTERS:
            owner[i] = i
            continue
        prev = max((j for j in real if j < i), default=None)
        nxt = min((j for j in real if j > i), default=None)
        if prev is not None and parts[prev].sent == p.sent:
            owner[i] = prev
        elif nxt is not None and parts[nxt].sent == p.sent:
            owner[i] = nxt
        else:
            owner[i] = prev if prev is not None else nxt
    out: List[List] = []  # [lang, start, end]
    last_owner = None
    for i, p in enumerate(parts):
        o = owner[i]
        lang = parts[o].lang
        if out and (o == last_owner or out[-1][0] == lang):
            out[-1][2] = p.end
        else:
            out.append([lang, p.start, p.end])
        last_owner = o
    if len(out) > MAX_PARTS:
        out[MAX_PARTS - 1][2] = out[-1][2]
        del out[MAX_PARTS:]
    return [(lang, text[a:b].strip()) for lang, a, b in out if text[a:b].strip()]
