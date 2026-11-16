"""ffmpeg work: what is in a file, and how the cleaned track gets into it.

The original file is never edited in place. Everything is built beside it and
swapped in only once ffprobe agrees the result has every stream the original
had, plus the new one, at the same duration. A media library is not a thing to
be clever with.
"""

from __future__ import annotations

from . import env

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

# Thread cap for every ffmpeg call. Audio work is nearly single-threaded
# anyway, and leaving the rest of the cores alone is what keeps Plex playing
# while a file is being cleaned. Set by the pipeline from settings.
THREADS = int(env.get("FFMPEG_THREADS", "2"))


def _threads() -> list[str]:
    return ["-threads", str(THREADS)] if THREADS else []

# What the added track is called, and what marks a track as ours. The name is
# a setting, so the pipeline calls set_clean_title() before every job. Only
# CLEAN_TITLE is written; every name in KNOWN_TITLES is recognised.
DEFAULT_CLEAN_TITLE = "Censored - English"
CLEAN_TITLE = DEFAULT_CLEAN_TITLE
KNOWN_TITLES = {DEFAULT_CLEAN_TITLE.lower(), "cleaned - english"}

# A mark that does not depend on the name at all. Matroska keeps arbitrary
# stream tags, so an MKV track stays ours through any number of renames; MP4
# drops them, which is why the names are still matched as well.
MARK_KEY = "censarr"
MARK_VALUE = "1"
# Written by the same app under its old name, Cleanarr: still ours.
OLD_MARK_KEYS = ("cleanarr",)
# Tracks it named by default before the rename.
OLD_DEFAULT_TITLES = ("Cleaned - English",)


def set_clean_title(title: str, also_known=()) -> None:
    """Take the track name from the settings, and the names it used to have.

    Detection matches all of them; only the current one is ever written.
    """
    global CLEAN_TITLE, KNOWN_TITLES
    CLEAN_TITLE = (title or "").strip() or DEFAULT_CLEAN_TITLE
    KNOWN_TITLES = {CLEAN_TITLE.lower(), DEFAULT_CLEAN_TITLE.lower()}
    KNOWN_TITLES |= {t.lower() for t in OLD_DEFAULT_TITLES}
    KNOWN_TITLES |= {t.strip().lower() for t in (also_known or ()) if t and t.strip()}


# How much a remux is allowed to differ from the original before it is thrown
# away: containers round durations, but a truncated file is a lost episode.
DURATION_TOLERANCE = 1.0


class MediaError(RuntimeError):
    pass


@dataclass
class AudioStream:
    index: int          # index within the file
    audio_index: int    # index among audio streams only (the a:N form)
    codec: str
    channels: int
    sample_rate: int
    language: str
    title: str
    default: bool
    handler: str = ""
    mark: str = ""
    # When the track's first sample plays, in seconds - see speech_offset.
    start: float = 0.0

    @property
    def is_cleaned(self) -> bool:
        """Is this our track?

        The mark settles it where the container kept one. Otherwise the name,
        read from two fields: MP4 has no per-track title - ffmpeg accepts
        `-metadata:s:a:N title=` on an MP4 and drops it at mux time - but it
        does keep `handler_name`, so the name is written to both and read from
        either. Untouched tracks carry handler_name "SoundHandler", so matching
        on it cannot claim a track we did not make.

        Matched against every name this install has used, not only the current
        one: a file cleaned under an older name is still ours.
        """
        if self.mark:
            return True
        names = {self.title.strip().lower(), self.handler.strip().lower()}
        return bool(names & KNOWN_TITLES)

    @property
    def written_here(self) -> bool:
        """Ours beyond doubt, rather than by name.

        is_cleaned also matches on the title alone, which a track the file came
        with can share - "English" is both a plausible name for the new track
        and a common name for an existing one. That is harmless for "do not
        listen to this one" and not harmless for "delete this one", so the
        steps that drop a track ask this instead.
        """
        if self.mark:
            return True
        handler = self.handler.strip().lower()
        return bool(handler) and handler in KNOWN_TITLES


# Subtitle formats that are text, and so can be read. Picture-based ones -
# Blu-ray PGS, DVD VobSub - would need OCR, and are skipped.
TEXT_SUBTITLES = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}


@dataclass
class SubtitleStream:
    sub_index: int      # index among subtitle streams only (the s:N form)
    codec: str
    language: str
    title: str
    forced: bool
    handler: str = ""
    mark: str = ""

    @property
    def written_here(self) -> bool:
        """The censored subtitles this install added - by the mark, or by the
        handler name only this writes (MP4 keeps no other tag)."""
        if self.mark:
            return True
        handler = self.handler.strip().lower()
        return bool(handler) and handler in KNOWN_TITLES

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_SUBTITLES

    @property
    def is_forced(self) -> bool:
        """Forced by flag, or by name - plenty of files only say so in the title."""
        return self.forced or "forced" in self.title.lower()


@dataclass
class Probe:
    path: Path
    duration: float
    video_streams: int
    audio: list[AudioStream]
    container: str
    subtitles: list[SubtitleStream] = field(default_factory=list)
    # The file's own start time; ffmpeg counts every stream's time from here.
    start: float = 0.0

    @property
    def cleaned_track(self) -> AudioStream | None:
        return next((a for a in self.audio if a.is_cleaned), None)

    @property
    def our_subtitles(self) -> tuple[int, ...]:
        """The censored subtitle tracks this install added (s:N indexes)."""
        return tuple(s.sub_index for s in self.subtitles if s.written_here)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe(path: str | Path) -> Probe:
    path = Path(path)
    out = _run([FFPROBE, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path)])
    if out.returncode != 0:
        raise MediaError(f"ffprobe failed on {path.name}: {out.stderr.strip()[:300]}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    audio: list[AudioStream] = []
    subtitles: list[SubtitleStream] = []
    ai = 0
    for s in streams:
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        if s.get("codec_type") == "subtitle":
            subtitles.append(SubtitleStream(
                sub_index=len(subtitles),
                codec=str(s.get("codec_name", "")),
                language=str(tags.get("language", "")),
                title=str(tags.get("title", "")),
                forced=bool((s.get("disposition") or {}).get("forced")),
                handler=str(tags.get("handler_name", "")),
                mark=str(tags.get(MARK_KEY) or ""),
            ))
            continue
        if s.get("codec_type") != "audio":
            continue
        audio.append(AudioStream(
            index=int(s.get("index", 0)),
            audio_index=ai,
            codec=str(s.get("codec_name", "")),
            channels=int(s.get("channels", 2) or 2),
            sample_rate=int(s.get("sample_rate", 48000) or 48000),
            language=str(tags.get("language", "")),
            title=str(tags.get("title", "")),
            default=bool((s.get("disposition") or {}).get("default")),
            handler=str(tags.get("handler_name", "")),
            mark=str(tags.get(MARK_KEY) or next(
                (tags[k] for k in OLD_MARK_KEYS if tags.get(k)), "")),
            start=_seconds(s.get("start_time")),
        ))
        ai += 1
    return Probe(
        path=path,
        duration=float((data.get("format") or {}).get("duration") or 0.0),
        video_streams=sum(1 for s in streams if s.get("codec_type") == "video"),
        audio=audio,
        container=path.suffix.lower().lstrip("."),
        subtitles=subtitles,
        start=_seconds((data.get("format") or {}).get("start_time")),
    )


def _seconds(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0         # "N/A", or not reported


def speech_offset(p: Probe, track: AudioStream) -> float:
    """Seconds to add to a time in the extracted audio to get the file's time.

    The WAV Whisper hears starts at the track's first sample. The mute filter,
    the subtitles and every player count from the start of the file. They are
    the same only when the audio starts with the picture: a recording whose
    audio starts 2 s in (TV captures often do) had every mute land 2 s early,
    missing the word, and every subtitle look 2 s out.
    """
    return round(track.start - p.start, 3)


def pick_source_track(p: Probe, prefer_language: str = "eng") -> AudioStream:
    """The track to listen to and to base the clean one on.

    Preference order: the default English track, any English track, the
    default track, the first track. A track we have cleaned already is never
    used as a source - cleaning a cleaned track would compound the muting and
    drift further from the original every run.
    """
    usable = [a for a in p.audio if not a.is_cleaned]
    if not usable:
        if p.audio:
            # The file has audio, and all of it matches the configured name.
            raise MediaError(
                f"every audio track in this file already looks like ours - the "
                f"track name “{CLEAN_TITLE}” matches a track the file came "
                f"with, so pick a different name")
        raise MediaError("no usable audio track in this file")

    def english(a: AudioStream) -> bool:
        return a.language.lower().startswith(prefer_language[:2]) or a.language == ""

    for test in (lambda a: english(a) and a.default,
                 lambda a: a.language.lower().startswith(prefer_language[:2]),
                 lambda a: a.default):
        hit = next((a for a in usable if test(a)), None)
        if hit:
            return hit
    return usable[0]


def pick_subtitles(p: Probe, prefer_language: str = "eng") -> SubtitleStream | None:
    """The English text subtitles to check the transcript against, if any.

    Forced subtitles only cover the odd line of foreign speech, so they would
    make every swear look unconfirmed. A track tagged English wins over an
    untagged one.
    """
    def usable(s: SubtitleStream) -> bool:
        lang = s.language.lower()
        # Commentary is somebody else talking; signs and songs, like forced
        # subtitles, cover only a few lines.
        extra = any(w in s.title.lower() for w in ("commentary", "signs", "songs"))
        return (s.is_text and not s.is_forced and not extra and not s.written_here
                and s.title.strip().lower() not in KNOWN_TITLES
                and (lang.startswith(prefer_language[:2]) or lang in ("", "und")))

    tagged = [s for s in p.subtitles if usable(s) and s.language]
    untagged = [s for s in p.subtitles if usable(s) and not s.language]
    return (tagged or untagged or [None])[0]


_SIDECAR_SUFFIXES = (".srt", ".vtt")
_ENGLISH_TAGS = {"en", "eng", "english"}
# Tags a sidecar name carries that say nothing about its language.
_NEUTRAL_TAGS = {"sdh", "hi", "cc", "default", "full"}


def find_sidecar_subtitles(path: Path) -> Path | None:
    """An English text subtitle file beside the media, as Bazarr and most
    downloaders name them: Episode.srt, Episode.en.srt, Episode.eng.sdh.srt.

    Forced files are skipped for the same reason as forced tracks. A file
    tagged English wins over an untagged one; one tagged another language is
    never used.
    """
    try:
        siblings = sorted(path.parent.iterdir())
    except OSError:
        return None
    tagged, untagged = [], []
    for f in siblings:
        name = f.name
        if (f.suffix.lower() not in _SIDECAR_SUFFIXES or not name.startswith(path.stem + ".")
                or not f.is_file()):
            continue
        tags = {t.lower() for t in name[len(path.stem) + 1:-len(f.suffix)].split(".") if t}
        if "forced" in tags:
            continue
        if tags & _ENGLISH_TAGS:
            tagged.append(f)
        elif not tags - _NEUTRAL_TAGS:
            untagged.append(f)
    return (tagged or untagged or [None])[0]


def cut_audio(wav: Path, start: float, length: float, dest: Path) -> Path:
    """A few seconds of the extracted speech, for a second listen."""
    out = _run([FFMPEG, "-nostdin", "-v", "error", "-y", "-ss", f"{max(0.0, start):.3f}",
                "-t", f"{length:.3f}", "-i", str(wav), "-c:a", "pcm_s16le", str(dest)])
    if out.returncode != 0 or not dest.exists():
        raise MediaError(f"could not cut the audio: {out.stderr.strip()[:200]}")
    return dest


def extract_subtitles(path: Path, stream: SubtitleStream, dest: Path) -> bool:
    """One text subtitle track as SRT. For another copy of a film, whose
    subtitles are wanted but whose audio is not - so it is its own pass."""
    out = _run([FFMPEG, "-nostdin", "-v", "error", "-y", *_threads(), "-i", str(path),
                "-map", f"0:s:{stream.sub_index}", "-c:s", "srt", str(dest)])
    return out.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def extract_for_asr(path: Path, track: AudioStream, dest: Path,
                    subtitles: SubtitleStream | None = None,
                    subtitle_dest: Path | None = None) -> Path:
    """16 kHz mono WAV, which is what Whisper wants and nothing else does.

    The subtitles, when asked for, come out of the same pass as SRT: reading a
    large file off a network share twice to get a few kilobytes of text is not
    worth it. If they cannot be converted, the audio is extracted on its own.
    """
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", *_threads(), "-i", str(path),
           "-map", f"0:a:{track.audio_index}", "-vn", "-sn", "-dn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)]
    if subtitles is not None and subtitle_dest is not None:
        out = _run([*cmd, "-map", f"0:s:{subtitles.sub_index}", "-c:s", "srt",
                    str(subtitle_dest)])
        if out.returncode == 0 and dest.exists():
            return dest
        if subtitle_dest.exists():
            subtitle_dest.unlink()
    out = _run(cmd)
    if out.returncode != 0 or not dest.exists():
        raise MediaError(f"could not extract audio: {out.stderr.strip()[:300]}")
    return dest


def _encoder_for(container: str, channels: int, source_codec: str = "",
                 surround_bitrate: str = "384k",
                 stereo_bitrate: str = "192k") -> tuple[list[str], str]:
    """Codec for the cleaned track, chosen so Plex can direct play it.

    Matching the original's codec is the safest choice available: whatever the
    client already plays for this file, it will play for the clean track too.
    Re-encoding is unavoidable - the audio genuinely changed - so this only
    decides the format and how much the file grows. AC-3 at 640k added 150MB
    to a 200MB episode; E-AC-3 at 384k adds about a third of that and is what
    the file was already using.
    """
    codec = (source_codec or "").lower()
    if channels > 2:
        if codec in ("eac3", "ac3"):
            return (["-c:a", codec, "-b:a", surround_bitrate], codec)
        if container in ("mkv", "mka"):
            return (["-c:a", "eac3", "-b:a", surround_bitrate], "eac3")
        return (["-c:a", "aac", "-b:a", surround_bitrate], "aac")
    return (["-c:a", "aac", "-b:a", stereo_bitrate], "aac")


def _run_with_progress(cmd: list[str], progress=None) -> None:
    """Run ffmpeg, reporting 0-1 through `progress` as it goes."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    total_us = None
    stderr_tail: list[str] = []
    try:
        for line in proc.stdout or []:
            line = line.strip()
            if line.startswith("out_time_us=") and progress and total_us:
                try:
                    progress(min(1.0, int(line.split("=")[1]) / total_us))
                except (ValueError, ZeroDivisionError):
                    pass
    finally:
        proc.wait()
        if proc.stderr:
            stderr_tail = proc.stderr.read().strip().splitlines()[-5:]
    if proc.returncode != 0:
        raise MediaError("ffmpeg failed: " + " / ".join(stderr_tail)[:400])


def build_cleaned_file(original: Probe, track: AudioStream,
                       spans: list[tuple[float, float]], dest: Path,
                       title: str = "", language: str = "eng",
                       fade: float = 0.02, drop_audio: tuple[int, ...] = (),
                       surround_bitrate: str = "384k", stereo_bitrate: str = "192k",
                       subtitles: Path | None = None, drop_subtitles: tuple[int, ...] = (),
                       progress=None) -> Path:
    """Silence the words and write the finished file in one pass.

    Doing it in two - render the muted track to its own file, then remux -
    reads the source twice and writes an extra copy of the audio. For a 1GB
    episode that was about a third of the time for no benefit: the filter and
    the encoder settings are identical either way.
    """
    args, _codec = _encoder_for(original.container, track.channels, track.codec,
                                surround_bitrate, stereo_bitrate)
    title = title or CLEAN_TITLE
    new_index = len(original.audio) - len(drop_audio)
    sub_codec = SUBTITLE_CODECS.get(original.container) if subtitles else None

    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", *_threads(),
           "-i", str(original.path)]
    if sub_codec:
        cmd += ["-i", str(subtitles)]
    cmd += ["-filter_complex_script", str(_filter_script(spans, track, fade)),
            "-map", "0"]
    for index in drop_audio:
        cmd += ["-map", f"-0:a:{index}"]
    for index in drop_subtitles:
        cmd += ["-map", f"-0:s:{index}"]
    cmd += ["-map", "[clean]"]
    if sub_codec:
        new_sub = len(original.subtitles) - len(drop_subtitles)
        cmd += ["-map", "1:s:0"]
    cmd += ["-c", "copy",
            # Only the new stream is encoded; everything else is copied.
            f"-c:a:{new_index}", args[1], f"-b:a:{new_index}", args[3],
            f"-metadata:s:a:{new_index}", f"title={title}",
            # MP4 drops the title and keeps this one. Harmless on Matroska.
            f"-metadata:s:a:{new_index}", f"handler_name={title}",
            f"-metadata:s:a:{new_index}", f"language={language}",
            # Kept by Matroska, ignored by MP4: the track stays ours even if
            # the name is changed later.
            f"-metadata:s:a:{new_index}", f"{MARK_KEY}={MARK_VALUE}",
            f"-disposition:a:{new_index}", "0"]
    if sub_codec:
        # The same name and marks as the audio, so a player lists the pair
        # together and removal can tell it was written here.
        cmd += [f"-c:s:{new_sub}", sub_codec,
                f"-metadata:s:s:{new_sub}", f"title={title}",
                f"-metadata:s:s:{new_sub}", f"handler_name={title}",
                f"-metadata:s:s:{new_sub}", f"language={language}",
                f"-metadata:s:s:{new_sub}", f"{MARK_KEY}={MARK_VALUE}",
                f"-disposition:s:{new_sub}", "0"]
    cmd += [
            # Interleave strictly. Left to itself ffmpeg gives up on ordering
            # after max_interleave_delta (10s) when a file has sparse streams -
            # one episode with 38 subtitle tracks ended up with its cleaned
            # audio for the 30-minute mark written 470MB from the matching
            # picture, which plays fine from a local disk and stalls forever
            # over a share.
            #
            # This is a MUXER option, so it belongs here, after the inputs.
            # Written before -i it is read as an input option and silently does
            # nothing - which is exactly what happened on the first attempt,
            # and is why the verify step measures the result rather than
            # trusting the flag.
            "-max_interleave_delta", "0",
            "-progress", "pipe:1", "-nostats", str(dest)]
    script = cmd[cmd.index("-filter_complex_script") + 1]
    try:
        _run_with_progress(cmd, progress)
    finally:
        Path(script).unlink(missing_ok=True)
    return dest


# What a censored subtitle track is written as, per container. Others (AVI,
# TS) get the audio track only.
SUBTITLE_CODECS = {"mkv": "srt", "mka": "srt", "mp4": "mov_text", "m4v": "mov_text",
                   "mov": "mov_text"}


def adds_subtitles(container: str) -> bool:
    return container in SUBTITLE_CODECS


def _filter_script(spans: list[tuple[float, float]], track: AudioStream,
                   fade: float) -> Path:
    """The volume curve, written to a file because a long episode carries
    hundreds of spans and the expression outgrows a command line."""
    if spans:
        terms = [
            f"min(1,max(0,(({start:.3f}-t)/{fade:.3f})))"
            f"+min(1,max(0,((t-{end:.3f})/{fade:.3f})))"
            for start, end in spans
        ]
        expr = "*".join(f"min(1,({t}))" for t in terms)
        chain = f"volume=volume='min(1,max(0,{expr}))':eval=frame"
    else:
        chain = "anull"
    text = f"[0:a:{track.audio_index}]{chain}[clean]"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf8") as fh:
        fh.write(text)
        return Path(fh.name)


def track_bytes(path: Path, audio_index: int) -> int:
    """How much disk one audio track actually occupies.

    Measured by adding its packets up, which takes a few seconds and is right
    in every case. Comparing file sizes before and after is not: re-cleaning a
    file replaces one cleaned track with another, so the difference is zero
    while the track still costs 150MB.
    """
    out = _run([FFPROBE, "-v", "error", "-select_streams", f"a:{audio_index}",
                "-show_entries", "packet=size", "-of", "csv=p=0", str(path)])
    total = 0
    for line in (out.stdout or "").splitlines():
        value = line.rstrip(",").strip()
        if value.isdigit():
            total += int(value)
    return total


def packet_offset(path: Path, stream: str, at: float) -> int | None:
    """Where in the file the first packet of `stream` at time `at` lives."""
    out = _run([FFPROBE, "-v", "error", "-select_streams", stream,
                "-show_entries", "packet=pos", "-of", "csv=p=0",
                "-read_intervals", f"{at}%+#1", str(path)])
    first = (out.stdout or "").strip().splitlines()
    if not first:
        return None
    value = first[0].rstrip(",")
    return int(value) if value.isdigit() else None


# How far the cleaned track may sit from the reference track at the same
# moment. A correctly interleaved file keeps them within a few kilobytes; the
# episode that started this check was 485MB out.
INTERLEAVE_TOLERANCE = 20_000_000


def check_interleave(path: Path, cleaned_index: int, duration: float,
                     reference: int = 0) -> None:
    """Refuse a file whose new track is written far from its own picture.

    This is the one fault that a stream-by-stream inspection misses entirely:
    every track is present, correct and decodes perfectly, and the file still
    stalls forever over a network share because the player has to seek half a
    gigabyte for each second of sound. So it is measured directly, at several
    points, before the file is allowed anywhere near the library.
    """
    if duration <= 0:
        return
    points = [t for t in (duration * 0.1, duration * 0.5, duration * 0.9) if t > 1]
    for at in points:
        here = packet_offset(path, f"a:{cleaned_index}", at)
        there = packet_offset(path, f"a:{reference}", at)
        if here is None or there is None:
            continue
        gap = abs(here - there)
        if gap > INTERLEAVE_TOLERANCE:
            raise MediaError(
                f"the censored track is badly interleaved - at {at:.0f}s it sits "
                f"{gap / 1e6:.0f}MB away from the other audio, which stalls "
                f"playback over a share")


def verify_replacement(original: Probe, candidate: Path,
                       expected_audio: int | None = None,
                       expect_cleaned: bool = True,
                       expected_subtitles: int | None = None) -> Probe:
    """Refuse a file that lost anything on the way through.

    `expect_cleaned` is False for a removal, where the new file is meant to
    have no track of ours.
    """
    got = probe(candidate)
    expected = len(original.audio) + 1 if expected_audio is None else expected_audio
    if got.video_streams != original.video_streams:
        raise MediaError(
            f"the new file has {got.video_streams} video streams, "
            f"the original had {original.video_streams}")
    if len(got.audio) != expected:
        raise MediaError(
            f"the new file has {len(got.audio)} audio tracks, expected {expected}")
    if expected_subtitles is not None and len(got.subtitles) != expected_subtitles:
        raise MediaError(
            f"the new file has {len(got.subtitles)} subtitle tracks, "
            f"expected {expected_subtitles}")
    if expect_cleaned and got.cleaned_track is None:
        raise MediaError(
            f"the new file has no track named “{CLEAN_TITLE}”")
    if not expect_cleaned and any(a.written_here for a in got.audio):
        raise MediaError("the new file still has a track this install wrote")
    if original.duration and abs(got.duration - original.duration) > DURATION_TOLERANCE:
        raise MediaError(
            f"the new file is {got.duration:.1f}s long, the original was "
            f"{original.duration:.1f}s")
    cleaned = got.cleaned_track if expect_cleaned else None
    if cleaned is not None and cleaned.audio_index != 0:
        check_interleave(candidate, cleaned.audio_index, got.duration)
    return got


def remove_cleaned_track(original: Probe, dest: Path, drop=None,
                         progress=None, drop_subtitles: tuple[int, ...] = ()) -> int:
    """Write the file back without its cleaned track. Returns how many it dropped.

    `drop` names the audio streams to leave out; by default every track that
    looks like ours. The pipeline passes the narrower list - see written_here.
    """
    drop = list(drop) if drop is not None else [
        a.audio_index for a in original.audio if a.is_cleaned]
    if not drop:
        return 0
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", *_threads(),
           "-i", str(original.path), "-map", "0"]
    for index in drop:
        cmd += ["-map", f"-0:a:{index}"]
    for index in drop_subtitles:
        cmd += ["-map", f"-0:s:{index}"]
    # After the inputs: -max_interleave_delta is a muxer option (see
    # build_cleaned_file).
    cmd += ["-c", "copy", "-max_interleave_delta", "0",
            "-progress", "pipe:1", "-nostats", str(dest)]
    _run_with_progress(cmd, progress)
    return len(drop)


def swap_in(new_file: Path, original: Path, keep_backup: bool = False) -> None:
    """Put the new file where the old one was, keeping how it looked to others.

    Owner, permissions and modification time are carried over: Plex treats
    mtime as when the episode arrived, and a library that suddenly thinks
    every cleaned episode is new would reorder itself on every run.
    """
    st = original.stat()
    backup = original.with_suffix(original.suffix + ".censarr-backup")
    if keep_backup:
        shutil.copy2(original, backup)
    os.replace(new_file, original)
    try:
        os.chown(original, st.st_uid, st.st_gid)
    except (AttributeError, PermissionError, OSError):
        pass  # Windows, or a share that does not allow it
    try:
        os.chmod(original, st.st_mode)
        os.utime(original, (st.st_atime, st.st_mtime))
    except OSError:
        pass


def free_space(path: Path) -> int:
    return shutil.disk_usage(path.parent if path.is_file() else path).free
