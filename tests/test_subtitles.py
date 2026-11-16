"""The subtitles as the first check on what Whisper heard: evidence, the
decision, and swears only the subtitles have."""

from __future__ import annotations

import pytest

from censarr import subtitles, words

SRT = """1
00:00:00,500 --> 00:00:01,800
<i>What the fuck</i> is that?

2
00:00:02,900 --> 00:00:04,200
Get some caulk
on that seam.

3
00:00:10,000 --> 00:00:11,000
Oh, sh*t.

broken block without times

4
00:00:20,000 --> 00:00:21,000
{\\an8}Aw, [BLEEP] it.
"""


def m(text, start, end=None, confidence=None):
    return words.Match(start, end if end is not None else start + 0.3, text, "strong",
                       confidence=confidence)


def test_parse_srt():
    cues = subtitles.parse_srt(SRT)
    assert [(c.start, c.end) for c in cues] == [(0.5, 1.8), (2.9, 4.2), (10.0, 11.0), (20.0, 21.0)]
    assert cues[0].text == "What the fuck is that?"          # markup gone
    assert cues[1].text == "Get some caulk on that seam."     # lines joined
    assert cues[3].text == "Aw, [BLEEP] it."
    assert subtitles.parse_srt("") == []
    assert subtitles.parse_srt("1\n00:00:01.5 --> 00:00:02.25\nhi")[0].start == 1.5


@pytest.mark.parametrize("match, state", [
    (m("fuck,", 1.0), "agrees"),               # the same word
    (m("cock", 3.2), "soundalike"),            # the homophone case: caulk
    (m("shit", 10.2), "agrees"),               # censored in the subtitle
    (m("damn", 20.3), "agrees"),               # [BLEEP]
    (m("shit", 40.0), ""),                     # no line anywhere near
    (m("fucking", 4.6), "differs"),            # within the window of line 2
])
def test_evidence(match, state):
    cues = subtitles.parse_srt(SRT)
    got, text = subtitles.evidence(match, cues, words.Matcher())
    assert got == state
    if state:
        assert text


def test_any_listed_word_in_the_line_counts_as_agreeing():
    cues = [subtitles.Cue(0, 2, "This is bullshit.")]
    assert subtitles.evidence(m("shit", 0.5), cues, words.Matcher())[0] == "agrees"


def test_annotate_records_evidence_and_keeps_every_match():
    cues = subtitles.parse_srt(SRT)
    got, note = subtitles.annotate([m("fuck", 1.0), m("cock", 3.2)], cues)
    assert note == ""
    assert [(g.text, g.subtitle_state) for g in got] == [("fuck", "agrees"), ("cock", "soundalike")]
    assert "caulk" in got[1].subtitle


def test_subtitles_from_another_release_are_set_aside():
    cues = [subtitles.Cue(t, t + 1, "Nothing rude at all.") for t in range(0, 60, 2)]
    matches = [m("shit", t + 0.2) for t in range(0, 12, 2)]
    got, note = subtitles.annotate(matches, cues)
    assert "another release" in note
    assert all(g.subtitle_state == "" for g in got)
    assert got == matches


def test_no_subtitles_changes_nothing():
    matches = [m("shit", 1.0)]
    assert subtitles.annotate(matches, []) == (matches, "")


def test_worth_checking():
    # Could be wrong: left in on a sound-alike, muted on the subtitles alone,
    # or unsure with nothing to check it.
    assert subtitles.worth_checking(words.Match(0, 1, "x", "c", subtitle_state="soundalike"))
    assert subtitles.worth_checking(words.Match(0, 1, "x", "c", subtitle_state="subtitle_only"))
    assert subtitles.worth_checking(m("x", 0, confidence=0.3))
    assert subtitles.worth_checking({"subtitle_state": "", "confidence": 0.2})
    # Settled: the subtitles have it, soften it, or leave it out.
    for state in ("agrees", "omits", "replaced", "differs"):
        assert not subtitles.worth_checking(
            words.Match(0, 1, "x", "c", confidence=0.3, subtitle_state=state))
    assert not subtitles.worth_checking(m("x", 0, confidence=0.9))
    assert not subtitles.worth_checking(m("x", 0))


def test_confidence_is_carried_from_the_transcript():
    got = words.Matcher().find([{"word": "shit", "start": 0, "end": 0.3, "probability": 0.42}])
    assert got[0].confidence == 0.42
    got = words.Matcher().find([{"word": "oh", "start": 0, "end": 0.1},
                                {"word": "my", "start": 0.1, "end": 0.2},
                                {"word": "god", "start": 0.2, "end": 0.4, "probability": 0.8}])
    assert got[0].confidence == 0.8
    assert words.Matcher().find([{"word": "shit", "start": 0, "end": 1}])[0].confidence is None


def test_webvtt_times_without_hours():
    vtt = "WEBVTT\n\n01:02.500 --> 01:04.000 align:start\nGet some caulk.\n"
    assert [(c.start, c.end, c.text) for c in subtitles.parse_srt(vtt)] == [(62.5, 64.0, "Get some caulk.")]


# Heard, as Whisper wrote it: "caulk" came out as a swear.
HEARD = [{"word": " what", "start": 0.6, "end": 0.8}, {"word": " the", "start": 0.8, "end": 0.9},
         {"word": " fuck", "start": 1.0, "end": 1.3}, {"word": " is", "start": 1.35, "end": 1.45},
         {"word": " that", "start": 1.5, "end": 1.7},
         {"word": " get", "start": 3.0, "end": 3.2}, {"word": " some", "start": 3.25, "end": 3.45},
         {"word": " cock", "start": 3.5, "end": 3.8, "probability": 0.97},
         {"word": " on", "start": 3.85, "end": 3.95}, {"word": " that", "start": 4.0, "end": 4.2},
         {"word": " oh", "start": 10.1, "end": 10.2}, {"word": " shit", "start": 10.3, "end": 10.6},
         {"word": " aw", "start": 20.1, "end": 20.2}, {"word": " damn", "start": 20.3, "end": 20.6},
         {"word": " it", "start": 20.65, "end": 20.8}]


def decided(texts):
    cues = subtitles.parse_srt(SRT)
    found = words.Matcher().find(HEARD)
    marked, note = subtitles.annotate([x for x in found if x.text.strip(" ,.") in texts],
                                      cues, HEARD)
    assert note == ""
    return subtitles.decide(marked)


def test_a_word_the_subtitles_have_is_muted():
    muted, alike, still_open = decided({"fuck"})
    assert [x.text for x in muted] == ["fuck"] and "subtitles have it" in muted[0].reason


def test_a_sound_alike_is_left_in_however_sure_whisper_was():
    muted, alike, _ = decided({"cock"})
    assert muted == [] and [x.text for x in alike] == ["cock"]
    assert alike[0].confidence == 0.97 and "caulk" in alike[0].reason


def test_hidden_and_bleeped_words_count_as_the_word():
    muted, alike, _ = decided({"shit", "damn"})
    assert sorted(x.text for x in muted) == ["damn", "shit"] and alike == []


def test_no_line_nearby_leaves_the_word_open():
    heard = [{"word": " shit", "start": 40.0, "end": 40.3}]
    marked, _ = subtitles.annotate(words.Matcher().find(heard), subtitles.parse_srt(SRT), heard)
    muted, alike, still_open = subtitles.decide(marked)
    assert (muted, alike) == ([], []) and [x.text for x in still_open] == ["shit"]


# ---------------------------------------------------------------- lining up another copy

LINES = ["Nobody leaves this house tonight", "Where did you put the money",
         "Somebody called the police already", "Get the truck around back now",
         "Those windows were locked yesterday", "Listen carefully before answering",
         "Nothing about this makes sense", "Grandma never trusted strangers",
         "Pack everything into the basement", "Morning comes faster than planned"]


def heard_and_cues(shift: float):
    """Words heard at their true times, and lines shown `shift` seconds late."""
    heard, cues = [], []
    for n in range(60):
        text = LINES[n % len(LINES)] + f" number{n}"
        start = 10.0 + n * 7.0
        for k, word in enumerate(text.split()):
            heard.append({"word": " " + word, "start": start + k * 0.4, "end": start + k * 0.4 + 0.3})
        cues.append(subtitles.Cue(start + shift - 0.2, start + shift + 3.0, text))
    return heard, cues


def test_subtitles_from_another_cut_are_moved_into_line():
    heard, cues = heard_and_cues(-8.5)
    moved, low, high = subtitles.align(cues, heard)
    assert abs(low + 8.5) <= 0.5 and abs(high + 8.5) <= 0.5
    assert abs(moved[0].start - cues[0].start + low) < 1e-9
    assert subtitles.align(moved, heard)[1:] == (0.0, 0.0)   # and they stay put


def test_subtitles_that_drift_are_moved_window_by_window():
    """Adverts cut differently: 2 s out at first, 12 s out after a break."""
    heard, cues = heard_and_cues(0.0)
    late = [subtitles.Cue(c.start + (2.0 if c.start < 220 else 12.0),
                          c.end + (2.0 if c.start < 220 else 12.0), c.text) for c in cues]
    moved, low, high = subtitles.align(late, heard)
    assert abs(low - 2.0) <= 0.5 and abs(high - 12.0) <= 0.5
    # Every line back within a fraction of its time on screen, the ones beside
    # the break included.
    assert max(abs(m.start - c.start) for m, c in zip(moved, cues)) <= 0.75


@pytest.mark.parametrize("speed", [23.976 / 25, 25 / 23.976])
def test_subtitles_timed_for_another_frame_rate_are_lined_up(speed):
    """A PAL release against a 23.976 fps file: 4% out, growing all the way."""
    heard, cues = heard_and_cues(0.0)
    stretched = [subtitles.Cue(c.start * speed + 3.0, c.end * speed + 3.0, c.text) for c in cues]
    moved, _low, _high = subtitles.align(stretched, heard)
    assert max(abs(m.start - c.start) for m, c in zip(moved, cues)) <= 0.75


def test_subtitles_that_drift_at_every_break_are_not_given_up_on():
    """Three different offsets: no single shift fits the whole file."""
    heard, cues = heard_and_cues(0.0)
    third = cues[-1].end / 3
    late = [subtitles.Cue(c.start + d, c.end + d, c.text) for c in cues
            for d in [2.0 if c.start < third else 6.0 if c.start < 2 * third else 11.0]]
    moved, low, high = subtitles.align(late, heard)
    assert max(abs(m.start - c.start) for m, c in zip(moved, cues)) <= 0.75


def test_subtitles_already_in_line_are_left_alone():
    heard, cues = heard_and_cues(0.0)
    moved, low, high = subtitles.align(cues, heard)
    assert (low, high) == (0.0, 0.0) and moved == cues


def test_subtitles_of_something_else_are_not_forced_into_line():
    heard, cues = heard_and_cues(0.0)
    other = [subtitles.Cue(c.start, c.end, "Completely unrelated dialogue here") for c in cues]
    assert subtitles.align(other, heard) == (other, 0.0, 0.0)
    assert subtitles.align([], heard) == ([], 0.0, 0.0)


def test_subtitles_that_do_not_follow_the_dialogue_are_rejected():
    heard, cues = heard_and_cues(0.0)
    assert subtitles.fits(cues, heard)
    other = [subtitles.Cue(c.start, c.end, "Completely unrelated dialogue here") for c in cues]
    assert not subtitles.fits(other, heard)
    # Too little heard to tell: no verdict, so the old per-detection check decides.
    assert subtitles.fits(other, heard[:30])
    assert subtitles.fits([], heard)



# ---------------------------------------------------------------- reading the script

def said(text: str, start: float = 10.0, gap: float = 0.35) -> list[dict]:
    return [{"word": " " + w, "start": start + n * gap, "end": start + n * gap + 0.3}
            for n, w in enumerate(text.split())]


def line_at(heard, text, start, end):
    return [subtitles.Cue(start, end, text)]


def detection(heard, word):
    w = next(h for h in heard if words.normalize(h["word"]) == word)
    return words.Match(start=w["start"], end=w["end"], text=w["word"].strip(),
                       category="strong", confidence=0.4)


SCRIPT_CASES = [
    # heard, subtitle line, detected word, expected
    ("you have to open a shit ton of bank accounts", "and open a ton of bank accounts", "shit", "omits"),
    ("it was no way in hell this is random", "No way this is random.", "hell", "omits"),
    ("pass me the cock gun right now please", "Pass me the caulk gun right now, please.", "cock", "soundalike"),
    ("he got out oh shit here he comes", "He got out. Whoa! Here he comes.", "shit", "replaced"),
    ("let me oh shit oh no it is fine", "Let me get that. Oh! No, it's fine.", "shit", "replaced"),
    ("what the fucking hell is that", "What the fuckin' hell is that?", "fucking", "agrees"),
    ("stop being such a fucking idiot", "Stop being such a freaking idiot.", "fucking", "replaced"),
    ("an accident on the road to hell i tried", "An accident on the Roosevelt. I tried", "hell", "soundalike"),
    ("kill this motherfucker right now", "Kill this mother right now.", "motherfucker", "replaced"),
    ("we are here kill this motherfucker right away", "-We are here! -Kill this mother--", "motherfucker", "agrees"),
    ("you know on your period or some shit but anyway", "You know, on your period or...", "shit", "omits"),
    ("look at that holy shit it is huge", "Look at that. Holy shit, it is huge!", "shit", "agrees"),
]


@pytest.mark.parametrize("heard_text, line, word, want", SCRIPT_CASES)
def test_the_script_around_a_word_decides(heard_text, line, word, want):
    heard = said(heard_text)
    # The line is on screen well before the word, beyond the quick check.
    cues = line_at(heard, line, 8.0, 9.0) if want != "agrees" else line_at(heard, line, 9.5, 14.0)
    m = detection(heard, word)
    state, _shown = subtitles.evidence(m, cues, words.Matcher(context_words=frozenset()), heard)
    assert state == want


def test_a_line_that_leaves_the_word_out_is_no_reason_to_unmute():
    heard = said("you have to open a shit ton of bank accounts")
    m = detection(heard, "shit")
    marked, note = subtitles.annotate([m], line_at(heard, "and open a ton of bank accounts", 10.0, 14.0), heard)
    assert marked[0].subtitle_state == "omits" and note == ""
    muted, alike, still_open = subtitles.decide(marked)
    assert [x.text for x in muted] == ["shit"] and alike == []      # captions soften: muted



# ---------------------------------------------------------------- swears only the subtitles have

def placed(heard_text, line, start=10.0):
    heard = said(heard_text, start)
    cues = [subtitles.Cue(start - 0.2, start + len(heard_text.split()) * 0.35 + 0.5, line)]
    matcher = words.Matcher()
    return subtitles.from_subtitles(cues, heard, matcher.find(heard), matcher), heard


def test_a_swear_whisper_heard_as_something_else_is_muted_there():
    got, heard = placed("dont turn your back on me you posse right now", "Don't turn your back on me, you pussy, right now.")
    posse = next(h for h in heard if h["word"].strip() == "posse")
    assert [(g.text, g.subtitle_state) for g in got] == [("pussy,", "subtitle_only")]
    # From the end of the word before to the start of the word after.
    assert got[0].start <= posse["start"] and got[0].end >= posse["end"]
    assert got[0].end - got[0].start <= subtitles.PLACE_MAX
    assert "posse" in got[0].reason and got[0].needs_review


def test_a_swear_whisper_already_caught_is_not_added_again():
    got, _ = placed("oh shit oh shit oh shit", "Oh shit. Oh shit. Oh shit.")
    assert got == []


def test_numbers_are_not_hidden_swears():
    got, _ = placed("it was over ten thousand dollars of damage", "It was over $10,000 of damage, 95% of it.")
    assert got == []


def test_a_swear_with_nowhere_to_go_is_not_placed():
    """The words either side were said back to back: the captions added it."""
    got, _ = placed("i dont know right i mean", "I don't know shit, right? I mean...", start=10.0)
    assert got == []



# ---------------------------------------------------------------- when the script cannot be read

def on_screen(heard_text, line, word, start=10.0):
    heard = said(heard_text, start)
    cues = [subtitles.Cue(start - 0.1, start + len(heard_text.split()) * 0.35, line)]
    return subtitles.evidence(detection(heard, word), cues, words.Matcher(context_words=frozenset()), heard)


def test_a_chant_misheard_is_a_sound_alike():
    """Nothing to anchor on in a chant: the line on screen at that moment."""
    state, _ = on_screen("shit shit shit shit", "-City! -City! -City! -City!", "shit")
    assert state == "soundalike"


def test_a_word_that_was_also_said_does_not_excuse_the_swear():
    """The captions left "Oh my god" out; "get" before it was said as well."""
    state, _ = on_screen("this is what you get oh my god", "This is what you get.", "god")
    assert state != "soundalike"


def test_british_spellings_are_the_same_swear():
    matcher = words.Matcher(context_words=frozenset())
    assert subtitles._is_word("arse", "ass", matcher)
    assert subtitles._is_word("arsehole", "asshole", matcher)


# ---------------------------------------------------------------- stress-test findings

@pytest.mark.parametrize("raw, word", [
    ("sh-", "shit"), ("f--", "fuck"), ("Mother—", "motherfucker"), ("b...", "bitch"),
    ("Christ's", "christ"), ("fucks", "fuck"),
])
def test_a_word_cut_short_or_possessive_is_the_word(raw, word):
    assert subtitles._is_word(raw, word, words.Matcher(context_words=frozenset()))


def test_a_dash_inside_a_word_is_not_a_cut():
    assert not subtitles._is_word("mother-in-law", "motherfucker",
                                  words.Matcher(context_words=frozenset()))


@pytest.mark.parametrize("heard_text, line, word", [
    ("oh shit they found us", "Oh, sh-, they found us!", "shit"),
    ("oh christ not again", "Oh, Christ's sake, not again.", "christ"),
])
def test_a_cut_short_or_possessive_caption_mutes(heard_text, line, word):
    heard = said(heard_text)
    state, _ = subtitles.evidence(detection(heard, word), line_at(heard, line, 9.8, 12.5),
                                  words.Matcher(context_words=frozenset()), heard)
    assert state == "agrees"


@pytest.mark.parametrize("heard_text, line, word", [
    ("kill this motherfucker", "Kill this mother...", "motherfucker"),
    ("motherfucker i forgot the keys", "Mother, I forgot the keys.", "motherfucker"),
    ("that is total bullshit", "That is total bull-shoot.", "bullshit"),
    ("what a load of horseshit", "What a load of horse-crap.", "horseshit"),
    ("well goddamn", "Well, god dang!", "goddamn"),
    ("you are such a jackass", "You're such a jack-", "jackass"),
    ("i got a boner", "I got a bone.", "boner"),
])
def test_a_softening_on_one_side_of_the_line_is_never_a_sound_alike(heard_text, line, word):
    heard = said(heard_text)
    state, _ = subtitles.evidence(detection(heard, word), line_at(heard, line, 9.8, 12.5),
                                  words.Matcher(context_words=frozenset()), heard)
    assert state != "soundalike"


@pytest.mark.parametrize("heard, shown", [
    ("motherfucker", "mother-trucker"), ("motherfucker", "mother lover"),
    ("motherfucking", "mother-loving"), ("smartass", "smarty"), ("dumbass", "dummy"),
    ("dumbass", "dumbo"), ("asshole", "a-hole"), ("motherfucker", "mother"),
])
def test_a_compound_is_compared_on_its_swear_half(heard, shown):
    assert not subtitles._alike(heard, shown)


def test_a_compound_still_finds_a_mishearing_of_its_swear_half():
    assert subtitles._alike("cocksucker", "caulk sucker")
    assert subtitles._alike("dickhead", "dig head")


@pytest.mark.parametrize("heard_text, line", [
    ("you motherfucker", "You mother-trucker!"),
    ("what a smartass you are", "What a smarty you are."),
    ("do not be a dumbass", "Don't be a dummy."),
])
def test_a_tv_dub_of_a_compound_is_muted(heard_text, line):
    heard = said(heard_text)
    m = next(x for x in words.Matcher().find(heard))
    state, _ = subtitles.evidence(m, line_at(heard, line, 9.8, 12.5),
                                  words.Matcher(context_words=frozenset()), heard)
    assert state != "soundalike"


@pytest.mark.parametrize("heard_text, line, word", [
    ("you little fucker get back here", "You little sucker, get back here!", "fucker"),
    ("we are so fucked now", "We're so freaked now.", "fucked"),
    ("snakes on this motherfucking plane", "Snakes on this monkey-fighting plane!", "motherfucking"),
    ("you motherfucker", "You mother flipper!", "motherfucker"),
    ("what a dickhead", "What a dipstick.", "dickhead"),
])
def test_a_known_dub_is_muted(heard_text, line, word):
    heard = said(heard_text)
    state, _ = subtitles.evidence(detection(heard, word), line_at(heard, line, 9.8, 12.5),
                                  words.Matcher(context_words=frozenset()), heard)
    assert state != "soundalike"


@pytest.mark.parametrize("heard_text, line, word", [
    ("oh shit she is here", "Oh, she's here.", "shit"),
    ("because the at and get ass talk", "Because the get and at whoa talk.", "ass"),
])
def test_a_caption_word_whisper_heard_nearby_is_not_the_sound_alike(heard_text, line, word):
    """Contracted ("she's" for "she is") or moved, it was said as well."""
    heard = said(heard_text)
    state, _ = subtitles.evidence(detection(heard, word), line_at(heard, line, 9.8, 13.0),
                                  words.Matcher(context_words=frozenset()), heard)
    assert state != "soundalike"


def test_stems():
    assert {"she"} <= subtitles._stems("she's") and {"we"} <= subtitles._stems("we're")
    assert subtitles._stems("caulk") == {"caulk"}


@pytest.mark.parametrize("heard, shown", [
    ("tits", "it's"), ("damn", "am"), ("cunt", "can't"), ("hell", "here"), ("shit", "she"),
    ("dick", "did"), ("whore", "or"), ("ass", "as"), ("bitch", "been"),
])
def test_a_function_word_is_never_a_sound_alike(heard, shown):
    """They score 0.67-0.9 against the swear, but a caption with one in the
    swear's place has reworded the line."""
    assert not subtitles._alike(heard, shown)


def test_tits_out_lads():
    heard = said("tits out lads")
    state, _ = subtitles.evidence(detection(heard, "tits"), line_at(heard, "It's out, lads.", 9.8, 11.5),
                                  words.Matcher(context_words=frozenset()), heard)
    assert state != "soundalike"


def test_a_dropped_small_word_is_no_part_of_a_misheard_phrase():
    heard = said("what up with to hell can")
    cues = [subtitles.Cue(9.8, 10.9, "What up"), subtitles.Cue(10.95, 12.4, "- with TABLE can!")]
    state, _ = subtitles.evidence(detection(heard, "hell"), cues,
                                  words.Matcher(context_words=frozenset()), heard)
    assert state != "soundalike"


@pytest.mark.parametrize("heard_text, line, word", [
    ("it is fucking cold out here", "It's freezing out here.", "fucking"),
    ("and bastard last that", "And before last that the.", "bastard"),
])
def test_a_reworded_line_on_screen_is_no_evidence_of_a_mishearing(heard_text, line, word):
    heard = said(heard_text)
    state, _ = subtitles.evidence(detection(heard, word), line_at(heard, line, 9.8, 12.5),
                                  words.Matcher(context_words=frozenset()), heard)
    assert state != "soundalike"


def test_a_chant_on_screen_is_still_read():
    heard = said("shit shit shit")
    state, _ = subtitles.evidence(detection(heard, "shit"), line_at(heard, "City! City! City!", 9.9, 11.2),
                                  words.Matcher(context_words=frozenset()), heard)
    assert state == "soundalike"


@pytest.mark.parametrize("heard_text, line, misheard", [
    ("because about hail about back", "Because about hell about back.", "hail"),
    ("get dan get party because back", "Get damn get party because back.", "dan"),
    ("to can dig can talk", "To can dick can talk.", "dig"),
])
def test_a_missed_swear_between_repeated_words_is_still_placed(heard_text, line, misheard):
    got, heard = placed(heard_text, line)
    w = next(h for h in heard if h["word"].strip() == misheard)
    assert len(got) == 1 and got[0].start <= w["start"] and got[0].end >= w["end"]


@pytest.mark.parametrize("name", ["Ke$ha", "A$AP", "C#", "F#", "E*Trade", "Q*bert", "M*A*S*H",
                                  "B*Witched"])
def test_a_name_written_with_a_symbol_is_not_a_hidden_swear(name):
    got, _ = placed("we were listening to something all night", f"We were listening to {name} all night.")
    assert got == []


@pytest.mark.parametrize("hidden", ["f***", "sh*t", "a**hole", "b*tch", "f***ing"])
def test_a_hidden_swear_still_is(hidden):
    assert subtitles._hides_a_swear(hidden)


@pytest.mark.parametrize("shown", ["pussycat", "puss", "pussy-"])
def test_a_caption_that_cuts_or_runs_on_the_swear_is_a_softening(shown):
    assert subtitles._softens(shown, "pussy", words.Matcher(context_words=frozenset()))


def test_a_homophone_is_not_cut_short():
    """"dam" sounds exactly like "damn": a mishearing, not a cut."""
    assert not subtitles._softens("dam", "damn", words.Matcher(context_words=frozenset()))


# -- the censored copy -------------------------------------------------------

def _match(text, start):
    return words.Match(start=start, end=start + 0.3, text=text, category="strong")


def test_censored_copy_masks_the_swears():
    cues = [subtitles.Cue(1.0, 2.5, "What the fuck is that, shit!"),
            subtitles.Cue(3.0, 4.0, "Pass the caulk.")]
    got = subtitles.censor(cues, [_match("fuck", 1.4)], [], words.Matcher())
    assert got[0].text == "What the **** is that, ****!"
    assert got[1].text == "Pass the caulk."
    assert (got[0].start, got[0].end) == (1.0, 2.5)


def test_a_word_left_in_on_purpose_stays_shown():
    """A reverent "God" the audio kept is not masked on screen either."""
    cues = [subtitles.Cue(5.0, 7.0, "Thank God you're safe.")]
    kept = [words.Match(start=5.6, end=5.9, text="God", category="blasphemy",
                        reason="left in: reverent, not an exclamation")]
    got = subtitles.censor(cues, [], kept, words.Matcher(context_words=frozenset({"god"})))
    assert got[0].text == "Thank God you're safe."


def test_a_word_muted_by_hand_is_masked_too():
    cues = [subtitles.Cue(1.0, 2.0, "You muppet.")]
    got = subtitles.censor(cues, [_match("muppet", 1.3)], [], words.Matcher())
    assert got[0].text == "You ******."


def test_censored_copy_writes_back_as_srt():
    cues = [subtitles.Cue(3661.25, 3662.5, "**** off")]
    text = subtitles.to_srt(cues)
    assert "01:01:01,250 --> 01:01:02,500" in text
    assert subtitles.parse_srt(text)[0].text == "**** off"
