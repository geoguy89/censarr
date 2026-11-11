"""One file, start to finish.

    probe -> extract -> listen -> match -> mute -> add track -> verify -> swap

Every step writes its progress to the job row, because the only thing worse
than a slow job is a slow job that looks stuck. Nothing touches the original
file until the last step, and that step refuses to run on a file that did not
verify.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from . import arr, asr, config, db, judge, library, media, subtitles, words

# What each stage is worth, so the bar moves smoothly across the whole job
# rather than sitting at 0% through the part that takes the longest.
# Shares of the bar, roughly proportional to how long each stage really takes.
# Judging used to get two percent of the bar while taking most of the minutes,
# so a job in that stage looked frozen - which is exactly how it was reported.
STAGES = {
    "probing": (0.00, 0.02),
    "extracting": (0.02, 0.05),
    "listening": (0.05, 0.45),
    "matching": (0.45, 0.46),
    "judging": (0.46, 0.88),
    # Muting and remuxing are one ffmpeg pass now; "remuxing" is still its own
    # stage for a removal, which has nothing to mute.
    "muting": (0.88, 0.97),
    "remuxing": (0.88, 0.97),
    "verifying": (0.97, 1.00),
}


def _missing(path: Path) -> str:
    """Why a file could not be opened, in the two cases that mean it.

    A file that was cleaned last week and has since been deleted is a
    different problem from a library folder that was never mounted here, and
    "is not there any more" is wrong about the second - which is the one a new
    install hits on every single job.
    """
    folder = path.parent
    if not folder.is_dir():
        return (f"{path} not found from this container - it cannot see "
                f"{folder}. Check Settings → Your library.")
    return f"{path} is not there any more"


class Cancelled(Exception):
    pass


class Pipeline:
    def __init__(self, settings: config.Settings, cache_dir: Path,
                 should_cancel=None, item: dict | None = None):
        self.settings = settings
        self.cache_dir = cache_dir
        self.should_cancel = should_cancel or (lambda: False)
        # kind, source, source_id and title of the job: how the media server
        # is told which item's file changed (see arr.refresh_for).
        self.item = item or {}
        self.job_id: int | None = None

    # -- progress plumbing ------------------------------------------------
    def _stage(self, job_id: int, stage: str, message: str = "") -> None:
        if self.should_cancel():
            raise Cancelled()
        self.job_id = job_id
        low, _high = STAGES[stage]
        db.update(job_id, stage=stage, progress=low, message=message)

    def _progress(self, job_id: int, stage: str):
        low, high = STAGES[stage]
        last = [0.0]

        def report(fraction: float) -> None:
            if self.should_cancel():
                raise Cancelled()
            value = low + (high - low) * max(0.0, min(1.0, fraction))
            if value - last[0] >= 0.005:      # don't write to sqlite every frame
                last[0] = value
                db.update(job_id, progress=round(value, 4))
        return report

    # -- second opinion ---------------------------------------------------
    def _adjudicate(self, matches: list, transcript_words: list[dict],
                    settings: config.Settings) -> tuple[list, list]:
        """Split matches into (mute, leave alone), asking Ollama about the
        ambiguous ones. Anything it cannot answer stays muted."""
        if not settings.judge_url:
            return matches, []

        # Which words are worth asking about is the household's call, not the
        # code's: the setting is the whole list, and an empty one means nothing
        # is asked. A word taken off the list is simply muted.
        checked = {w.strip().lower() for w in settings.check_in_context if w.strip()}
        ambiguous = [(i, m) for i, m in enumerate(matches)
                     if judge.is_ambiguous(m.text, checked)]
        if not ambiguous:
            return matches, []

        # The same word said repeatedly in one scene is one decision, not
        # twelve. Grouping pools the context - the drywall scene that produced
        # nine "cock" mutes only reads as caulk once you see several of them
        # together - and keeps the answers consistent across the scene.
        groups = judge.group(ambiguous, transcript_words)
        lines = [{"n": g.n, "line": g.line} for g in groups]

        # Give the speech model's VRAM back before asking Ollama anything. On
        # an 8GB card those two gigabytes are the difference between the judge
        # running on the GPU and Ollama quietly putting half its layers on the
        # CPU: measured, that was 230 seconds with eight cores pegged against
        # 25 seconds with the card to itself. Reloading Whisper for the next
        # job costs a few seconds.
        asr.unload_model()

        low, high = STAGES["judging"]

        def say(done: int, total: int, word: str) -> None:
            if self.should_cancel():
                raise Cancelled()
            db.update(self.job_id,
                      progress=round(low + (high - low) * (done / max(total, 1)), 4),
                      message=f"second opinion {done + 1} of {total}: is “{word}” "
                              f"profanity here? (each takes 30-100s)")

        verdicts = judge.adjudicate(
            lines, settings.judge_url, settings.judge_model,
            threads=settings.judge_threads, keep_alive=settings.judge_keep_alive,
            progress=say)

        mute, leave = [], []
        lookup = {g.n: g.indexes for g in groups}
        cleared = {i: instead for n, (verdict, instead) in verdicts.items()
                   if verdict == "CLEAN" for i in lookup.get(n, ())}
        asked = {i for g in groups for i in g.indexes}
        for i, match in enumerate(matches):
            if i in cleared:
                heard = cleared[i]
                leave.append(replace(
                    match, needs_review=True,
                    reason=("left in: reverent, not an exclamation"
                            if heard.lower().startswith("reverent")
                            else f"left in: heard as “{heard}” here")))
            else:
                if i in asked and verdicts:
                    match = replace(
                        match, needs_review=True,
                        reason=match.reason or "ambiguous word, judged profanity here")
                mute.append(match)
        return mute, leave

    # -- a second opinion on a sound-alike ---------------------------------
    def _second_listen(self, alike: list, wav: Path, transcript_words: list[dict],
                       settings: config.Settings) -> tuple[list, list]:
        """(muted, left in) for the words the subtitles would leave in because
        their word only sounds like the swear.

        With a second Whisper server, that moment is heard again; with the
        model, it is asked with the subtitle line in front of it. Either can
        overrule the subtitles and mute. Without one - or when it cannot
        answer - the subtitles' verdict stands, and the word is left in,
        marked worth a listen.
        """
        if not alike:
            return [], []
        if settings.recheck_url:
            return self._listen_again(alike, wav, settings)
        if settings.judge_url:
            return self._ask_about(alike, transcript_words, settings)
        return [], [replace(m, needs_review=True) for m in alike]

    def _listen_again(self, alike: list, wav: Path, settings) -> tuple[list, list]:
        muted, kept = [], []
        for n, m in enumerate(alike):
            if self.should_cancel():
                raise Cancelled()
            db.update(self.job_id, message=f"second listen {n + 1} of {len(alike)}: "
                                           f"“{m.text}” or “{m.subtitle[:40]}”?")
            try:
                clip = media.cut_audio(wav, m.start - RELISTEN_PAD,
                                       (m.end - m.start) + 2 * RELISTEN_PAD,
                                       wav.parent / f"again-{n}.wav")
                again = asr.transcribe_remote(clip, settings.recheck_url, settings.recheck_model,
                                              settings.recheck_api_key, timeout=120.0)
            except Exception as exc:  # noqa: BLE001
                print(f"[censarr] second listen failed: {exc}", flush=True)
                kept.append(replace(m, needs_review=True))
                continue
            said = " ".join(str(w.get("word", "")).strip() for w in again.words)
            word = words.normalize(m.text)
            if any(words.normalize(w.get("word", "")).startswith(word[:4])
                   for w in again.words if len(word) >= 3):
                muted.append(replace(m, needs_review=True,
                                     reason=f"a second listen heard “{m.text}” again: “{said}”"))
            else:
                kept.append(replace(m, needs_review=True,
                                    reason=f"{m.reason}; a second listen heard “{said}”"))
        return muted, kept

    def _ask_about(self, alike: list, transcript_words: list[dict],
                   settings) -> tuple[list, list]:
        groups = judge.group(list(enumerate(alike)), transcript_words)
        items = []
        for g in groups:
            shown = alike[g.indexes[0]].subtitle
            items.append({"n": g.n, "line": g.line
                          + f'\n   (the subtitles show: "{shown[:160]}")'})
        asr.unload_model()
        try:
            verdicts = judge.adjudicate(items, settings.judge_url, settings.judge_model,
                                        threads=settings.judge_threads,
                                        keep_alive=settings.judge_keep_alive)
        except Exception as exc:  # noqa: BLE001
            print(f"[censarr] second opinion failed: {exc}", flush=True)
            verdicts = {}
        muted, kept = [], []
        profane = {i for g in groups if verdicts.get(g.n, ("",))[0] == "PROFANE"
                   for i in g.indexes}
        for i, m in enumerate(alike):
            if i in profane:
                muted.append(replace(m, needs_review=True,
                                     reason="the second opinion heard profanity, not the subtitles' word"))
            else:
                kept.append(replace(m, needs_review=True))
        return muted, kept

    # -- whose track is that? ---------------------------------------------
    def _ours(self, original, path: Path, job_id: int) -> tuple[int, ...]:
        """The audio streams this service can show it wrote.

        Dropping a track is the only destructive thing here, so it is not done
        on a name match alone: a track carries the mark, or the handler name
        only this writes, or there is a finished job for this file. A track
        that merely shares the configured name belongs to the file.
        """
        seen = db.cleaned_before(str(path), job_id)
        return tuple(a.audio_index for a in original.audio
                     if a.is_cleaned and (a.written_here or seen))

    # -- taking it back out -----------------------------------------------
    def remove(self, job_id: int, path: Path) -> dict:
        """Write the file back without its cleaned track, freeing the space."""
        settings = self.settings
        if not path.exists():
            raise FileNotFoundError(_missing(path))

        self._stage(job_id, "probing", "reading the file")
        original = media.probe(path)
        if original.cleaned_track is None:
            db.forget_cleaned(str(path))
            return {"status": "skipped", "message": "there was no censored track to remove"}
        drop = self._ours(original, path, job_id)
        if not drop:
            raise RuntimeError(
                f"the track named “{settings.track_title}” in this file is not "
                f"one this install wrote, so it is left alone - remove it with "
                f"the tool that made it")

        before = path.stat().st_size
        work = Path(tempfile.mkdtemp(prefix="censarr-", dir=str(path.parent)))
        try:
            self._stage(job_id, "remuxing", "removing the censored track")
            candidate = work / f"remux{path.suffix}"
            dropped = media.remove_cleaned_track(
                original, candidate, drop=drop,
                progress=self._progress(job_id, "remuxing"))

            self._stage(job_id, "verifying", "checking the new file")
            media.verify_replacement(
                original, candidate, expected_audio=len(original.audio) - dropped,
                expect_cleaned=False)
            media.swap_in(candidate, path, keep_backup=settings.keep_backup)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        freed = max(0, before - path.stat().st_size)
        db.forget_cleaned(str(path))
        note = arr.refresh_for(settings, str(path), **self.item)
        return {"status": "done", "muted": 0, "added_bytes": 0,
                "message": f"removed the censored track · {freed / 1e6:.0f} MB back · "
                           f"{note}"}

    # -- the work ---------------------------------------------------------
    def run(self, job_id: int, path: Path, force: bool = False, title: str = "",
            more_subtitles=None) -> dict:
        settings = self.settings
        if not path.exists():
            raise FileNotFoundError(_missing(path))

        self._stage(job_id, "probing", "reading the file")
        original = media.probe(path)
        looks_ours = [a for a in original.audio if a.is_cleaned]
        existing = self._ours(original, path, job_id)
        if looks_ours and not existing:
            # Every track that matches the name is one we cannot claim, so
            # cleaning would either add a confusing duplicate name or drop
            # somebody else's audio.
            raise RuntimeError(
                f"this file already has an audio track named "
                f"“{settings.track_title}”, and nothing in the file says "
                f"Cens-arr wrote it - pick a different name for the new track "
                f"in Settings, so an existing track is never replaced by mistake")
        if existing and not force:
            return {"status": "skipped",
                    "message": f"already has a “{settings.track_title}” track"}
        source = media.pick_source_track(original)
        db.update(job_id, duration=original.duration)

        # A remux needs room for a second copy of the file while it is built.
        needed = path.stat().st_size + 512 * 1024 * 1024
        if media.free_space(path) < needed:
            raise RuntimeError(
                f"not enough free space beside {path.name}: needs about "
                f"{needed / 1e9:.1f} GB")

        _sweep_stale_work(path.parent)
        work = Path(tempfile.mkdtemp(prefix="censarr-", dir=str(path.parent)))
        try:
            self._stage(job_id, "extracting", "pulling out the audio")
            subtitle_file = work / "subtitles.srt"
            wav = media.extract_for_asr(path, source, work / "asr.wav",
                                        subtitles=media.pick_subtitles(original),
                                        subtitle_dest=subtitle_file)

            self._stage(job_id, "listening", "listening for profanity")
            transcript = asr.transcribe(
                wav, model_name=settings.model, device=settings.device,
                compute_type=settings.compute_type,
                trim_silence=settings.trim_silence,
                # If the card is full when we get there, this is how we clear
                # it - Ollama's lease outlives any wait we could sit through.
                free_vram=lambda: judge.release_vram(settings.judge_url),
                # Set, and the listening happens on somebody else's Whisper
                # server instead - no model here, no GPU here.
                remote_url=(settings.asr_url if settings.asr_backend == "remote" else ""),
                remote_model=settings.asr_remote_model,
                remote_key=settings.asr_api_key,
                cache_dir=self.cache_dir,
                download_root=str(self.cache_dir / "models"),
                progress=self._progress(job_id, "listening"))

            # Whisper's times count from the track's first sample; everything
            # after this - the mutes, the subtitles, the times shown - counts
            # from the start of the file. The cached transcript keeps its own.
            offset = media.speech_offset(original, source)
            if abs(offset) >= 0.005:
                transcript = replace(transcript, words=[
                    {**w, "start": float(w.get("start") or 0.0) + offset,
                     "end": float(w.get("end") or 0.0) + offset}
                    for w in transcript.words])

            self._stage(job_id, "matching", "checking the word list")
            context_words = {w.strip().lower() for w in settings.check_in_context
                             if w.strip()}
            # This show's own exceptions join the household's.
            here = (settings.allow_words_by_title or {}).get(title, [])
            matcher = words.Matcher(
                categories=tuple(settings.categories),
                never=frozenset(words.NEVER) | {w.lower() for w in settings.allow_words}
                | {w.lower() for w in here},
                extra=tuple(settings.custom_words),
                context_words=frozenset(context_words))
            matches = matcher.find(transcript.words)

            self._stage(job_id, "judging", "checking the ambiguous ones")
            # What the subtitles say at each word: evidence for whoever reviews
            # the job, and - only with the subtitle second opinion on - the
            # deciding word on the uncertain ones. The file's own track first,
            # else a subtitle file beside it.
            cues, found_where = [], ""
            source_file = (subtitle_file if subtitle_file.exists()
                           else media.find_sidecar_subtitles(path))
            if source_file is not None:
                cues = subtitles.parse_srt(source_file.read_text(encoding="utf8",
                                                                 errors="replace"))
            # None of its own: the media server's, another copy's, or a search.
            search_note = ""
            if not cues and more_subtitles is not None:
                self._stage(job_id, "judging", "looking for subtitles elsewhere")
                text, found_where, search_note = more_subtitles(work)
                cues = subtitles.parse_srt(text) if text else []
                if text:
                    # Kept for review: the file has none of its own, so this is
                    # the only copy of what its words were checked against.
                    kept_dir = self.cache_dir / "subtitles"
                    kept_dir.mkdir(parents=True, exist_ok=True)
                    (kept_dir / f"job-{job_id}.srt").write_text(text, encoding="utf8")
            # Any of them may be timed for another cut - borrowed ones often,
            # a .srt downloaded for the file now and then, the file's own
            # track rarely. align() leaves subtitles that already fit alone.
            moved = ""
            if cues:
                cues, low, high = subtitles.align(cues, transcript.words)
                if low or high:
                    low, high = sorted((abs(low), abs(high)))
                    moved = (f"moved {high:g} s to line up" if low == high
                             else f"moved {low:g}-{high:g} s to line up")
            if found_where and moved:
                found_where += f", {moved}"
            unrelated = bool(cues) and not subtitles.fits(cues, transcript.words)
            if unrelated:
                cues = []
            matches, note = subtitles.annotate(matches, cues, transcript.words)
            if note:
                print(f"[censarr] job {job_id}: {note}", flush=True)
                cues = []
            # The subtitles first: muted where they have the word, soften it or
            # leave it out; left in where their word only sounds like it.
            settled, alike, matches = subtitles.decide(matches)
            # ...and the swears they have that Whisper did not catch.
            extra = subtitles.from_subtitles(cues, transcript.words,
                                             [*settled, *alike, *matches], matcher)
            alike_muted, alike_kept = self._second_listen(alike, wav, transcript.words,
                                                          settings)
            sub_note = ""
            if unrelated:
                sub_note = " · subtitles set aside (they do not follow the dialogue)"
            elif note:
                sub_note = " · subtitles set aside (another release?)"
            elif not cues:
                sub_note = " · no subtitles found to check against"
                if search_note:
                    sub_note += f" ({search_note})"
            elif found_where:
                sub_note = f" · subtitles from {found_where}"
            elif moved:
                sub_note = f" · subtitles {moved}"
            if extra:
                sub_note += f" · {len(extra)} from the subtitles"
            if alike_kept:
                sub_note += (f" · {len(alike_kept)} sound-alike"
                             f"{'s' if len(alike_kept) != 1 else ''} left in")
            matches, judged = self._adjudicate(matches, transcript.words, settings)
            matches = sorted([*settled, *extra, *alike_muted, *matches], key=lambda m: m.start)
            kept = sorted([*alike_kept, *judged], key=lambda m: m.start)
            db.save_detections(job_id, matches, kept)
            spans = words.to_spans(matches, settings.pad_start, settings.pad_end)

            if not spans:
                return {"status": "skipped", "muted": 0,
                        "message": "nothing to mute - no profanity found" + sub_note,
                        "cached_transcript": transcript.cached}

            self._stage(job_id, "muting",
                        f"muting {len(matches)} word{'s' if len(matches) != 1 else ''}")
            candidate = work / f"remux{path.suffix}"
            media.build_cleaned_file(
                original, source, spans, candidate, title=settings.track_title,
                fade=settings.fade, drop_audio=existing,
                surround_bitrate=settings.bitrate_surround,
                stereo_bitrate=settings.bitrate_stereo,
                progress=self._progress(job_id, "muting"))

            self._stage(job_id, "verifying", "checking the new file")
            checked = media.verify_replacement(
                original, candidate,
                expected_audio=len(original.audio) + 1 - len(existing))
            new_track = checked.cleaned_track
            added = (media.track_bytes(candidate, new_track.audio_index)
                     if new_track else 0)
            media.swap_in(candidate, path, keep_backup=settings.keep_backup)
            note = arr.refresh_for(settings, str(path), **self.item)
            return {"status": "done", "muted": len(matches), "added_bytes": added,
                    "message": f"added “{settings.track_title}” · "
                               f"{len(matches)} muted · {added / 1e6:.0f} MB · "
                               f"{note}{sub_note}",
                    "cached_transcript": transcript.cached}
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _sweep_stale_work(folder: Path, older_than: float = 6 * 3600) -> None:
    """Delete scratch directories a killed run left behind.

    The scratch directory sits beside the media file so the finished remux can
    be moved into place without crossing filesystems. That also means a
    container killed mid-job leaves several GB inside the user's TV folder,
    where it is both confusing and expensive.
    """
    now = time.time()
    for leftover in [*folder.glob("censarr-*"), *folder.glob("cleanarr-*")]:
        try:
            if leftover.is_dir() and now - leftover.stat().st_mtime > older_than:
                shutil.rmtree(leftover, ignore_errors=True)
        except OSError:
            pass


def more_subtitles(settings, kind: str, source: str, source_id: str, path: Path,
                   work: Path, title: str = "") -> tuple[str, str, str]:
    """(SRT text, where it came from, a note) for a file with no subtitles of
    its own; ("", "", note) when none were found. The note says why a search
    could not run - Jellyfin without a subtitle plugin.

    In order: what the media server holds for this very file (subtitles it
    downloaded, say); for a film, another copy of it that the server knows
    about - a WEB-DL beside a WEBRip often carries the subtitles the other
    lacks; and a search the server makes online.
    Any of these may be timed for a different cut: the caller lines them up,
    and sets them aside if they still disagree.
    """
    name = source if source in ("plex", "jellyfin") else getattr(settings, "media_server", "")
    client = library.server(settings, name) if name in ("plex", "jellyfin") else None
    if client is None:
        return "", "", ""
    here = str(path)
    try:
        item_id = (str(source_id) if source == name and source_id
                   else client.find(kind, here, title))
        versions = client.versions(item_id) if item_id else []
    except Exception as exc:  # noqa: BLE001
        print(f"[censarr] subtitles from {name}: {exc}", flush=True)
        return "", "", ""

    def fetch(v) -> tuple[str, str]:
        for label, url in v["subtitles"]:
            try:
                text = client.download(url)
            except Exception:  # noqa: BLE001
                continue
            if subtitles.parse_srt(text):
                return text, f"{name.title()} ({label})"
        return "", "", ""

    # The server may see the library under another path than this container.
    mine = [v for v in versions if library.same_file(v["path"], here)]
    for v in mine:
        found = fetch(v)
        if found[0]:
            return (*found, "")
    if kind == "movie":
        theirs_here = mine[0]["path"] if mine else here
        for v in (v for v in versions if not library.same_file(v["path"], here)):
            other = Path(library.local_path(v["path"], theirs_here, here))
            if not other.is_file():
                continue
            where = f"another copy ({other.name})"
            try:
                stream = media.pick_subtitles(media.probe(other))
            except Exception:  # noqa: BLE001
                stream = None
            dest = work / "other-copy.srt"
            if stream is not None and media.extract_subtitles(other, stream, dest):
                text = dest.read_text(encoding="utf8", errors="replace")
                if subtitles.parse_srt(text):
                    return text, where, ""
            side = media.find_sidecar_subtitles(other)
            if side is not None:
                text = side.read_text(encoding="utf8", errors="replace")
                if subtitles.parse_srt(text):
                    return text, where, ""
            text, _label = fetch(v)
            if text:
                return text, where, ""

    if not item_id:
        return "", "", ""
    if name == "jellyfin" and client.subtitle_provider() is False:
        return "", "", JELLYFIN_NO_PLUGIN
    try:
        attached = client.search_subtitles(item_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[censarr] {name} subtitle search: {exc}", flush=True)
        return "", "", ""
    if not attached:
        return "", "", ""
    # The server fetches the file after answering, so it can take a moment to
    # appear on the item.
    for attempt in range(SEARCH_POLLS):
        try:
            mine = [v for v in client.versions(item_id)
                    if library.same_file(v["path"], here)]
        except Exception:  # noqa: BLE001
            mine = []
        for v in mine:
            text, _label = fetch(v)
            if text:
                return text, f"a {name.title()} search ({attached})", ""
        if attempt + 1 < SEARCH_POLLS:
            time.sleep(SEARCH_WAIT)
    print(f"[censarr] {name} attached {attached!r} but it never appeared on the file",
          flush=True)
    return "", "", ""


JELLYFIN_NO_PLUGIN = ("Jellyfin has no subtitle plugin, so it cannot search for subtitles - install Open Subtitles from Dashboard > Plugins > Catalog")

# Seconds either side of a sound-alike played to the second Whisper server.
RELISTEN_PAD = 2.0

# How long to wait for subtitles the media server was asked to fetch.
SEARCH_POLLS = 6
SEARCH_WAIT = 3.0


def run_job(job_id: int, settings: config.Settings, cache_dir: Path,
            should_cancel=None) -> None:
    """Run one queued job and record what happened to it."""
    job = db.get(job_id)
    if job is None:
        return
    # The library's path is opened as given: this container has the media
    # mounted where the library says it is, or the job fails saying so.
    path = Path(job["path"])
    # What the added track is called, and what it has been called before, so
    # detection and the name written agree with the settings.
    media.set_clean_title(settings.track_title, settings.known_track_titles)
    # Removals run ffmpeg too, so the thread cap applies to both.
    media.THREADS = settings.ffmpeg_threads
    db.update(job_id, status="running", started_at=time.time(), message="",
              stage="probing", progress=0.0)
    try:
        keys = job.keys()
        item = {"kind": job["kind"] if "kind" in keys else "",
                "source": job["source"] if "source" in keys else "",
                "source_id": job["source_id"] if "source_id" in keys else "",
                "title": job["title"]}
        pipeline = Pipeline(settings, cache_dir, should_cancel, item=item)
        action = (job["action"] if "action" in keys else "clean") or "clean"
        if action == "remove":
            result = pipeline.remove(job_id, path)
        else:
            result = pipeline.run(
                job_id, path, force=bool(job["force"]), title=job["title"],
                more_subtitles=lambda work: more_subtitles(
                    settings, item["kind"], item["source"], item["source_id"],
                    path, work, title=job["title"]))
        db.update(job_id, status=result.get("status", "done"),
                  muted=result.get("muted", 0), message=result.get("message", ""),
                  added_bytes=result.get("added_bytes", 0),
                  progress=1.0, stage="", finished_at=time.time())
    except Cancelled:
        db.update(job_id, status="cancelled", message="cancelled", stage="",
                  finished_at=time.time())
    except Exception as exc:  # noqa: BLE001
        db.update(job_id, status="failed", message=f"{type(exc).__name__}: {exc}"[:500],
                  stage="", finished_at=time.time())
