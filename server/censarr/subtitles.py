"""The subtitles as the primary check on what Whisper heard.

Speech recognition writes homophones - "caulk" comes back as a swear - and
nothing in the transcript says so. The subtitles were written by a person who
knew what was said, so every detection is read against them, and so is every
swear the subtitles have that Whisper did not catch:

  the subtitles have the word (or f***, [bleep])        muted
  they run straight past it ("open a ton of")          muted - captions soften
  a word that does not sound like it ("Whoa!")         muted - captions soften
  a word that sounds like it ("caulk")                 left in, worth a listen
  no line near it, or no subtitles                     muted, as Whisper heard
  a swear only the subtitles have                      muted, worth a listen

"Sounds like" is sounds.py. A sound-alike can be sent to a second opinion -
Ollama, or another Whisper server listening to that moment again - before it
is left in.

Subtitles are often censored, trimmed, or timed for another release. They are
lined up with what was heard first (align), set aside when they do not follow
the dialogue at all (fits), and set aside when most detections disagree
(annotate).
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, replace
from functools import lru_cache

from . import sounds, words

# How far either side of a word a subtitle line may start or end and still
# count as the line it was said in. Subtitles lead and trail speech.
WINDOW = 0.75

# Below this, Whisper itself was unsure of the word.
LOW_CONFIDENCE = 0.5

# With this many detections checked, and more than this share disagreeing,
# the subtitles are taken not to match the file.
MISMATCH_MIN = 5
MISMATCH_SHARE = 0.6

AGREES = "agrees"
# A line near the word says something else, but it could not be read against
# the script to say what stands in its place. Muted.
DIFFERS = "differs"
# A different word in exactly that place that sounds like the swear - "caulk".
# Whisper misheard; left in, unless a second opinion says otherwise.
SOUNDALIKE = "soundalike"
# A different word in that place that does not sound like it - "Whoa!" for
# "Oh, shit!" - or a minced oath, or another swear. The captioner softened it.
REPLACED = "replaced"
# A swear in the subtitles where Whisper heard something else, or nothing.
SUBTITLE_ONLY = "subtitle_only"
# The subtitles run straight past where the word was said - "open a ton of"
# for "open a shit-ton of". Subtitles soften swears all the time, so this is
# no evidence either way: the word is treated as if no line were near.
OMITS = "omits"

# Hours are optional: WebVTT may write 01:02.500.
_TIME = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{1,3})")
_TAG = re.compile(r"<[^>]+>|\{[^}]*\}")
# f***, s**t, sh*t, [bleep], ***: a swear the subtitler hid.
_CENSORED = re.compile(r"\b\w+[*#@$%]{2,}\w*|\w\*+\w|\[\s*(bleep|beep|censored|expletive)[^\]]*\]"
                       r"|\*{3,}", re.I)


@dataclass
class Cue:
    start: float
    end: float
    text: str


def _seconds(match: re.Match) -> float:
    h, m, s, ms = match.groups()
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[Cue]:
    """SRT (or WebVTT) to cues. Markup is dropped; malformed blocks are skipped."""
    cues: list[Cue] = []
    for block in re.split(r"\r?\n\s*\r?\n", (text or "").strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            times = _TIME.findall(line)
            if "-->" in line and len(times) == 2:
                start, end = (_seconds(t) for t in _TIME.finditer(line))
                body = " ".join(_TAG.sub("", ln).strip() for ln in lines[i + 1:])
                if body:
                    cues.append(Cue(start, end, body))
                break
    return cues


_HIDDEN = re.compile(r"[*#@$%]+")
# A swear with letters hidden: f***, sh*t, a**hole. Letters and symbols only -
# "95%" and "$5,000" are not swears.
_HIDDEN_WORD = re.compile(r"[^\w*#@$%]*[a-z]+[*#@$%]+[a-z]*[^\w*#@$%]*", re.I)


def _plain(bare: str) -> str:
    """One spelling for comparing: "fuckin'" and "fucking", "goddamn'" alike."""
    bare = bare.replace("'", "").replace("’", "")
    if bare.startswith("arse"):                           # British: arse, arsehole
        bare = "ass" + bare[4:]
    return bare + "g" if bare.endswith("in") and len(bare) >= 5 else bare
_BLEEP = re.compile(r"\[\s*(bleep|beep|censored|expletive)[^\]]*\]|\*{3,}", re.I)
# A word broken off: letters, then a dash or dots, and nothing after.
_STUB = re.compile(r"[^\w]*([a-z]+)(?:-+|—|–|…|\.\.\.)[^\w]*", re.I)


def _is_word(raw: str, word: str, matcher: words.Matcher) -> bool:
    """Whether one subtitle token is this word: itself, a spelling of it
    ("fuckin'", "bullshit" for "shit"), or a hidden form of it ("f***",
    "sh*t"). Another swear is not: in "gonna caulk that shit" the "shit"
    says nothing about the word before it."""
    if _HIDDEN_WORD.fullmatch(raw):
        letters = _HIDDEN.split(raw.lower())
        pattern = ".+".join(re.escape(re.sub(r"[^a-z']", "", p)) for p in letters)
        return bool(pattern.strip(".+")) and re.fullmatch(pattern, word) is not None
    # Cut short with a dash or dots: "sh-", "f--", "mother—", "b...".
    stub = _STUB.fullmatch(raw)
    if stub:
        return word.startswith(stub.group(1).lower())
    bare, word = _plain(words.normalize(raw)), _plain(word)
    if not bare:
        return False
    # The word itself, possessive or plural: "Christ's", "fucks".
    if bare in (word, word + "s", word + "es"):
        return True
    if len(bare) >= 3 and len(word) >= 3 and (word in bare or bare in word):
        return bool(matcher.find([{"word": bare, "start": 0.0, "end": 0.0}]))
    return False


def _says_it(text: str, word: str, matcher: words.Matcher) -> bool:
    """Whether a subtitle line carries this word - see _is_word - or a bleep
    that could be any word."""
    if _BLEEP.search(text):
        return True
    # Whole tokens first, so a dash that cuts a word short is still seen.
    return any(_is_word(raw, word, matcher) for raw in text.split()) or \
        any(_is_word(raw, word, matcher) for raw in _SPLIT.split(text) if raw)


# Lining up borrowed subtitles - another copy's, or ones the media server
# fetched. Two kinds of difference, both measured:
#   * a steady shift: a WEB-DL and a WEBRip of Trap House (2025) differ by a
#     studio logo, so every line is 8.5 s out. 3% of the transcript's words
#     matched as they were, 79% shifted, 2% for the wrong film at any shift.
#   * drift: an HDTV recording of Line of Fire S01E01 against subtitles made
#     for a release with the adverts cut differently - 2 s out at the start,
#     13 s by the end, stepping at each break. One shift for the file put 32%
#     of words in the right line; a shift per three-minute window put 91%.
# So a shift is found for the whole file first, then refined window by window.
ALIGN_LIMIT = 180.0        # seconds either way, for the whole file
ALIGN_MIN_SHARE = 0.4      # of sampled words found in the line at the new time
ALIGN_MIN_GAIN = 0.15      # over leaving them where they are
# ...or this little, once the result fits well: a file's own track that is a
# touch loose. The Bear S01E02's put lines up to 0.8 s off the speech; lining
# it up took words in the right line from 81% to 86% and fixed a verdict.
ALIGN_GOOD_FIT = 0.6
ALIGN_SMALL_GAIN = 0.03
ALIGN_SPAN = 180.0         # seconds of speech per window
ALIGN_STEP = 60.0          # between window centres
ALIGN_REACH = 20.0         # how far a window may move from the whole-file shift
ALIGN_MIN_WORDS = 25       # fewer than this in a window: use the whole-file shift
# Subtitles timed for another frame rate run fast or slow throughout: a PAL
# release (25 fps) against a 23.976 fps file is 4% out, three and a half
# minutes over a film. No shift can fix that, so each common ratio is tried.
# In an audit of 8 episodes, every such case was set aside until this was
# added. 1.0 is kept unless another ratio does clearly better.
ALIGN_SPEEDS = (1.0, 23.976 / 25, 25 / 23.976, 24 / 25, 25 / 24, 23.976 / 24, 24 / 23.976)
ALIGN_SPEED_MARGIN = 0.05
_WORD = re.compile(r"[a-z']+")


def _share(cues: list[Cue], starts: list[float], vocab: list[set[str]],
           sample: list[tuple[float, str]], offset: float, slack: float = 1.0) -> float:
    """Share of sampled words that a line on screen at (their time + offset),
    give or take `slack` seconds, contains."""
    if not sample:
        return 0.0
    hit = 0
    for at, word in sample:
        at += offset
        i = bisect.bisect_right(starts, at + slack) - 1
        while i >= 0 and cues[i].end >= at - slack:
            if word in vocab[i]:
                hit += 1
                break
            i -= 1
    return hit / len(sample)


def _middle(scored: list[tuple[float, float]]) -> tuple[float, float]:
    """(shift, its score): the middle of the best-scoring run of shifts.

    A line stays on screen for seconds, so neighbouring shifts score about the
    same; the middle of that run is the one that lines up, not whichever end
    max() happens to land on."""
    best = max(v for _o, v in scored)
    top = [o for o, v in scored if v >= best * 0.98]
    return round((min(top) + max(top)) / 2 * 4) / 4, best


def align(cues: list[Cue], heard: list[dict]) -> tuple[list[Cue], float, float]:
    """(the cues moved to line up with what was heard; smallest and largest
    shift applied, in seconds - both 0 when they were left alone).

    Each common frame-rate ratio is tried (ALIGN_SPEEDS), then a shift for the
    whole file, then a shift per window. The result is kept only when, in the
    end, it clearly fits better than leaving the cues alone - otherwise they
    come back unmoved, and if they belong to another cut entirely fits() sets
    them aside. Judged on the end result rather than the whole-file shift
    alone: subtitles that drift at every advert break fit no single shift well,
    and an audit found two of four such episodes given up on that way.
    """
    if not cues:
        return cues, 0.0, 0.0
    here = _fit_share(cues, heard)
    here_tight = _fit_share(cues, heard, 0.3)
    best = None
    for speed in ALIGN_SPEEDS:
        timed = cues if speed == 1.0 else [Cue(c.start / speed, c.end / speed, c.text)
                                            for c in cues]
        moved = _align_at_speed(timed, heard)
        if moved is None:
            continue
        score = _fit_share(moved, heard)
        # A clear gain, or - once it fits well - a small one measured tightly,
        # where a line a fraction of a second off shows.
        clear = score >= ALIGN_MIN_SHARE and score - here >= ALIGN_MIN_GAIN
        tighter = (score >= ALIGN_GOOD_FIT
                   and _fit_share(moved, heard, 0.3) - here_tight >= ALIGN_SMALL_GAIN)
        if not (clear or tighter):
            continue
        if best is None or score > best[0] + (ALIGN_SPEED_MARGIN if best[2] == 1.0 else 0.0):
            best = (score, moved, speed)
    if best is None:
        return cues, 0.0, 0.0
    moved = best[1]
    shifts = [round(c.start - m.start, 2) for c, m in zip(cues, moved)]
    return moved, min(shifts), max(shifts)


def _fit_share(cues: list[Cue], heard: list[dict], slack: float = 1.0) -> float:
    """Share of sampled heard words inside a line that contains them, as they
    stand, give or take `slack` seconds."""
    ordered = sorted(cues, key=lambda c: c.start)
    sample = [(float(w.get("start") or 0.0), words.normalize(w.get("word", ""))) for w in heard]
    sample = [(t, w) for t, w in sample if len(w) >= 4][::2][:3000]
    if not ordered or not sample:
        return 0.0
    vocab = [set(_WORD.findall(c.text.lower())) for c in ordered]
    return _share(ordered, [c.start for c in ordered], vocab, sample, 0.0, slack)


def _align_at_speed(cues: list[Cue], heard: list[dict]) -> list[Cue] | None:
    """The cues moved by a whole-file shift and then window by window, in the
    same order as given; None when there is nothing to line up with."""
    ordered = sorted(cues, key=lambda c: c.start)
    words_heard = [(float(w.get("start") or 0.0), words.normalize(w.get("word", "")))
                   for w in heard]
    words_heard = [(t, w) for t, w in words_heard if len(w) >= 4]
    sample = words_heard[::2][:3000]
    if not ordered or not sample:
        return None
    starts = [c.start for c in ordered]
    vocab = [set(_WORD.findall(c.text.lower())) for c in ordered]

    def share(words_, off, slack=1.0):
        return _share(ordered, starts, vocab, words_, off, slack)

    _best, rough = max((share(sample, float(k)), float(k))
                       for k in range(-int(ALIGN_LIMIT), int(ALIGN_LIMIT) + 1))
    offset, _best = _middle([(rough + d / 4, share(sample, rough + d / 4))
                             for d in range(-12, 13)])

    # Every word heard, short ones included, for finding where a line starts.
    spoken = sorted((float(w.get("start") or 0.0), words.normalize(w.get("word", "")))
                    for w in heard)
    spoken_at = [t for t, _w in spoken]

    def settle(local: float, lo: float, hi: float) -> float:
        """`local` corrected so each line starts where its first word is heard.

        The best-fit run above is found from where words fall inside lines,
        and a line stays on screen after its words end - reading time - so the
        middle of that run sits about half a second late. Where a line begins
        is what a subtitler times to, so the typical gap between the start of a
        line and its first word heard is taken off. Needs three lines that
        agree on their first word; moves at most a second."""
        gaps = []
        for cue in ordered:
            at = cue.start - local
            if not lo <= at < hi:
                continue
            first = _WORD.findall(cue.text.lower())
            if not first:
                continue
            a = bisect.bisect_left(spoken_at, at - 1.0)
            b = bisect.bisect_right(spoken_at, at + 1.0)
            near = [t - at for t, w in spoken[a:b] if w == first[0]]
            if near:
                gaps.append(min(near, key=abs))
        if len(gaps) < 3:
            return local
        gaps.sort()
        middle = gaps[len(gaps) // 2]
        return round(local - max(-1.0, min(1.0, middle)), 2)

    # Window by window, on the transcript's clock, with a tighter slack: here
    # the question is which line, not which film.
    end = words_heard[-1][0] if words_heard else 0.0
    windows: list[tuple[float, float]] = []
    centre = ALIGN_SPAN / 2
    while centre - ALIGN_SPAN / 2 <= end:
        chunk = [(t, w) for t, w in words_heard
                 if centre - ALIGN_SPAN / 2 <= t < centre + ALIGN_SPAN / 2]
        local = offset
        if len(chunk) >= ALIGN_MIN_WORDS:
            reach = int(ALIGN_REACH * 4)
            found, score = _middle([(offset + d / 4, share(chunk, offset + d / 4, 0.3))
                                    for d in range(-reach, reach + 1)])
            if score >= ALIGN_MIN_SHARE:
                local = found
        local = settle(local, centre - ALIGN_SPAN / 2, centre + ALIGN_SPAN / 2)
        windows.append((centre, local))
        centre += ALIGN_STEP

    # A word heard at t was shown at t + shift, so a line shown at s belongs
    # at s - shift. Each line takes the shift of the window it falls in - or,
    # next to an advert break where the shift steps, a neighbouring window's,
    # if that puts more of its own words where they were heard.
    centres = [c for c, _ in windows]
    times = [t for t, _ in words_heard]

    def fits(cue: Cue, shift: float) -> int:
        lo = bisect.bisect_left(times, cue.start - shift - 0.3)
        hi = bisect.bisect_right(times, cue.end - shift + 0.3)
        mine = set(_WORD.findall(cue.text.lower()))
        return sum(1 for _t, w in words_heard[lo:hi] if w in mine)

    moved = []
    for cue in cues:
        heard_at = cue.start - offset
        k = bisect.bisect_left(centres, heard_at)
        if k == len(centres) or (k > 0 and heard_at - centres[k - 1] < centres[k] - heard_at):
            k -= 1
        k = max(k, 0)
        shift = windows[k][1]
        for other in {windows[j][1] for j in (k - 1, k + 1) if 0 <= j < len(windows)} - {shift}:
            if fits(cue, other) > fits(cue, shift):
                shift = other
        moved.append(Cue(cue.start - shift, cue.end - shift, cue.text))
    return moved


# Whether a set of subtitles is of this dialogue at all. Measured after lining
# up: the right subtitles put 78-98% of heard words in the line on screen,
# another film's 2%. Checked against everything heard, not only the swears, so
# a file with two detections is protected as well as one with twenty.
FIT_MIN = 0.3
FIT_MIN_WORDS = 20          # fewer words heard than this: no verdict


def fits(cues: list[Cue], heard: list[dict]) -> bool:
    """False when enough was heard to tell, and the subtitles do not follow it -
    a commentary track, another episode, a language tagged wrongly."""
    ordered = sorted(cues, key=lambda c: c.start)
    sample = [(float(w.get("start") or 0.0), words.normalize(w.get("word", "")))
              for w in heard]
    sample = [(t, w) for t, w in sample if len(w) >= 4][::2][:3000]
    if not ordered or len(sample) < FIT_MIN_WORDS:
        return True
    vocab = [set(_WORD.findall(c.text.lower())) for c in ordered]
    return _share(ordered, [c.start for c in ordered], vocab, sample, 0.0) >= FIT_MIN


# Reading the script around a word. Asking only "does the line on screen have
# the word?" cannot tell a subtitle that leaves a swear out from one that says
# something else in its place, and the two mean opposite things: only the
# second is evidence Whisper misheard. So the words heard just before and after
# are found in the subtitles, and what sits between them is the answer. On
# Trap House (2025) this turned 5 "says something else" into "leaves it out"
# and 3 into "agrees", and changed none of the 84 that already agreed.
_ANCHOR_STOP = {"a", "an", "the", "i", "i'm", "im", "my", "me", "you", "your", "it", "its",
                "it's", "oh", "uh", "um", "and", "or", "but", "to", "of", "in", "on", "at",
                "is", "was", "be", "we", "he", "she", "they", "so", "no", "yes", "yeah", "ok",
                "okay", "up", "for", "that", "this", "just", "like", "what", "do", "don't", "not"}
SCRIPT_CONTEXT = 6.0       # seconds of subtitles either side of the word
SCRIPT_MAX_GAP = 6         # subtitle words allowed between the two anchors
WIDE_WINDOW = 2.5          # the nearby-line check, when nothing anchors
_SPLIT = re.compile(r"[\s\-—–/]+")
_CUT = re.compile(r"(--|—|…|\.\.\.)$")


def _script_tokens(cues: list[Cue], at: float) -> list[tuple[str, str]]:
    """(bare word, as written) for every subtitle word around `at`, in order."""
    out = []
    for c in sorted(cues, key=lambda c: c.start):
        if c.end >= at - SCRIPT_CONTEXT and c.start <= at + SCRIPT_CONTEXT:
            for raw in _ASIDE.sub(" ", c.text).split():
                for part in _SPLIT.split(raw):
                    bare = words.normalize(part)
                    if bare:
                        out.append((bare, raw))
    return out


def _anchors(heard: list[str], i: int, step: int) -> list[tuple[tuple[str, ...], int]]:
    """Words to find on one side of heard[i], nearest first: a distinctive word
    on its own, or a pair of words for common ones. Each comes with how many
    heard words lie between it and the detection."""
    out = []
    for k in range(1, 4):
        j = i + step * k
        if not 0 <= j < len(heard):
            break
        w = heard[j]
        if w and w not in _ANCHOR_STOP and len(w) >= 3:
            out.append(((w,), k - 1))
        j2 = j + step
        if 0 <= j2 < len(heard) and heard[j2] and w:
            out.append(((heard[j2], w) if step < 0 else (w, heard[j2]), k - 1))
    return out


def _at(tokens: list[tuple[str, str]], seq: tuple[str, ...], start: int = 0, end: int = -1):
    end = len(tokens) if end < 0 else min(end, len(tokens))
    for p in range(start, end - len(seq) + 1):
        if tuple(t for t, _ in tokens[p:p + len(seq)]) == seq:
            yield p


def _in_the_script(match: words.Match, word: str, heard: list[dict], cues: list[Cue],
                   matcher: words.Matcher) -> tuple[str, str] | None:
    """AGREES / OMITS / REPLACED / SOUNDALIKE from what the subtitles have
    between the words heard either side, or None when neither side is found."""
    bare = [words.normalize(h.get("word", "")) for h in heard]
    times = [float(h.get("start") or 0.0) for h in heard]
    k = bisect.bisect_left(times, match.start - 0.05)
    near = [j for j in range(max(0, k - 3), min(len(bare), k + 4))
            if bare[j] == word or (len(word) >= 4 and bare[j].startswith(word[:4]))]
    if not near:
        return None
    i = min(near, key=lambda j: abs(times[j] - match.start))
    tokens = _script_tokens(cues, match.start)
    if not tokens:
        return None
    # Words Whisper heard around the swear: in the line, they were said too,
    # so they are not what the swear really was.
    heard_here = _heard_stems(heard, match.start)

    def says(slot) -> bool:
        return any(_is_word(raw, word, matcher) or _BLEEP.search(raw)
                   or (len(t) >= 4 and word.startswith(t) and _CUT.search(raw))
                   for t, raw in slot)

    def instead(slot, rest, lgap, rgap) -> tuple[str, str]:
        """(REPLACED or SOUNDALIKE, the words in its place) for a slot holding other words."""
        shown = [raw for t, raw in slot if t in rest]
        said = " ".join(shown).strip(" ,.!?;:-")
        if any(sounds.softened(t) for t in rest) or matcher.find(
                [{"word": raw, "start": 0.0, "end": 0.0} for raw in shown]):
            return REPLACED, said
        # Cut short ("mother" for "motherfucker"), or run on ("pussycat").
        if any(_softens(raw, word, matcher) for raw in shown):
            return REPLACED, said
        # What was heard there that the line does not have - the swear, and
        # any words misheard with it: "road to hell" against "Roosevelt".
        in_line = {t for t, _raw in slot}
        span = [w for w in bare[i - lgap:i + 1 + rgap] if w == bare[i] or w not in in_line]
        # From its first content word to its last: a "to" the line dropped is
        # no part of the mishearing ("to hell" scored 0.65+ against "table").
        while len(span) > 1 and span[0] != bare[i] and sounds.function_word(span[0]):
            span.pop(0)
        while len(span) > 1 and span[-1] != bare[i] and sounds.function_word(span[-1]):
            span.pop()
        spoken = " ".join(span)
        new = [raw for raw in shown if not _stems(words.normalize(raw)) & heard_here]
        if new and (_alike(spoken, " ".join(new)) or any(_alike(word, raw) for raw in new)):
            return SOUNDALIKE, said
        return REPLACED, said

    for left, lgap in _anchors(bare, i, -1):
        for right, rgap in _anchors(bare, i, +1):
            between = set(bare[i - lgap:i] + bare[i + 1:i + 1 + rgap])
            outcomes = []
            for p in _at(tokens, left):
                a = p + len(left)
                found = False
                for q in _at(tokens, right, a, a + SCRIPT_MAX_GAP + len(right)):
                    found = True
                    slot = tokens[a:q]
                    rest = [t for t, _raw in slot if t not in between]
                    outcomes.append((AGREES, "") if says(slot) else (OMITS, "") if not rest
                                    else instead(slot, rest, lgap, rgap))
                    break
                # Nothing lines up after it, and the subtitles break off right
                # where the word was: "on your period or..."
                if not found and _CUT.search(tokens[a - 1][1]):
                    outcomes.append((OMITS, ""))
            if outcomes:
                # Any reading will do when a phrase repeats, and every doubt
                # lands on muting: a sound-alike only when it is the only one.
                for state in (AGREES, OMITS, REPLACED):
                    for got in outcomes:
                        if got[0] == state:
                            return got
                return outcomes[0]
    # A line cut off right on the word: "Kill this mother--".
    for left, _gap in _anchors(bare, i, -1):
        for p in _at(tokens, left):
            a = p + len(left)
            if a < len(tokens) and _CUT.search(tokens[a][1]) and len(tokens[a][0]) >= 4 \
                    and word.startswith(tokens[a][0]):
                return AGREES, ""
    # Only one side found - the line ends on the word, or starts with it: the
    # subtitle word right next to that side. Enough to see the word itself, or
    # one that sounds like it ("Get some caulk."); not enough to call anything
    # else a softening, since it may simply be the next word said.
    for side, step in ((-1, 0), (+1, -1)):
        for anchor, gap in _anchors(bare, i, side):
            if gap:
                continue
            for p in _at(tokens, anchor):
                k = p + len(anchor) if side < 0 else p - 1
                if not 0 <= k < len(tokens):
                    continue
                t, raw = tokens[k]
                if _is_word(raw, word, matcher) or _BLEEP.search(raw):
                    return AGREES, ""
                if not _softens(raw, word, matcher) and not _stems(t) & heard_here \
                        and _alike(word, raw):
                    return SOUNDALIKE, raw.strip(" ,.!?;:-")
    return None


# Endings, not words: "fuck" + "ing" is not a compound.
_ENDINGS = frozenset({"ing", "ings", "ed", "er", "ers", "es", "in", "ted", "ting", "ter", "ty"})


@lru_cache(maxsize=4096)
def _halves(word: str) -> tuple[str, str, bool] | None:
    """(harmless half, swear half, whether the harmless half comes first) for a
    compound swear - "mother" + "fucker", "dumb" + "ass", "dick" + "head" - or None."""
    if not word.isalpha():
        return None
    known, listed = sounds._dictionary(), _listed
    for i in range(3, len(word) - 2):
        a, b = word[:i], word[i:]
        if a in known and b in known:
            if listed(b) and not listed(a) and a not in _ENDINGS:
                return a, b, True
            if listed(a) and not listed(b) and b not in _ENDINGS:
                return b, a, False
    return None


@lru_cache(maxsize=4096)
def _listed(word: str) -> bool:
    return bool(_everyone().find([{"word": word, "start": 0.0, "end": 0.0}]))


@lru_cache(maxsize=1)
def _everyone() -> words.Matcher:
    return words.Matcher(context_words=frozenset())


def _alike(heard: str, shown: str) -> bool:
    """sounds.sounds_alike, except that a caption keeping the harmless half of a
    compound swear is compared on the other half alone: "mother-trucker" is
    trucker against fucker, "smarty" is -y against ass. The shared half made
    them score as sound-alikes (0.84, 0.84) and a dub was left in. With nothing
    left once the shared half is taken off, the swear was cut short. Nor is a
    caption of only function words a sound-alike: see sounds.FUNCTION_WORDS."""
    shown_words = _WORD.findall(str(shown or "").lower().replace("’", "'"))
    if shown_words and all(sounds.function_word(w) for w in shown_words):
        return False
    halves = _halves(heard)
    y = sounds.phones(shown)
    if halves and y:
        harmless, swear, first = halves
        h = sounds.phones(harmless) or []
        if h and first and y[:len(h)] == h:
            return sounds.phones_alike(sounds.phones(swear), y[len(h):])
        if h and not first and y[-len(h):] == h:
            return sounds.phones_alike(sounds.phones(swear), y[:-len(h)])
    return sounds.sounds_alike(heard, shown)


def _stems(bare: str) -> set[str]:
    """A word and what it may be short for: "she's" -> she, "we're" -> we,
    "gets" -> get. A caption contracts and inflects what was said."""
    plain = _plain(bare)
    out = {plain}
    for end in ("s", "es", "d", "ed", "ing", "ll", "re", "ve", "nt"):
        if plain.endswith(end) and len(plain) - len(end) >= 2:
            out.add(plain[:-len(end)])
    return out


def _heard_stems(heard: list[dict], at: float, seconds: float = 3.0) -> set[str]:
    out: set[str] = set()
    for h in heard:
        if abs(float(h.get("start") or 0.0) - at) <= seconds:
            out |= _stems(words.normalize(h.get("word", "")))
    return out


def _softens(raw: str, word: str, matcher: words.Matcher) -> bool:
    """Whether a caption word stands in for the swear rather than sounding like
    it, reading each part of a hyphenated one: a minced oath ("bull-shoot",
    "god dang"), another swear ("horse-crap"), or the swear cut short ("mother",
    "bull-", "jack", "bone" for "boner")."""
    parts = [words.normalize(x) for x in [raw, *_SPLIT.split(raw)]]
    for part in filter(None, parts):
        if sounds.softened(part) or matcher.find([{"word": part, "start": 0.0, "end": 0.0}]):
            return True
        plain = _plain(part)
        if len(plain) >= 3 and plain != word and (word.startswith(plain) or plain.startswith(word)):
            # Cut short ("puss"), or the swear with more on it ("pussycat",
            # "what the dickens") - unless it is the same sound spelled
            # another way: "dam" is not "damn" cut short, it is a homophone.
            x, y = sounds.phones(plain), sounds.phones(word)
            if not (x and y and x == y):
                return True
    return bool(_STUB.fullmatch(raw))


def evidence(match: words.Match, cues: list[Cue], matcher: words.Matcher,
             heard: list[dict] | None = None) -> tuple[str, str]:
    """(state, the subtitle line) - see _evidence."""
    return _evidence(match, cues, matcher, heard)[:2]


def _evidence(match: words.Match, cues: list[Cue], matcher: words.Matcher,
              heard: list[dict] | None = None) -> tuple[str, str, str]:
    """(state, the subtitle line, the words in the swear's place) for one
    detection; state is "" when no line is near.

    A line on screen at that moment that has the word settles it. Otherwise
    the script around it is read (see _in_the_script), when the transcript is
    given; failing that, the lines a little either side are checked.
    """
    word = words.normalize(match.text)
    near = [c for c in cues if c.start - WINDOW <= match.end and c.end + WINDOW >= match.start]
    if near and _says_it(" ".join(c.text for c in near), word, matcher):
        return AGREES, " ".join(c.text for c in near)[:200], ""
    wide = [c for c in cues
            if c.start - WIDE_WINDOW <= match.end and c.end + WIDE_WINDOW >= match.start]
    shown = " ".join(c.text for c in (near or wide))[:200]
    if heard:
        found = _in_the_script(match, word, heard, cues, matcher)
        if found is not None:
            return found[0], shown, found[1]
    if not wide:
        return "", "", ""
    # From a line a little further off only the word itself counts: the next
    # line's own swear ("What the fuck is that?") says nothing about this one.
    if any(_says_it(c.text, word, matcher) for c in wide):
        return AGREES, shown, ""
    alike = _sounds_alike_on_screen(match, word, near or wide, matcher, heard or [])
    if alike:
        return SOUNDALIKE, shown, alike
    return DIFFERS, shown, ""


# How far a word may sit from where its place in the line puts it in time.
LINE_POSITION = 1.2


def _sounds_alike_on_screen(match: words.Match, word: str, cues: list[Cue],
                            matcher: words.Matcher, heard: list[dict]) -> str:
    """A word in the lines on screen that sounds like the swear, at about the
    right point of its line - for when the script cannot be read around it
    (a chant: "City! City!", heard as "Shit, shit!"). Where a line's words fall
    is estimated from their place in it, and only one within LINE_POSITION
    seconds of the swear counts, so another "help" in the line cannot excuse a
    "hell". Nor does a word Whisper also heard close by: it was said as well,
    so it is not what the swear really was ("This is what you get." with the
    "Oh my god" after it left out)."""
    said = _heard_stems(heard, match.start)
    for cue in cues:
        tokens = [r for r in _ASIDE.sub(" ", cue.text).split() if words.normalize(r)]
        if not tokens or _reworded(cue, tokens, heard, match):
            continue
        step = (cue.end - cue.start) / len(tokens)
        for n, raw in enumerate(tokens):
            at = cue.start + step * (n + 0.5)
            if abs(at - (match.start + match.end) / 2) > LINE_POSITION + step / 2:
                continue
            bare = words.normalize(raw)
            if _stems(bare) & said:
                continue
            if _softens(raw, word, matcher):
                continue
            if _alike(word, raw):
                return raw.strip(" ,.!?;:-")
    return ""


def _reworded(cue: Cue, tokens: list[str], heard: list[dict], match: words.Match) -> bool:
    """Whether a line leaves out words Whisper heard while it was on screen -
    content words, not the swears or the little words captions drop. Then the
    line was reworded ("It's freezing out here." for "it is fucking cold out
    here"), and where a word falls in it says nothing about what the swear was.
    A chant ("City! City!" heard as "Shit, shit!") leaves nothing out."""
    shown: set[str] = set()
    for raw in tokens:
        for part in [raw, *_SPLIT.split(raw)]:
            if words.normalize(part):
                shown |= _stems(words.normalize(part))
    for h in heard:
        at = float(h.get("start") or 0.0)
        if not cue.start - 0.3 <= at <= cue.end + 0.3 or abs(at - match.start) < 0.01:
            continue
        bare = words.normalize(h.get("word", ""))
        if len(bare) < 3 or sounds.function_word(bare) or _listed(bare):
            continue
        if not _stems(bare) & shown:
            return True
    return False


def annotate(matches: list[words.Match], cues: list[Cue],
             heard: list[dict] | None = None) -> tuple[list[words.Match], str]:
    """Each match with its subtitle evidence, and a note if the subtitles were set aside.

    The comparison uses every built-in list whatever the household switched
    off: the question is whether the subtitle has a swear in it, not whether
    this household mutes that one.
    """
    if not cues or not matches:
        return matches, ""
    matcher = words.Matcher(context_words=frozenset())
    judged = [(m, *_evidence(m, cues, matcher, heard)) for m in matches]
    checked = [state for _m, state, _t, _i in judged if state]
    differing = sum(1 for state in checked if state in (DIFFERS, REPLACED, SOUNDALIKE))
    if len(checked) >= MISMATCH_MIN and differing / len(checked) > MISMATCH_SHARE:
        return matches, (f"subtitles set aside: {differing} of {len(checked)} lines "
                         f"disagreed, so they probably belong to another release")
    reasons = {SOUNDALIKE: "left in: the subtitles have “{}” here, which sounds like it",
               REPLACED: "the subtitles soften it to “{}”"}
    return [replace(m, subtitle=text, subtitle_state=state,
                    reason=reasons[state].format(said) if state in reasons and said else m.reason)
            for m, state, text, said in judged], ""


def worth_checking(match) -> bool:
    """Whether a detection is worth a listen - whether it could be wrong:
    left in because the subtitles' word only sounds like it, muted on the
    subtitles' word alone, or Whisper was unsure and nothing could check it.

    Whisper being unsure is not enough once the subtitles have ruled: a word
    they have, soften or leave out is settled. Flagging those made a film show
    fourteen words worth a listen where one was (Trap House)."""
    state = match["subtitle_state"] if not hasattr(match, "subtitle_state") else match.subtitle_state
    sure = match["confidence"] if not hasattr(match, "confidence") else match.confidence
    return (state in (SOUNDALIKE, SUBTITLE_ONLY)
            or (not state and sure is not None and sure < LOW_CONFIDENCE))


def decide(matches: list[words.Match]
           ) -> tuple[list[words.Match], list[words.Match], list[words.Match]]:
    """(muted on the subtitles' word, sound-alikes, still open).

    Works on matches annotate() has already marked. A sound-alike is the one
    case the subtitles leave in; the caller may ask a second opinion first.
    Still open means no line was near: the model, when there is one and the
    word is on its list, or muting, decides.
    """
    reasons = {AGREES: "the subtitles have it",
               OMITS: "the subtitles leave it out - captions soften",
               REPLACED: "the subtitles soften it",
               DIFFERS: "a line nearby says something else"}
    muted, alike, still_open = [], [], []
    for m in matches:
        if m.subtitle_state == SOUNDALIKE:
            alike.append(m)
        elif m.subtitle_state in reasons:
            muted.append(replace(m, reason=m.reason or reasons[m.subtitle_state],
                                 needs_review=m.needs_review))
        else:
            still_open.append(m)
    return muted, alike, still_open


# Swears the subtitles have that Whisper did not catch - mumbled, talked over,
# or heard as something else. Placed between the words heard either side.
PLACE_MAX = 1.5            # seconds: longer than this is not one word
PLACE_MIN = 0.2            # shorter: there was no room for it in the audio
PLACE_ONE_SIDE = 0.7       # with only one side found, this long at most
_ASIDE = re.compile(r"\([^)]*\)|\[[^\]]*\]|(?:^|(?<=\s)|(?<=-))[A-Z][A-Z .'’-]+:")


def from_subtitles(cues: list[Cue], heard: list[dict], matches: list[words.Match],
                   matcher: words.Matcher) -> list[words.Match]:
    """Detections for the swears in the (lined-up) subtitles that no detection
    covers, placed in the audio by the words heard either side of them.

    `matcher` is the household's own, so its lists and exceptions apply; words
    it checks in context are left alone here. A swear with no room for it in
    the audio - the words either side were said back to back - is not placed:
    the captions added it.
    """
    spoken = [(float(h.get("start") or 0.0), float(h.get("end") or 0.0),
               words.normalize(h.get("word", "")), str(h.get("word", "")).strip())
              for h in heard]
    starts = [w[0] for w in spoken]
    taken = [(m.start, m.end) for m in matches]
    everyone = words.Matcher(context_words=frozenset())
    out: list[words.Match] = []

    def same(raw: str, detected: str) -> bool:
        word = words.normalize(detected)
        return bool(word) and (_is_word(raw, word, everyone)
                               or _is_word(detected, words.normalize(raw), everyone))

    for cue in sorted(cues, key=lambda c: c.start):
        text = _ASIDE.sub(" ", cue.text)
        raw_tokens = [r for r in text.split() if words.normalize(r) or _HIDDEN_WORD.fullmatch(r)]
        tokens = [(words.normalize(r), r) for r in raw_tokens]
        # What Whisper already caught around this line, used up one by one: a
        # line saying "Oh shit" three times needs three detections, not one
        # each for every "shit" in it.
        caught = [m for m in matches
                  if cue.start - WIDE_WINDOW <= m.start <= cue.end + WIDE_WINDOW]
        for n, (bare, raw) in enumerate(tokens):
            found = matcher.find([{"word": raw, "start": 0.0, "end": 0.0}])
            hidden = _hides_a_swear(raw) and "strong" in matcher.categories
            if not (found or hidden):
                continue
            if found and bare in matcher.context_words:
                continue
            mine = next((m for m in caught if same(raw, m.text)), None)
            if mine is not None:
                caught.remove(mine)
                continue
            lo = bisect.bisect_left(starts, cue.start - 3.0)
            hi = bisect.bisect_right(starts, cue.end + 3.0)
            window = spoken[lo:hi]
            place = _place(tokens, n, window)
            if place is None:
                continue
            start, end, instead_of = place
            if any(s < end + 0.3 and e > start - 0.3 for s, e in taken):
                continue
            taken.append((start, end))
            out.append(words.Match(
                start=start, end=end, text=raw,
                category=found[0].category if found else "strong",
                needs_review=True, confidence=None, subtitle=cue.text[:200],
                subtitle_state=SUBTITLE_ONLY,
                reason=("muted from the subtitles: Whisper heard “" + instead_of + "”"
                        if instead_of else "muted from the subtitles: Whisper heard nothing")))
    return out


@lru_cache(maxsize=1)
def _all_listed() -> tuple[str, ...]:
    out = set(words.BLASPHEMY_SOLO)
    for group in words.WORDLISTS.values():
        out |= set(group)
    return tuple(sorted(out))


def _hides_a_swear(raw: str) -> bool:
    """A word with letters hidden that fits a listed swear: "f***", "sh*t",
    "a**hole". Names written with a symbol - "Ke$ha", "A$AP", "C#",
    "E*Trade", "M*A*S*H" - fit none, and a lone $ or # is a name or a note,
    not a bleep: each was muted as a swear before."""
    if not _HIDDEN_WORD.fullmatch(raw):
        return False
    if "*" not in raw and sum(len(x) for x in _HIDDEN.findall(raw)) < 2:
        return False
    return any(_is_word(raw, w, _everyone()) for w in _all_listed())


def _place(tokens, n, window) -> tuple[float, float, str] | None:
    curse = tokens[n][0]
    """(start, end, what Whisper heard there) for subtitle token n, from the
    subtitle words either side found among the heard ones; None if neither
    side is found, or the gap cannot hold a word."""
    bare = [w[2] for w in window]

    def find(seq, forward: bool):
        hits = [p for p in range(len(bare) - len(seq) + 1)
                if tuple(bare[p:p + len(seq)]) == seq]
        return hits

    def usable(seq, k) -> bool:
        return len(seq) == k and all(seq) and (k == 2 or (seq[0] not in _ANCHOR_STOP
                                                          and len(seq[0]) >= 3))

    # Every place either side is found, paired nearest first: when the words
    # either side repeat ("about hell about"), the last "about" before the
    # swear and the first after it are not the pair that holds it.
    lefts, rights = [], []
    for k in (1, 2):
        if n - k >= 0:
            seq = tuple(t for t, _r in tokens[n - k:n])
            if usable(seq, k):
                lefts += [p + k - 1 for p in find(seq, True)]
        seq = tuple(t for t, _r in tokens[n + 1:n + 1 + k])
        if usable(seq, k):
            rights += find(seq, False)
    pairs = sorted((r - l, l, r) for l in lefts for r in rights if 0 < r - l <= 4)
    if pairs:
        _gap, left, right = pairs[0]
        start, end = window[left][1], window[right][0]
        between = " ".join(w[3] for w in window[left + 1:right])
        if not PLACE_MIN <= end - start <= PLACE_MAX:
            return None
        return start, end, between

    left = right = None
    paired = False
    for k in (1, 2):                              # one distinctive word, or a pair
        if left is None and n - k >= 0:
            seq = tuple(t for t, _r in tokens[n - k:n])
            if all(seq) and (k == 2 or (seq[0] not in _ANCHOR_STOP and len(seq[0]) >= 3)):
                hits = find(seq, True)
                if hits:
                    left = hits[-1] + len(seq) - 1          # index of the word before
                    paired = paired or k == 2
        if right is None and n + k < len(tokens) + 1:
            seq = tuple(t for t, _r in tokens[n + 1:n + 1 + k])
            if len(seq) == k and all(seq) and (k == 2 or (seq[0] not in _ANCHOR_STOP
                                                          and len(seq[0]) >= 3)):
                hits = find(seq, False)
                if hits:
                    right = hits[0]
                    paired = paired or k == 2
    if left is not None and right is not None and right <= left:
        # The first match after the left side, if there is one close by.
        later = [p for p in range(left + 1, min(len(bare), left + 6))
                 if right is not None and bare[p] == bare[right]]
        right = later[0] if later else None
    if left is not None and right is not None:
        if right - left - 1 > 3:
            return None
        start, end = window[left][1], window[right][0]
        between = " ".join(w[3] for w in window[left + 1:right])
    elif not paired:
        # One side, found by a single word: too easily some other "know".
        return None
    elif left is not None:
        # The word heard just after may be the swear, misheard: "Dan" for "Damn".
        if left + 1 < len(window) and sounds.sounds_alike(window[left + 1][2], curse):
            return window[left + 1][0], window[left + 1][1], window[left + 1][3]
        start = window[left][1]
        nxt = window[left + 1][0] if left + 1 < len(window) else start + PLACE_ONE_SIDE
        end = min(start + PLACE_ONE_SIDE, nxt)
        between = ""
    elif right is not None:
        if right > 0 and sounds.sounds_alike(window[right - 1][2], curse):
            return window[right - 1][0], window[right - 1][1], window[right - 1][3]
        end = window[right][0]
        prv = window[right - 1][1] if right > 0 else end - PLACE_ONE_SIDE
        start = max(end - PLACE_ONE_SIDE, prv)
        between = ""
    else:
        return None
    if not PLACE_MIN <= end - start <= PLACE_MAX:
        return None
    return start, end, between


# ---------------------------------------------------------------------------
#  A censored copy of the subtitles
# ---------------------------------------------------------------------------
#  Turning on subtitles to follow a thick accent used to put every swear the
#  audio track silenced back on screen. So the subtitles the words were
#  checked against - already lined up with the dialogue - are written out
#  again with the swears masked, as a track beside the censored audio.
#
#  A word is masked when the household's word lists catch it in the line,
#  unless the audio side heard that word at that moment and deliberately left
#  it in (a reverent "God", a word marked wrong by hand). Words the audio side
#  muted are masked too even where the lists alone would not catch them - a
#  word muted by hand, say.

MASK_SLACK = 1.0       # seconds either side of a line that still count as it
_TOKEN = re.compile(r"\S+")
_LETTERS = re.compile(r"[^\W_]")


def _mask(token: str) -> str:
    return _LETTERS.sub("*", token)


def censor(cues: list[Cue], muted: list[words.Match], kept: list[words.Match],
           matcher: words.Matcher) -> list[Cue]:
    """The cues with the swears masked."""
    tokens: list[list[str]] = [_TOKEN.findall(c.text) for c in cues]
    heard, where = [], {}
    for ci, (cue, row) in enumerate(zip(cues, tokens)):
        for ti, tok in enumerate(row):
            at = round(cue.start + ti * 1e-4, 4)
            where[at] = (ci, ti)
            heard.append({"word": tok, "start": at, "end": at})
    hide = {where[m.start] for m in matcher.find(heard) if m.start in where}

    def near(cue: Cue, ms: list[words.Match]) -> list[str]:
        return [words.normalize(m.text) for m in ms
                if cue.start - MASK_SLACK <= m.start <= cue.end + MASK_SLACK]

    out = []
    for ci, (cue, row) in enumerate(zip(cues, tokens)):
        said, spared = near(cue, muted), near(cue, kept)
        for ti, tok in enumerate(row):
            this = (ci, ti)
            if this in hide:
                # Left in on purpose, and not also muted here: leave it shown.
                if (any(_is_word(tok, w, matcher) for w in spared)
                        and not any(_is_word(tok, w, matcher) for w in said)):
                    hide.discard(this)
            elif any(w and _is_word(tok, w, matcher) for w in said):
                hide.add(this)
        masked = [(_mask(tok) if (ci, ti) in hide else tok) for ti, tok in enumerate(row)]
        out.append(Cue(cue.start, cue.end, " ".join(masked)))
    return out


def _stamp(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list[Cue]) -> str:
    return "".join(f"{n}\n{_stamp(c.start)} --> {_stamp(c.end)}\n{c.text}\n\n"
                   for n, c in enumerate(cues, 1) if c.text.strip() and c.end > c.start)
