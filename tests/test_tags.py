import pytest

from afterword.hooks import tags
from afterword.hooks.tags import clean_text, parse, truncate
from conftest import TIME_SCALE


# --------------------------------------------------------------------------- #
# <say>                                                                       #
# --------------------------------------------------------------------------- #


def test_no_tag_is_silent():
    r = parse("Just a normal reply with no tags.")
    assert r.say is None
    assert r.voice is None
    assert r.says == [] and r.voices == []


@pytest.mark.parametrize("value", [None, 42, b"<say>x</say>", ["<say>x</say>"], {}, ""])
def test_non_string_or_empty_input_never_raises(value):
    r = parse(value)
    assert r.say is None and r.voice is None


def test_basic_say():
    r = parse("Done.\n\n<say>All tests pass.</say>")
    assert r.say.text == "All tests pass."
    assert r.say.attrs == {}


def test_say_attributes_all_quote_styles():
    r = parse("""<say mood="dry" lang='ja' pace=1.1>こんにちは</say>""")
    assert r.say.text == "こんにちは"
    assert r.say.attrs == {"mood": "dry", "lang": "ja", "pace": 1.1}


def test_say_case_insensitive_and_whitespace():
    r = parse("<SAY  Mood = \"calm\" >Hello</SAY >")
    assert r.say.text == "Hello"
    assert r.say.attrs == {"mood": "calm"}


def test_multiline_say_collapses_whitespace():
    r = parse("<say>\n  line one\n\n  line two  \n</say>")
    assert r.say.text == "line one line two"


def test_last_nonempty_say_wins():
    r = parse("<say>first</say> middle <say>second</say> <say>  </say>")
    assert r.say.text == "second"
    assert [t.text for t in r.says] == ["first", "second"]  # empty ones are not kept
    assert any("empty" in w for w in r.warnings)


def test_empty_say_is_ignored():
    assert parse("<say></say>").say is None
    assert parse("<say>   </say>").say is None
    assert parse("<say><b></b></say>").say is None


def test_self_closing_say_is_not_a_say():
    assert parse("<say/>").say is None
    assert parse("<say pace='1'/> text </say>").say is None


def test_unclosed_say_ignored_with_warning():
    r = parse("<say>never closed")
    assert r.say is None
    assert any("unclosed" in w for w in r.warnings)


def test_similar_tag_names_do_not_match():
    assert parse("<sayings>no</sayings>").say is None
    assert parse("<saying>no</saying>").say is None
    assert parse("<voices set='nova'/>").voice is None
    assert parse("<voice-over set='nova'/>").voice is None


def test_invalid_pace_dropped_and_out_of_range_clamped():
    r = parse("<say pace='fast'>a</say>")
    assert "pace" not in r.say.attrs
    assert any("invalid pace" in w for w in r.warnings)
    assert parse("<say pace='9'>a</say>").say.attrs["pace"] == tags.PACE_MAX
    assert parse("<say pace='0.1'>a</say>").say.attrs["pace"] == tags.PACE_MIN
    for bad in ("nan", "inf", "-inf"):
        assert "pace" not in parse("<say pace='%s'>a</say>" % bad).say.attrs


def test_lang_validation_and_normalisation():
    assert parse("<say lang='EN_us'>a</say>").say.attrs["lang"] == "en-us"
    assert parse("<say lang='zh-Hans'>a</say>").say.attrs["lang"] == "zh-hans"
    r = parse("<say lang='english please'>a</say>")
    assert "lang" not in r.say.attrs


def test_unknown_attribute_dropped():
    r = parse("<say volume='11'>a</say>")
    assert r.say.attrs == {}
    assert any("unknown attribute" in w for w in r.warnings)


def test_mood_capped():
    r = parse("<say mood='%s'>a</say>" % ("x" * 500))
    assert len(r.say.attrs["mood"]) == tags.MOOD_MAX


def test_entities_and_inner_markup_cleaned():
    r = parse("<say>Fish &amp; chips are <b>great</b> &lt;3</say>")
    assert r.say.text == "Fish & chips are great <3"


def test_comparison_operators_survive():
    assert parse("<say>a < b and c > d</say>").say.text == "a < b and c > d"


def test_markdown_stripped_from_say():
    r = parse("<say>**Bold**, *italic*, `code`, and [a link](http://x.y).</say>")
    assert r.say.text == "Bold, italic, code, and a link."


def test_nested_say_does_not_crash():
    r = parse("<say><say>inner</say></say>")
    assert r.say.text == "inner"
    assert [t.text for t in r.says] == ["inner"]  # spoken once, not "inner inner"


def test_nested_say_keeps_only_the_outer_block():
    r = parse("Hi. <say>Outer start <say>inner bit</say> outer end.</say> <say>two</say>")
    assert [t.text for t in r.says] == ["Outer start inner bit outer end.", "two"]
    assert any("nested" in w for w in r.warnings)
    r = parse("<say>Hello there <say>inner bit</say> and more</say>")
    assert r.say.text == "Hello there inner bit and more" and len(r.says) == 1
    # A bare <say> mentioned in prose stays unpaired: the real block counts.
    assert [t.text for t in parse("Type <say> to start. <say>real</say>").says] == ["real"]


def test_to_dict_previews_long_blocks():
    from afterword.hooks import tags

    long = "word " * 1500  # under MAX_SAY_CHARS, so nothing is cut
    d = parse("<say>%s</say><say>%s</say>" % (long, long)).to_dict()
    assert all(len(s["text"]) == tags.SEGMENT_PREVIEW for s in d["segments"])
    assert d["segments"][0]["chars"] == len(long.strip())


# --------------------------------------------------------------------------- #
# Code regions are ignored                                                     #
# --------------------------------------------------------------------------- #


def test_say_in_fenced_block_ignored():
    msg = "Example:\n```xml\n<say>not me</say>\n```\nDone."
    assert parse(msg).say is None


def test_say_in_tilde_fence_ignored():
    assert parse("~~~\n<say>nope</say>\n~~~\n").say is None


def test_unclosed_fence_masks_rest():
    assert parse("```\n<say>nope</say>\n").say is None


def test_say_after_fence_is_used():
    msg = "```\n<say>no</say>\n```\n\n<say>yes</say>"
    assert parse(msg).say.text == "yes"


def test_longer_fence_needs_matching_close():
    msg = "````\n```\n<say>no</say>\n```\n````\n<say>yes</say>"
    assert parse(msg).say.text == "yes"


def test_say_in_inline_code_ignored():
    assert parse("Use `<say>text</say>` to speak.").say is None
    assert parse("Use ``<say>a ` b</say>`` here.").say is None


def test_inline_code_inside_say_is_spoken():
    r = parse("<say>Run `make test` now.</say>")
    assert r.say.text == "Run make test now."


def test_unmatched_backtick_is_literal():
    r = parse("It's a ` stray backtick. <say>still works</say>")
    assert r.say.text == "still works"


def test_say_in_html_comment_ignored():
    assert parse("<!-- <say>hidden</say> -->").say is None
    assert parse("<!-- unclosed <say>hidden</say>").say is None


def test_voice_in_code_ignored():
    assert parse("Type `<voice set=\"nova\"/>` to enable.").voice is None
    assert parse("```\n<voice set='nova'/>\n```").voice is None


# --------------------------------------------------------------------------- #
# <voice>                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    ['<voice set="nova"/>', "<voice set='nova' />", "<voice set=nova>", "<VOICE SET=\"nova\"/>"],
)
def test_voice_set_forms(raw):
    assert parse(raw).voice.profile == "nova"


@pytest.mark.parametrize("raw", ['<voice set="off"/>', '<voice set="OFF"/>'])
def test_voice_off(raw):
    v = parse(raw).voice
    assert v is not None and v.profile is None


def test_last_voice_wins():
    assert parse('<voice set="a"/> <voice set="b"/>').voice.profile == "b"
    assert parse('<voice set="a"/> <voice set="off"/>').voice.profile is None


def test_invalid_voice_ignored():
    r = parse('<voice set="../etc/passwd"/> <voice/> <voice set=""/>')
    assert r.voice is None
    assert len(r.warnings) >= 2


def test_voice_and_say_together():
    r = parse('<voice set="nova"/>\nHi.\n<say lang="en">Voice on.</say>')
    assert r.voice.profile == "nova"
    assert r.say.text == "Voice on."


def test_to_dict_is_json_ready():
    import json

    d = parse('<voice set="nova"/><say pace="1.2">x</say>').to_dict()
    assert json.loads(json.dumps(d)) == {
        "say": {"text": "x", "attrs": {"pace": 1.2}},
        "segments": [{"text": "x", "chars": 1, "attrs": {"pace": 1.2}}],
        "voice": {"set": "nova"},
        "say_count": 1,
        "voice_count": 1,
        "warnings": [],
    }


# --------------------------------------------------------------------------- #
# clean_text / truncate                                                       #
# --------------------------------------------------------------------------- #


def test_clean_text_strips_control_chars():
    assert clean_text("a\x07b\x1bc\x00d") == "abcd"


def test_clean_text_markdown_line_prefixes():
    assert clean_text("# Title\n- one\n> quoted") == "Title one quoted"


def test_truncate_noop_when_short():
    assert truncate("short", 400) == "short"


def test_truncate_prefers_sentence_end():
    text = "First sentence here. Second sentence is much longer and runs on."
    assert truncate(text, 40) == "First sentence here."


def test_truncate_falls_back_to_word_boundary():
    text = "word " * 50
    out = truncate(text.strip(), 42)
    assert len(out) <= 42 and not out.endswith(" ") and out.endswith("word")


def test_truncate_cjk_sentence_end():
    text = "これは文です。" + "あ" * 100
    assert truncate(text, 12) == "これは文です。"


def test_truncate_hard_cut_without_boundaries():
    assert truncate("x" * 100, 10) == "x" * 10


def test_pathological_input_is_fast():
    import time

    msg = ("<say " * 20000) + ("`" * 5000) + ("<voice " * 20000)
    t = time.time()
    parse(msg)
    assert time.time() - t < 2.0 * TIME_SCALE


@pytest.mark.parametrize(
    "msg",
    [
        "<say " * 50000,
        "<say>" * 50000,
        "<voice " * 50000,
        "` `` " * 20000,
        "<!--" * 50000,
        "<say><a " + "x" * 200000 + "</say>",
        "<say>" + "*a " * 50000 + "</say>",
    ],
)
def test_adversarial_inputs_are_linear(msg):
    import time

    t = time.time()
    parse(msg)
    assert time.time() - t < 1.0 * TIME_SCALE


def test_say_with_selfclosing_then_real():
    assert parse("<say/> then <say>real</say>").say.text == "real"


# --------------------------------------------------------------------------- #
# Regressions from review                                                     #
# --------------------------------------------------------------------------- #


def test_comment_opener_inside_inline_code_does_not_mask_reply():
    msg = "Start an HTML comment with `<!--` in your template.\n\n<say>Fixed the template.</say>"
    assert parse(msg).say.text == "Fixed the template."


def test_backtick_inside_comment_is_comment():
    assert parse("<!-- a ` b --> <say>ok</say> `x`").say.text == "ok"


def test_unclosed_comment_ends_with_its_paragraph():
    assert parse("Prose with a stray <!-- here.\n\n<say>still spoken</say>").say.text == "still spoken"


def test_comment_may_span_paragraphs_when_closed():
    assert parse("<!-- a\n\n<say>hidden</say>\n\n-->").say is None


def test_bare_say_in_prose_does_not_swallow_reply():
    msg = "I added support for the <say> tag in the parser. Tests pass.\n\n<say>Parser done.</say>"
    r = parse(msg)
    assert r.say.text == "Parser done."
    assert any("unclosed" in w for w in r.warnings)


def test_stray_closer_is_ignored():
    assert parse("text </say> more <say>real</say>").say.text == "real"


def test_unmatched_backtick_does_not_cross_paragraphs():
    msg = "Press the ` key to open the console.\n\n<say>Done</say>\n\nSee `README`."
    assert parse(msg).say.text == "Done"


def test_blank_line_flood_is_linear():
    import time

    # Within MAX_MESSAGE_CHARS; the block is cut to MAX_SAY_CHARS, so the
    # word goes first.
    for body in ("\n" * 60000, " \n" * 30000):
        t = time.time()
        assert parse("<say>x" + body + "</say>").say.text == "x"
        assert time.time() - t < 1.0 * TIME_SCALE


def test_unpaired_emphasis_markers_are_kept():
    assert clean_text("That's 2**10, so 1024 entries.") == "That's 2**10, so 1024 entries."
    assert clean_text("a ** b") == "a ** b"
    assert clean_text("**bold** and __under__ and ~~gone~~") == "bold and under and gone"


def test_prose_angle_brackets_keep_their_words():
    assert clean_text("x<y and z>w now") == "x<y and z>w now"
    assert clean_text("Use <T> generics") == "Use <T> generics"
    assert clean_text("a <b>bold</b> <em class='x'>move</em>") == "a bold move"


def test_truncate_never_cuts_at_a_decimal_point():
    assert truncate("Pi is about 3.14159 and a bit more than that", 14) == "Pi is about"
    assert truncate("Version 2.0 shipped. Then more words follow here", 22) == "Version 2.0 shipped."


def test_say_embedded_commands_are_stripped():
    r = parse("<say>Done. [[slnc 3600000]] [[inpt PHON]] ok [[ rate 50 ]]</say>")
    assert r.say.text == "Done. ok"
    assert "[[" not in clean_text("a [[ b") and "]]" not in clean_text("a ]] b")


# --------------------------------------------------------------------------- #
# Size caps: a crafted reply cannot burn the hook's deadline                  #
# --------------------------------------------------------------------------- #

PATHOLOGICAL_BLOCKS = {
    "emphasis": "**a " * 3000,
    "underline": "__a~~b " * 2000,
    "link text": "[" * 12000,
    "link target": "[a](" * 3000,
    "mixed": "**[*`<b " * 1500,
    "single stars": "*a " * 4000,
    "tags": "<b " * 4000,
    "entities": "&#x" * 4000,
}


@pytest.mark.parametrize("name", sorted(PATHOLOGICAL_BLOCKS))
def test_pathological_blocks_parse_fast(name):
    import time

    from conftest import TIME_SCALE

    body = PATHOLOGICAL_BLOCKS[name]
    # As many full blocks as fit in the message cap, then a 16 MB tail.
    block = "<say>%s</say>\n\n" % body
    msg = block * (tags.MAX_MESSAGE_CHARS // len(block)) + body * (16 * 1024 * 1024 // len(body))
    t = time.perf_counter()
    r = parse(msg)
    elapsed = time.perf_counter() - t
    assert elapsed < 0.2 * TIME_SCALE, "%s: %.0f ms" % (name, elapsed * 1000)
    assert 1 <= len(r.says) <= tags.MAX_SAYS


def test_deeply_nested_says_parse_fast():
    import time

    from conftest import TIME_SCALE

    for msg in ("<say>" * 6000 + "x" + "</say>" * 6000,
                ("<say>" + "y" * 50) * 1000 + "</say>" * 1000,
                "<say>a</say>" * 5000):
        t = time.perf_counter()
        r = parse(msg)
        assert time.perf_counter() - t < 0.2 * TIME_SCALE
        assert 1 <= len(r.says) <= tags.MAX_SAYS


def test_nesting_is_resolved_before_cleaning():
    r = parse("<say>outer <say>inner</say> end</say><say>two</say>")
    assert [t.text for t in r.says] == ["outer inner end", "two"]
    assert "say: 1 nested tag(s) ignored" in r.warnings
    # Extra blocks are counted, not cleaned.
    r = parse("<say>a</say>" * 7)
    assert len(r.says) == tags.MAX_SAYS
    assert "say: 3 extra tag(s) dropped (at most 4 per reply)" in r.warnings
    # An empty outer block does not use up a place.
    r = parse("<say> </say>" + "<say>b</say>" * 4)
    assert [t.text for t in r.says] == ["b"] * 4


def test_message_is_cut_at_the_cap():
    head = "<say>first</say>"
    filler = "x" * (tags.MAX_MESSAGE_CHARS - len(head) - 5)
    r = parse(head + filler + "<say>past the cap</say>")
    assert [t.text for t in r.says] == ["first"]
    assert any("rest is ignored" in w for w in r.warnings)
    # A block straddling the cap is unclosed (early speaking keeps waiting).
    assert r.open_says == [len(head) + len(filler)]
    # Positions are those of the original message.
    assert r.says[0].start == 0 and r.says[0].raw == head


def test_say_block_is_cut_before_cleaning():
    r = parse("<say>%s</say>" % ("word " * 4000))
    assert len(r.says[0].text) <= tags.MAX_SAY_CHARS
    assert any("cut" in w for w in r.warnings)


def test_hook_with_a_huge_crafted_reply_stays_fast(aw_home, say_stub):
    from conftest import TIME_SCALE, hook_log, run_hook, stop_payload, wait_for

    msg = '<voice set="nova"/><say>Still heard.</say>' + ("<say>" + "**[a](" * 2000 + "</say>") * 3
    msg += ("<say>" * 50 + "**a [" * 1000) * 40
    proc, _ = run_hook(stop_payload(msg))
    assert proc.returncode == 0 and proc.stderr == b""
    row = hook_log(aw_home)[-1]
    assert row["decision"] == "speak" and row["elapsed_ms"] < 500 * TIME_SCALE
    assert wait_for(lambda: say_stub.exists() and "Still heard." in say_stub.read_text())


def test_emphasis_and_links_are_bounded():
    assert clean_text("**%s**" % ("a" * 300)) == "a" * 300
    assert clean_text("**%s**" % ("a" * 301)) == "**%s**" % ("a" * 301)  # too long to be emphasis
    assert clean_text("[%s](u)" % ("t" * 300)) == "t" * 300
    assert clean_text("see [docs](%s)" % ("u" * 1000)) == "see docs"
