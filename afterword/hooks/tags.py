"""Parse AfterWord tags out of a Claude reply.

Two tags exist:

    <say mood="dry" lang="ja" pace="1.1">one or two sentences</say>
    <voice set="nova"/>        <voice set="off"/>

Rules:
- Tag names are case-insensitive. Attribute values may use double quotes,
  single quotes, or no quotes.
- Tags inside Markdown code (fenced blocks, inline code spans) and HTML
  comments are ignored, so a reply can *talk about* the protocol without
  triggering it.
- Every non-empty <say> is kept, in order, at most MAX_SAYS of them (extra
  ones are dropped with a warning); `.say` is the last one (the v1 rule).
  The last valid <voice> wins.
- Unknown or invalid attributes are dropped and reported in `warnings`.

This module is stdlib-only and must stay importable on Python 3.9+, because
the hook runs under whichever python3 Claude Code is configured to call.
"""

from __future__ import annotations

import bisect
import html
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

__all__ = [
    "strip_speech_commands",
    "SayTag",
    "VoiceTag",
    "ParseResult",
    "parse",
    "clean_text",
    "truncate",
    "limit_says",
    "MAX_SAYS",
    "PROFILE_NAME_RE",
]

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")  # \Z: no trailing newline
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*$")

PACE_MIN = 0.5
PACE_MAX = 2.0
MOOD_MAX = 64
MAX_SAYS = 4  # <say> blocks spoken per reply; extra ones are dropped

# Opening tag name must be followed by whitespace, '/' or '>' so that
# <sayings> or <voice-over> never match.
# Attribute runs are bounded so adversarial input stays linear.
_SAY_OPEN_RE = re.compile(r"<say(?=[\s/>])([^<>]{0,2000})>", re.IGNORECASE)
_SAY_CLOSE_RE = re.compile(r"</say\s*>", re.IGNORECASE)
_VOICE_RE = re.compile(r"<voice(?=[\s/>])([^<>]{0,2000})>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"""([A-Za-z_][\w-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`/]+))"""
)

_MASK = "\x00"


SEGMENT_PREVIEW = 200  # characters of each block kept in to_dict()


@dataclass
class SayTag:
    text: str  # cleaned, speakable text (not yet truncated)
    attrs: Dict[str, object]  # normalised: mood:str, lang:str, pace:float
    raw: str
    start: int


@dataclass
class VoiceTag:
    profile: Optional[str]  # None means "off"
    raw: str
    start: int


@dataclass
class ParseResult:
    says: List[SayTag] = field(default_factory=list)  # non-empty, in order, at most MAX_SAYS
    voices: List[VoiceTag] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    # For text that is still being written (early speaking): where a later
    # closer could still change the parse. `open_says`: starts of <say>
    # openers left unpaired (a later </say> pairs with them and nests the
    # blocks after them); `open_comments`: starts of `<!--` with no `-->`
    # after them (a later `-->` masks everything up to it).
    open_says: List[int] = field(default_factory=list)
    open_comments: List[int] = field(default_factory=list)

    @property
    def say(self) -> Optional[SayTag]:
        """The <say> to speak: the last one with non-empty text."""
        for tag in reversed(self.says):
            if tag.text:
                return tag
        return None

    @property
    def voice(self) -> Optional[VoiceTag]:
        """The effective <voice>: the last valid one."""
        return self.voices[-1] if self.voices else None

    def to_dict(self) -> dict:
        say = self.say
        voice = self.voice
        return {
            "say": None if say is None else {"text": say.text, "attrs": dict(say.attrs)},
            # A preview of each block (the spoken text goes in the hook's
            # own `segments`); `chars` is the block's full length.
            "segments": [
                {"text": t.text[:SEGMENT_PREVIEW], "chars": len(t.text), "attrs": dict(t.attrs)} for t in self.says
            ],
            "voice": None if voice is None else {"set": voice.profile or "off"},
            "say_count": len(self.says),
            "voice_count": len(self.voices),
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# Masking of regions where tags must not count                                #
# --------------------------------------------------------------------------- #

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_COMMENT_END_RE = re.compile(r"-->")
_INLINE_TOKEN_RE = re.compile(r"`+|<!--")
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")


def _paragraphs(text: str):
    """(start, end) of each blank-line-separated block."""
    start = 0
    for m in _BLANK_LINE_RE.finditer(text):
        yield start, m.start()
        start = m.end()
    yield start, len(text)


def _mask_span(chars: List[str], start: int, end: int) -> None:
    for i in range(start, end):
        if chars[i] != "\n":
            chars[i] = _MASK


def _mask_code(text: str, open_comments: Optional[List[int]] = None) -> str:
    """Return a copy of `text`, same length, with code and comments masked.

    Positions are preserved, so a match found in the masked copy can be
    sliced from the original text. The start of each comment that has no
    `-->` after it is appended to `open_comments` when given.
    """
    chars = list(text)

    # 1. Fenced code blocks (``` or ~~~). An unclosed fence runs to the end,
    #    as in CommonMark.
    pos = 0
    lines = text.splitlines(keepends=True)
    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_start = 0
    for line in lines:
        line_end = pos + len(line)
        if not in_fence:
            m = _FENCE_OPEN_RE.match(line)
            # A backtick fence's info string may not contain backticks.
            if m and not (m.group(1)[0] == "`" and "`" in line[m.end():]):
                in_fence = True
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                fence_start = pos
        else:
            stripped = line.strip()
            if (
                len(stripped) >= fence_len
                and set(stripped) == {fence_char}
                and len(line) - len(line.lstrip(" ")) <= 3
            ):
                _mask_span(chars, fence_start, line_end)
                in_fence = False
        pos = line_end
    if in_fence:
        _mask_span(chars, fence_start, len(text))

    masked = "".join(chars)

    # 2. Inline code spans and HTML comments, in one left-to-right scan so
    #    whichever construct starts first wins (`<!--` inside backticks is
    #    code, a backtick inside a comment is comment). A code span is a run
    #    of N backticks closed by the next run of exactly N backticks in the
    #    same paragraph; unmatched runs are literal. A comment ends at the
    #    next `-->`; an unclosed one ends with its paragraph.
    comment_ends = [m.end() for m in _COMMENT_END_RE.finditer(masked)]
    consumed = 0
    for block_start, block_end in _paragraphs(masked):
        start = max(block_start, consumed)
        if start >= block_end:
            continue
        tokens = list(_INLINE_TOKEN_RE.finditer(masked, start, block_end))
        by_len: Dict[int, List[int]] = {}
        for idx, tok in enumerate(tokens):
            if tok.group(0)[0] == "`":
                by_len.setdefault(len(tok.group(0)), []).append(idx)
        for idx, tok in enumerate(tokens):
            if tok.start() < consumed:
                continue
            if tok.group(0)[0] == "<":
                k = bisect.bisect_left(comment_ends, tok.end() + 3)
                end = comment_ends[k] if k < len(comment_ends) else block_end
                if k >= len(comment_ends) and open_comments is not None:
                    open_comments.append(tok.start())
                _mask_span(chars, tok.start(), end)
                consumed = end
            else:
                same = by_len[len(tok.group(0))]
                k = bisect.bisect_right(same, idx)
                while k < len(same) and tokens[same[k]].start() < consumed:
                    k += 1
                if k < len(same):
                    end = tokens[same[k]].end()
                    _mask_span(chars, tok.start(), end)
                    consumed = end

    return "".join(chars)


# --------------------------------------------------------------------------- #
# Attribute handling                                                          #
# --------------------------------------------------------------------------- #


def _parse_attrs(raw: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for m in _ATTR_RE.finditer(raw):
        name = m.group(1).lower()
        value = next(g for g in (m.group(2), m.group(3), m.group(4)) if g is not None)
        attrs[name] = html.unescape(value).strip()
    return attrs


def _normalise_say_attrs(raw: Dict[str, str], warnings: List[str]) -> Dict[str, object]:
    out: Dict[str, object] = {}
    for name, value in raw.items():
        if name == "pace":
            try:
                pace = float(value)
            except ValueError:
                warnings.append("say: invalid pace %r dropped" % value)
                continue
            if pace != pace or pace in (float("inf"), float("-inf")):
                warnings.append("say: invalid pace %r dropped" % value)
                continue
            clamped = min(PACE_MAX, max(PACE_MIN, pace))
            if clamped != pace:
                warnings.append("say: pace %s clamped to %s" % (value, clamped))
            out["pace"] = clamped
        elif name == "lang":
            if _LANG_RE.match(value):
                out["lang"] = value.lower().replace("_", "-")
            else:
                warnings.append("say: invalid lang %r dropped" % value)
        elif name == "mood":
            mood = " ".join(value.split())[:MOOD_MAX]
            if mood:
                out["mood"] = mood
        else:
            warnings.append("say: unknown attribute %r dropped" % name)
    return out


# --------------------------------------------------------------------------- #
# Text cleaning                                                               #
# --------------------------------------------------------------------------- #

# Only real HTML-ish tags (and our own) are removed from speech, so prose
# such as "x<y and z>w" or "Use <T> generics" keeps its words.
_TAG_LIKE_RE = re.compile(
    r"</?(?:say|voice|b|i|u|s|em|strong|code|kbd|samp|var|span|div|p|br|hr|a|mark|small"
    r"|sub|sup|del|ins|q|cite|abbr|li|ul|ol|h[1-6]|pre|blockquote|font|tt)"
    r"(?:\s[^<>]{0,1000})?/?>",
    re.IGNORECASE,
)
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
# Paired emphasis only: "2**10" and "a ** b" keep their stars.
_MD_EMPH_RE = re.compile(r"(\*\*|__|~~)(?=\S)(.+?)(?<=\S)\1")
_MD_LINE_PREFIX_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+)", re.MULTILINE)
_SINGLE_STAR_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_SPEECH_CMD_RE = re.compile(r"\[\[[^\[\]]{0,200}\]\]")


def strip_speech_commands(text: str) -> str:
    """Remove macOS `say` embedded commands such as [[slnc 5000]] or
    [[inpt PHON]], which would otherwise be obeyed, not spoken."""
    t = _SPEECH_CMD_RE.sub(" ", text)
    return t.replace("[[", "[ [").replace("]]", "] ]")


def clean_text(text: str) -> str:
    """Turn the inside of a <say> into plain speakable text."""
    if not text:
        return ""
    t = _TAG_LIKE_RE.sub(" ", text)
    t = html.unescape(t)
    t = _MD_LINK_RE.sub(r"\1", t)
    t = _MD_LINE_PREFIX_RE.sub("", t)
    t = _MD_EMPH_RE.sub(r"\2", t)
    t = _SINGLE_STAR_RE.sub(r"\1", t)
    t = t.replace("`", "")
    t = t.replace(_MASK, "")
    t = strip_speech_commands(t)
    # Drop other control characters; keep normal whitespace for splitting.
    t = "".join(ch for ch in t if ch >= " " or ch in "\t\n\r")
    return " ".join(t.split())


# A sentence end is terminal punctuation followed by whitespace (so "3.14"
# is not one), or CJK terminal punctuation anywhere.
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s)|[。！？]")


def truncate(text: str, max_chars: int) -> str:
    """Shorten `text` to at most `max_chars`, preferring a sentence end,
    then a word boundary."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = text[:max_chars]
    # Scan one char past the cut so the lookahead sees real context.
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(text, 0, max_chars + 1) if m.end() <= max_chars]
    if ends and ends[-1] >= max_chars // 2:
        return head[: ends[-1]].rstrip()
    space = head.rfind(" ")
    if space >= max_chars // 2:
        return head[:space].rstrip(" ,;:-")
    return head.rstrip()


MIN_SAY_CHARS = 20  # a later <say> is dropped when less than this is left of the total


def limit_says(says: List[SayTag], max_chars: int, keep_index: bool = False) -> List[dict]:
    """The <say> blocks as they are spoken: each truncated to `max_chars`,
    all of them together to 2 x `max_chars` (counting a joining space).
    A later block that would get fewer than MIN_SAY_CHARS is dropped.
    Returns [{"text", "attrs"}] in order (plus "i", the block's index in
    `says`, with keep_index). The result for the first k blocks does not
    depend on later ones."""
    out: List[dict] = []
    budget = 2 * max_chars if max_chars > 0 else 0
    for i, tag in enumerate(says):
        if not tag.text:
            continue
        if max_chars > 0:
            room = min(max_chars, budget - (1 if out else 0))
            if room < (MIN_SAY_CHARS if out else 1):
                break
            text = truncate(tag.text, room)
        else:
            text = tag.text
        if not text:
            continue
        out.append({"text": text, "attrs": dict(tag.attrs)})
        if keep_index:
            out[-1]["i"] = i
        budget -= len(text) + (1 if len(out) > 1 else 0)
    return out


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #


def parse(message: object) -> ParseResult:
    """Parse a reply. Never raises; non-string input yields an empty result."""
    result = ParseResult()
    if not isinstance(message, str) or not message:
        return result

    masked = _mask_code(message, result.open_comments)

    # Pair each </say> with the nearest preceding unpaired <say>, like
    # brackets. A bare "<say>" mentioned in prose then stays unpaired instead
    # of swallowing everything up to the real tag's closer.
    marks = [(m.start(), 0, m) for m in _SAY_OPEN_RE.finditer(masked)]
    marks += [(m.start(), 1, m) for m in _SAY_CLOSE_RE.finditer(masked)]
    marks.sort(key=lambda x: (x[0], x[1]))
    stack: List[re.Match] = []
    ends: Dict[int, int] = {}  # start -> end of each kept block
    for _pos, kind, m in marks:
        if kind == 0:
            if not m.group(1).rstrip().endswith("/"):  # <say …/> is empty
                stack.append(m)
            continue
        if not stack:
            continue  # stray closer
        opener = stack.pop()
        inner = message[opener.end():m.start()]
        attrs = _normalise_say_attrs(_parse_attrs(opener.group(1)), result.warnings)
        text = clean_text(inner)
        if not text:
            result.warnings.append("say: empty tag ignored")
            continue
        result.says.append(
            SayTag(text=text, attrs=attrs, raw=message[opener.start():m.end()], start=opener.start())
        )
        ends[opener.start()] = m.end()
    if stack:
        result.warnings.append("say: %d unclosed tag(s) ignored" % len(stack))
        result.open_says = [m.start() for m in stack]
    # A block nested in another one is already part of the outer block's
    # text: keep the outer one only (as v1 did), so nothing is said twice.
    # Pairs are bracket-matched, so two blocks either nest or do not touch.
    result.says.sort(key=lambda t: t.start)
    outer: List[SayTag] = []
    reach = -1
    for t in result.says:
        if t.start < reach:
            continue
        outer.append(t)
        reach = ends[t.start]
    if len(outer) < len(result.says):
        result.warnings.append("say: %d nested tag(s) ignored" % (len(result.says) - len(outer)))
        result.says = outer
    if len(result.says) > MAX_SAYS:
        result.warnings.append(
            "say: %d extra tag(s) dropped (at most %d per reply)" % (len(result.says) - MAX_SAYS, MAX_SAYS)
        )
        del result.says[MAX_SAYS:]

    for m in _VOICE_RE.finditer(masked):
        raw = message[m.start():m.end()]
        attrs = _parse_attrs(m.group(1))
        value = attrs.get("set")
        if value is None:
            result.warnings.append("voice: tag without set= ignored: %s" % raw[:80])
            continue
        if value.lower() == "off":
            result.voices.append(VoiceTag(profile=None, raw=raw, start=m.start()))
        elif PROFILE_NAME_RE.match(value):
            result.voices.append(VoiceTag(profile=value, raw=raw, start=m.start()))
        else:
            result.warnings.append("voice: invalid profile name %r ignored" % value[:80])

    return result
