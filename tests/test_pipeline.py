"""One file start to finish, with real ffmpeg and a fixed transcript."""

from __future__ import annotations

import json

import pytest

from censarr import asr, config, db, judge, media, pipeline
from mediakit import make_media, rms, samples

pytestmark = pytest.mark.ffmpeg

WORDS = [
    {"word": " what", "start": 0.5, "end": 0.7},
    {"word": " the", "start": 0.7, "end": 0.9},
    {"word": " fuck,", "start": 1.0, "end": 1.4},
    {"word": " some", "start": 2.0, "end": 2.2},
    {"word": " cock", "start": 3.0, "end": 3.4},
    {"word": " please", "start": 4.0, "end": 4.4},
]


@pytest.fixture
def fake_asr(monkeypatch):
    calls = []

    def transcribe(audio, **kw):
        calls.append(kw)
        return asr.Transcript(words=list(WORDS), language="en", duration=6.0,
                              model="fake")
    monkeypatch.setattr(asr, "transcribe", transcribe)
    return calls


@pytest.fixture(params=["mkv", "mp4"])
def episode(request, tmp_path):
    folder = tmp_path / "tv"
    folder.mkdir()
    return make_media(folder / f"e1.{request.param}", seconds=6.0)


def bare(episode):
    """The same episode with no subtitles: the model alone decides."""
    out = episode.with_name("bare-" + episode.name)
    make_media(out, seconds=6.0, subtitles=False)
    return out


def run(path, settings, cache, **kw) -> dict:
    job = db.enqueue(kind="episode", title="Show", path=str(path), **kw)
    pipeline.run_job(job, settings, cache)
    return dict(db.get(job))


def test_cleans_marks_and_records(home, settings, fake_asr, episode):
    job = run(episode, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    assert job["muted"] == 2
    assert job["added_bytes"] > 0
    p = media.probe(episode)
    assert p.cleaned_track is not None and p.cleaned_track.written_here
    pcm, loud = samples(episode, 1), rms(samples(episode, 0), 0.1, 0.4)
    assert rms(pcm, 1.0, 1.4) < loud * 0.01          # "fuck"
    assert rms(pcm, 3.0, 3.4) < loud * 0.01          # "cock", no judge configured
    assert rms(pcm, 2.0, 2.6) > loud * 0.5
    rows = db.detections(job["id"])
    assert [(r["text"], r["muted"]) for r in rows] == [("fuck,", 1), ("cock", 1)]
    assert not list(episode.parent.glob("cleanarr-*"))     # scratch removed


def test_a_cleaned_file_is_skipped_unless_forced(home, settings, fake_asr, episode):
    run(episode, settings, home / "cache")
    again = run(episode, settings, home / "cache")
    assert again["status"] == "skipped"
    forced = run(episode, settings, home / "cache", force=True)
    assert forced["status"] == "done", forced["message"]
    assert len(media.probe(episode).audio) == 2              # replaced, not added


def test_remove_takes_the_track_back_out(home, settings, fake_asr, episode):
    run(episode, settings, home / "cache")
    removed = run(episode, settings, home / "cache", action="remove")
    assert removed["status"] == "done", removed["message"]
    assert media.probe(episode).cleaned_track is None
    assert not db.cleaned_before(str(episode))
    nothing = run(episode, settings, home / "cache", action="remove")
    assert nothing["status"] == "skipped"


def test_jellyfin_is_asked_to_read_the_file_again(home, settings, fake_asr, stub, episode):
    # The swap keeps the file's date, so Jellyfin is told which item to read
    # again rather than only that a path changed - after a clean and a removal.
    def items(req):
        if req.query["IncludeItemTypes"] == ["Series"]:
            return 200, {"Items": [{"Id": "s1", "Name": "Show"}]}
        return 200, {"Items": [{"Id": "e1", "ParentIndexNumber": 1, "IndexNumber": 1,
                                "Path": str(episode)}]}
    stub.route("GET", "/Items", items)
    stub.route("POST", "/Items/e1/Refresh", lambda req: (204, b""))
    settings.media_server = "jellyfin"
    settings.jellyfin_url, settings.jellyfin_api_key = stub.url, "k"
    before = episode.stat().st_mtime

    job = run(episode, settings, home / "cache", source="sonarr", source_id="7")
    assert job["status"] == "done", job["message"]
    assert "Jellyfin: asked it to read the file again" in job["message"]
    assert abs(episode.stat().st_mtime - before) < 1         # the date is still kept
    assert len(stub.seen("/Items/e1/Refresh")) == 1

    removed = run(episode, settings, home / "cache", source="sonarr", source_id="7",
                  action="remove")
    assert removed["status"] == "done", removed["message"]
    assert "Jellyfin: asked it to read the file again" in removed["message"]
    assert len(stub.seen("/Items/e1/Refresh")) == 2
    assert not stub.seen("/Library/Media/Updated")


def test_a_lookalike_track_is_never_replaced(home, settings, fake_asr, tmp_path):
    src = make_media(tmp_path / "e.mkv", seconds=3.0, title="English")
    settings.track_title = "English"
    job = run(src, settings, home / "cache")
    assert job["status"] == "failed"
    assert "nothing in the file says" in job["message"] or "pick a different name" in job["message"]
    assert len(media.probe(src).audio) == 1


def test_nothing_to_mute_is_skipped(home, settings, monkeypatch, episode):
    monkeypatch.setattr(asr, "transcribe", lambda audio, **kw: asr.Transcript(
        words=[{"word": "hello", "start": 1, "end": 1.2}], language="en",
        duration=6.0, model="fake"))
    job = run(episode, settings, home / "cache")
    assert job["status"] == "skipped"
    assert "nothing to mute" in job["message"]
    assert media.probe(episode).cleaned_track is None


def test_the_judge_can_leave_a_word_in(home, settings, fake_asr, stub, episode):
    judge._style.clear()

    def generate(req):
        body = req.json()
        if body.get("prompt") == "":
            return 200, {}
        return 200, {"response": json.dumps([{"n": 1, "verdict": "CLEAN", "instead": "caulk"}])}
    stub.route("POST", "/api/generate", generate)
    stub.route("GET", "/api/ps", {"models": []})
    settings.judge_url = stub.url
    episode = bare(episode)
    job = run(episode, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    assert job["muted"] == 1
    rows = {r["text"]: r for r in db.detections(job["id"])}
    assert rows["cock"]["muted"] == 0
    assert "caulk" in rows["cock"]["reason"]
    pcm, loud = samples(episode, 1), rms(samples(episode, 0), 0.1, 0.4)
    assert rms(pcm, 3.0, 3.4) > loud * 0.5          # left audible


def test_an_unanswered_judge_leaves_the_word_muted(home, settings, fake_asr, episode):
    settings.judge_url = "http://127.0.0.1:9"
    job = run(bare(episode), settings, home / "cache")
    assert job["muted"] == 2


def test_allow_and_custom_words_apply(home, settings, fake_asr, episode):
    settings.allow_words = ["fuck"]
    settings.custom_words = ["please"]
    job = run(episode, settings, home / "cache")
    texts = sorted(r["text"] for r in db.detections(job["id"]))
    assert texts == ["cock", "please"]


def test_cancel_stops_before_anything_is_written(home, settings, fake_asr, episode):
    job = db.enqueue(kind="episode", title="Show", path=str(episode))
    pipeline.run_job(job, settings, home / "cache", should_cancel=lambda: True)
    assert db.get(job)["status"] == "cancelled"
    assert media.probe(episode).cleaned_track is None


def test_missing_file_explains_itself(home, settings, tmp_path):
    job = run(tmp_path / "gone" / "e.mkv", settings, home / "cache")
    assert job["status"] == "failed"
    assert "not found from this container" in job["message"]


def test_settings_reach_the_listener(home, settings, fake_asr, episode):
    settings.asr_backend = "remote"
    settings.asr_url = "http://whisper"
    settings.asr_remote_model = "m"
    settings.trim_silence = True
    run(episode, settings, home / "cache")
    kw = fake_asr[-1]
    assert kw["remote_url"] == "http://whisper" and kw["remote_model"] == "m"
    assert kw["trim_silence"] is True
    settings.asr_backend = "builtin"
    run(episode, settings, home / "cache", force=True)
    assert fake_asr[-1]["remote_url"] == ""


def test_every_muting_setting_reaches_ffmpeg(home, settings, fake_asr, episode, monkeypatch):
    seen = {}
    real = media.build_cleaned_file

    def spy(original, track, spans, dest, **kw):
        seen.update(kw, spans=spans)
        return real(original, track, spans, dest, **kw)
    monkeypatch.setattr(media, "build_cleaned_file", spy)
    settings.pad_start, settings.pad_end, settings.fade = 0.3, 0.25, 0.05
    settings.bitrate_stereo, settings.bitrate_surround = "96k", "256k"
    settings.track_title = "Family"
    settings.ffmpeg_threads = 1
    job = run(episode, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    assert seen["spans"][0] == (0.7, 1.65)            # "fuck" 1.0-1.4, padded
    assert seen["fade"] == 0.05
    assert (seen["stereo_bitrate"], seen["surround_bitrate"]) == ("96k", "256k")
    assert seen["title"] == "Family"
    assert media.THREADS == 1
    pcm, loud = samples(episode, 1), rms(samples(episode, 0), 0.1, 0.4)
    assert rms(pcm, 0.75, 0.95) < loud * 0.01          # the padding is muted too


def test_keep_backup_leaves_the_original_beside_it(home, settings, fake_asr, episode):
    settings.keep_backup = True
    run(episode, settings, home / "cache")
    backup = episode.with_suffix(episode.suffix + ".censarr-backup")
    assert backup.exists()
    assert media.probe(backup).cleaned_track is None


def test_listening_settings_reach_the_model(home, settings, fake_asr, episode):
    settings.device, settings.compute_type, settings.model = "cpu", "int8", "small.en"
    settings.asr_backend, settings.asr_url, settings.asr_api_key = "remote", "http://w", "sk"
    run(episode, settings, home / "cache")
    kw = fake_asr[-1]
    assert (kw["device"], kw["compute_type"], kw["model_name"]) == ("cpu", "int8", "small.en")
    assert kw["remote_key"] == "sk"


def test_scratch_folders_left_by_a_killed_run_are_swept(tmp_path):
    import os
    import time
    old = tmp_path / "cleanarr-old"
    fresh = tmp_path / "cleanarr-fresh"
    mine = tmp_path / "Season 01"
    for folder in (old, fresh, mine):
        folder.mkdir()
    stale = time.time() - 7 * 3600
    os.utime(old, (stale, stale))
    pipeline._sweep_stale_work(tmp_path)
    assert not old.exists()
    assert fresh.exists() and mine.exists()


def test_not_enough_space_stops_before_anything_is_written(home, settings, fake_asr,
                                                             episode, monkeypatch):
    monkeypatch.setattr(media, "free_space", lambda path: 1024)
    job = run(episode, settings, home / "cache")
    assert job["status"] == "failed" and "not enough free space" in job["message"]
    assert not list(episode.parent.glob("cleanarr-*"))
    assert media.probe(episode).cleaned_track is None
    assert fake_asr == []                       # it did not even listen


# ---------------------------------------------------------------- guardrails

CUES = [(0.8, 1.6, "What the fuck?"), (2.8, 3.8, "Get some caulk on it."), (4.0, 4.6, "Please.")]


@pytest.fixture(params=["mkv", "mp4"])
def subtitled(request, tmp_path):
    folder = tmp_path / "tv"
    folder.mkdir()
    return make_media(folder / f"e1.{request.param}", seconds=6.0, cues=CUES)


def test_the_subtitles_are_recorded_on_every_detection(home, settings, monkeypatch, subtitled):
    heard = [dict(w) for w in WORDS]
    heard[2]["probability"] = 0.97          # "fuck," - sure
    heard[4]["probability"] = 0.31          # "cock" - unsure
    monkeypatch.setattr(asr, "transcribe", lambda audio, **kw: asr.Transcript(
        words=heard, language="en", duration=6.0, model="fake"))
    job = run(subtitled, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    rows = {r["text"]: dict(r) for r in db.detections(job["id"])}
    assert rows["fuck,"]["subtitle_state"] == "agrees" and rows["fuck,"]["confidence"] == 0.97
    assert rows["cock"]["subtitle_state"] == "soundalike" and rows["cock"]["confidence"] == 0.31
    assert "caulk" in rows["cock"]["subtitle"]
    history = {r["id"]: r["to_check"] for r in db.history()}
    assert history[job["id"]] == 1


def test_a_sound_alike_is_left_in_even_when_whisper_was_sure(home, settings, monkeypatch, subtitled):
    heard = [dict(w) for w in WORDS]
    heard[4]["probability"] = 0.98
    monkeypatch.setattr(asr, "transcribe", lambda audio, **kw: asr.Transcript(
        words=heard, language="en", duration=6.0, model="fake"))
    job = run(subtitled, settings, home / "cache")
    assert job["muted"] == 1 and "1 sound-alike left in" in job["message"]


def unsure_cock(monkeypatch):
    heard = [dict(w) for w in WORDS]
    heard[2]["probability"] = 0.97          # "fuck," - sure
    heard[4]["probability"] = 0.31          # "cock" - unsure
    monkeypatch.setattr(asr, "transcribe", lambda audio, **kw: asr.Transcript(
        words=heard, language="en", duration=6.0, model="fake"))


def test_the_subtitles_leave_in_what_only_sounds_like_the_word(home, settings,
                                                                      monkeypatch, subtitled):
    unsure_cock(monkeypatch)
    job = run(subtitled, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    assert job["muted"] == 1
    rows = {r["text"]: dict(r) for r in db.detections(job["id"])}
    assert rows["fuck,"]["muted"] == 1
    assert rows["cock"]["muted"] == 0 and "caulk" in rows["cock"]["reason"]
    pcm, loud = samples(subtitled, 1), rms(samples(subtitled, 0), 0.1, 0.4)
    assert rms(pcm, 1.0, 1.4) < loud * 0.01          # "fuck" muted
    assert rms(pcm, 3.0, 3.4) > loud * 0.5           # "caulk" left in


@pytest.mark.parametrize("answer, muted", [("PROFANE", 2), ("CLEAN", 1), (None, 1)])
def test_the_model_rules_on_a_sound_alike_before_it_is_left_in(home, settings, monkeypatch,
                                                                subtitled, answer, muted):
    unsure_cock(monkeypatch)
    settings.judge_url = "http://judge"
    asked = []

    def adjudicate(items, *a, **kw):
        asked.extend(items)
        return {items[0]["n"]: (answer, "caulk")} if answer else {}
    monkeypatch.setattr(judge, "adjudicate", adjudicate)
    monkeypatch.setattr(asr, "unload_model", lambda: None)
    job = run(subtitled, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    assert len(asked) == 1 and "the subtitles show" in asked[0]["line"]
    assert job["muted"] == muted


def test_a_second_whisper_server_can_hear_it_again(home, settings, monkeypatch, subtitled):
    unsure_cock(monkeypatch)
    settings.recheck_url = "http://whisper"
    heard_again = []

    def again(clip, url, model, key="", timeout=0):
        heard_again.append((url, model))
        return asr.Transcript(words=[{"word": " some", "start": 0.1, "end": 0.3},
                                     {"word": " cock", "start": 1.0, "end": 1.3}],
                              language="en", duration=4.0, model=model)
    monkeypatch.setattr(asr, "transcribe_remote", again)
    job = run(subtitled, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    assert heard_again == [("http://whisper", settings.recheck_model)]
    rows = {r["text"]: dict(r) for r in db.detections(job["id"])}
    assert rows["cock"]["muted"] == 1 and "second listen" in rows["cock"]["reason"]


def test_a_subtitle_file_beside_the_media_is_used(home, settings, monkeypatch, tmp_path):
    unsure_cock(monkeypatch)
    folder = tmp_path / "tv"
    folder.mkdir()
    plain = make_media(folder / "e1.mkv", seconds=6.0, subtitles=False)
    (folder / "e1.en.srt").write_text(
        "1\n00:00:00,800 --> 00:00:01,600\nWhat the fuck?\n\n"
        "2\n00:00:02,800 --> 00:00:03,800\nGet some caulk on it.\n")
    job = run(plain, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    rows = {r["text"]: dict(r) for r in db.detections(job["id"])}
    assert rows["cock"]["muted"] == 0 and rows["fuck,"]["muted"] == 1


def test_no_subtitles_means_muted(home, settings, monkeypatch, tmp_path):
    unsure_cock(monkeypatch)
    plain = make_media(tmp_path / "plain.mkv", seconds=6.0, subtitles=False)
    job = run(plain, settings, home / "cache")
    assert job["status"] == "done" and job["muted"] == 2
    assert "no subtitles found to check against" in job["message"]


SRT = ("1\n00:00:00,800 --> 00:00:01,600\nWhat the fuck?\n\n"
       "2\n00:00:02,800 --> 00:00:03,800\nGet some caulk on it.\n")


class FakeServer:
    """A media server that knows a film's versions and holds one subtitle file.

    `on_search` is what the item's versions become once it is asked to search
    online; None means the search finds nothing."""

    def __init__(self, versions, films=(), on_search=None, provider=True):
        self._versions, self._films, self.asked = versions, list(films), []
        self.on_search, self.searched, self.found_for = on_search, [], []
        self.provider = provider

    def subtitle_provider(self):
        return self.provider

    def versions(self, item_id):
        self.asked.append(item_id)
        return self._versions

    def find(self, kind, path, title=""):
        self.found_for.append((kind, title))
        return next((f["id"] for f in self._films if f["path"] == path), "")

    def search_subtitles(self, item_id):
        self.searched.append(item_id)
        if self.on_search is None:
            return ""
        self._versions = self.on_search
        return "English (OpenSubtitles)"

    def download(self, url):
        return SRT


def run_film(path, settings, cache, **kw) -> dict:
    job = db.enqueue(kind="movie", title="Film", path=str(path), **kw)
    pipeline.run_job(job, settings, cache)
    return dict(db.get(job))


def test_another_copy_of_the_film_lends_its_subtitles(home, settings, monkeypatch, tmp_path):
    unsure_cock(monkeypatch)
    settings.media_server = "plex"
    folder = tmp_path / "movies" / "Film"
    folder.mkdir(parents=True)
    plain = make_media(folder / "Film WEBRip.mkv", seconds=6.0, subtitles=False)
    other = make_media(folder / "Film WEBDL.mkv", seconds=6.0, cues=CUES)
    server = FakeServer([{"path": str(plain), "subtitles": []},
                         {"path": str(other), "subtitles": []}],
                        films=[{"id": "5", "path": str(plain)}])
    monkeypatch.setattr(pipeline.library, "server", lambda s, name="": server)
    # Queued when Radarr listed the films: found on the media server by its path.
    job = run_film(plain, settings, home / "cache", source="radarr", source_id="3003")
    assert job["status"] == "done", job["message"]
    assert job["muted"] == 1
    assert "subtitles from another copy (Film WEBDL.mkv)" in job["message"]
    assert server.asked == ["5"]
    assert media.probe(other).cleaned_track is None        # the other copy is only read


def test_another_copy_is_found_when_the_server_mounts_films_elsewhere(
        home, settings, monkeypatch, tmp_path):
    unsure_cock(monkeypatch)
    settings.media_server = "plex"
    folder = tmp_path / "movies" / "Film"
    folder.mkdir(parents=True)
    plain = make_media(folder / "Film WEBRip.mkv", seconds=6.0, subtitles=False)
    make_media(folder / "Film WEBDL.mkv", seconds=6.0, cues=CUES)
    # Plex has the films at /srv/movies; this container at <tmp>/movies.
    server = FakeServer([{"path": "/srv/movies/Film/Film WEBRip.mkv", "subtitles": []},
                         {"path": "/srv/movies/Film/Film WEBDL.mkv", "subtitles": []}],
                        films=[{"id": "5", "path": str(plain)}])
    monkeypatch.setattr(pipeline.library, "server", lambda s, name="": server)
    job = run_film(plain, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    assert job["muted"] == 1
    assert "subtitles from another copy (Film WEBDL.mkv)" in job["message"]


def test_a_search_is_read_back_when_the_server_mounts_shows_elsewhere(
        home, settings, monkeypatch, tmp_path):
    unsure_cock(monkeypatch)
    monkeypatch.setattr(pipeline, "SEARCH_WAIT", 0)
    folder = tmp_path / "Season 01"
    folder.mkdir()
    plain = make_media(folder / "e1.mkv", seconds=6.0, subtitles=False)
    theirs = "/data/tv/Show/Season 01/e1.mkv"
    server = FakeServer([{"path": theirs, "subtitles": []}],
                        on_search=[{"path": theirs, "subtitles": [("English", "http://p/s/9")]}])
    monkeypatch.setattr(pipeline.library, "server", lambda s, name="": server)
    job = run(plain, settings, home / "cache", source="plex", source_id="41")
    assert job["status"] == "done", job["message"]
    assert job["muted"] == 1 and "a Plex search" in job["message"]


def test_subtitles_the_media_server_holds_are_used(home, settings, monkeypatch, tmp_path):
    unsure_cock(monkeypatch)
    plain = make_media(tmp_path / "e1.mkv", seconds=6.0, subtitles=False)
    server = FakeServer([{"path": str(plain), "subtitles": [("English (SRT)", "http://plex/s/1")]}])
    monkeypatch.setattr(pipeline.library, "server", lambda s, name="": server)
    job = run(plain, settings, home / "cache", source="plex", source_id="7")
    assert job["status"] == "done", job["message"]
    assert job["muted"] == 1 and "subtitles from Plex (English (SRT))" in job["message"]
    assert server.asked == ["7"]


def test_an_episode_never_borrows_another_files_subtitles(home, settings, monkeypatch, tmp_path):
    unsure_cock(monkeypatch)
    plain = make_media(tmp_path / "e1.mkv", seconds=6.0, subtitles=False)
    other = make_media(tmp_path / "e2.mkv", seconds=6.0, cues=CUES)
    server = FakeServer([{"path": str(plain), "subtitles": []},
                         {"path": str(other), "subtitles": []}])
    monkeypatch.setattr(pipeline.library, "server", lambda s, name="": server)
    job = run(plain, settings, home / "cache", source="plex", source_id="7")
    assert job["muted"] == 2 and "no subtitles found" in job["message"]


def test_the_media_server_is_asked_to_search_when_nothing_else_has_them(
        home, settings, monkeypatch, tmp_path):
    unsure_cock(monkeypatch)
    settings.media_server = "plex"
    monkeypatch.setattr(pipeline, "SEARCH_WAIT", 0)
    plain = make_media(tmp_path / "e1.mkv", seconds=6.0, subtitles=False)
    server = FakeServer([{"path": str(plain), "subtitles": []}],
                        films=[{"id": "41", "path": str(plain)}],
                        on_search=[{"path": str(plain), "subtitles": [("English", "http://p/s/9")]}])
    monkeypatch.setattr(pipeline.library, "server", lambda s, name="": server)
    # An episode Sonarr listed: found on the media server by its show and file.
    job = run(plain, settings, home / "cache", source="sonarr", source_id="1234")
    assert job["status"] == "done", job["message"]
    assert job["muted"] == 1
    assert "subtitles from a Plex search (English (OpenSubtitles))" in job["message"]
    assert server.found_for == [("episode", "Show")] and server.searched == ["41"]


def test_a_search_that_finds_nothing_leaves_everything_muted(home, settings, monkeypatch, tmp_path):
    unsure_cock(monkeypatch)
    plain = make_media(tmp_path / "e1.mkv", seconds=6.0, subtitles=False)
    server = FakeServer([{"path": str(plain), "subtitles": []}])
    monkeypatch.setattr(pipeline.library, "server", lambda s, name="": server)
    job = run(plain, settings, home / "cache", source="plex", source_id="41")
    assert job["muted"] == 2 and server.searched == ["41"]
    assert "no subtitles found" in job["message"]


def test_the_old_subtitle_switches_are_forgotten(home):
    config.CONFIG_FILE.write_text("subtitle_opinion: false\nsubtitle_search: false\n",
                                  encoding="utf8")
    s = config.load()
    assert not hasattr(s, "subtitle_opinion") and not hasattr(s, "subtitle_search")


def test_jellyfin_without_a_subtitle_plugin_cleans_by_listening(home, settings, monkeypatch, tmp_path):
    unsure_cock(monkeypatch)
    plain = make_media(tmp_path / "e1.mkv", seconds=6.0, subtitles=False)
    server = FakeServer([{"path": str(plain), "subtitles": []}], provider=False)
    monkeypatch.setattr(pipeline.library, "server", lambda s, name="": server)
    job = run(plain, settings, home / "cache", source="jellyfin", source_id="41")
    assert job["status"] == "done" and job["muted"] == 2
    assert "no subtitle plugin" in job["message"] and server.searched == []


def test_subtitles_that_do_not_follow_the_dialogue_decide_nothing(home, settings, monkeypatch, tmp_path):
    """Two detections are too few for the per-detection check; the whole
    transcript is not."""
    talk = [f"sentence{n} about gardening tomatoes carefully" for n in range(12)]
    heard, cues = [], []
    for n, line in enumerate(talk):
        start = 10.0 + n * 4.0
        for k, word in enumerate(line.split()):
            heard.append({"word": " " + word, "start": start + k * 0.4, "end": start + k * 0.4 + 0.3,
                          "probability": 0.9})
        cues.append((start - 0.1, start + 2.5, "Completely unrelated commentary about lenses"))
    heard.append({"word": " cock", "start": 70.0, "end": 70.3, "probability": 0.3})
    cues.append((69.8, 71.0, "Get some caulk on it."))
    monkeypatch.setattr(asr, "transcribe", lambda audio, **kw: asr.Transcript(
        words=heard, language="en", duration=80.0, model="fake"))
    src = make_media(tmp_path / "e1.mkv", seconds=80.0, cues=cues)
    job = run(src, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    assert job["muted"] == 1                            # "cock" stays muted
    assert "do not follow the dialogue" in job["message"]


def test_a_file_without_subtitles_still_cleans(home, settings, fake_asr, tmp_path):
    src = make_media(tmp_path / "plain.mkv", seconds=6.0, subtitles=False)
    job = run(src, settings, home / "cache")
    assert job["status"] == "done"
    assert {r["subtitle_state"] for r in db.detections(job["id"])} == {""}
    assert "no subtitles found to check against" in job["message"]


def test_a_per_show_exception_applies_to_that_show_only(home, settings, fake_asr, tmp_path):
    settings.allow_words_by_title = {"Show": ["cock"]}
    a = make_media(tmp_path / "a.mkv", seconds=6.0)
    b = make_media(tmp_path / "b.mkv", seconds=6.0)
    here = run(a, settings, home / "cache")                      # titled "Show"
    job = db.enqueue(kind="episode", title="Another Show", path=str(b))
    pipeline.run_job(job, settings, home / "cache")
    assert [r["text"] for r in db.detections(here["id"])] == ["fuck,"]
    assert [r["text"] for r in db.detections(job)] == ["fuck,", "cock"]
