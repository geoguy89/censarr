"""Real files through real ffmpeg: marking, muting, verifying, swapping."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from censarr import media
from mediakit import audio_tags, make_media, rms, samples, streams

pytestmark = pytest.mark.ffmpeg


@pytest.fixture(autouse=True)
def _title():
    media.set_clean_title(media.DEFAULT_CLEAN_TITLE)
    yield
    media.set_clean_title(media.DEFAULT_CLEAN_TITLE)


@pytest.fixture(params=["mkv", "mp4"])
def source(request, tmp_path):
    return make_media(tmp_path / f"episode.{request.param}", seconds=6.0)


def build(src_path, spans, out_name="out", fade=0.02, drop=()):
    original = media.probe(src_path)
    track = media.pick_source_track(original)
    dest = src_path.with_name(f"{out_name}{src_path.suffix}")
    media.build_cleaned_file(original, track, spans, dest, fade=fade, drop_audio=drop)
    return original, dest


def test_probe_reads_the_file(source):
    p = media.probe(source)
    assert p.video_streams == 1
    assert len(p.audio) == 1
    assert abs(p.duration - 6.0) < 0.2
    assert p.audio[0].language == "eng"
    assert p.audio[0].default
    assert not p.audio[0].is_cleaned


def test_probe_of_a_non_media_file_fails_cleanly(tmp_path):
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"not a video")
    with pytest.raises(media.MediaError, match="ffprobe failed"):
        media.probe(junk)


def test_new_track_is_marked_in_both_containers(source):
    original, out = build(source, [(1.0, 2.0)])
    kinds = [s["codec_type"] for s in streams(out)]
    assert kinds.count("subtitle") == 1          # subtitles survive
    tags = audio_tags(out, 1)
    assert tags["handler_name"] == media.CLEAN_TITLE
    if source.suffix == ".mkv":
        assert tags["title"] == media.CLEAN_TITLE
        assert tags[media.MARK_KEY] == media.MARK_VALUE
    else:
        # MP4 keeps only handler_name: no per-track title, no custom tag.
        assert "title" not in tags
        assert media.MARK_KEY not in tags
    reread = media.probe(out)
    assert reread.cleaned_track is not None
    assert reread.cleaned_track.audio_index == 1
    assert reread.cleaned_track.written_here
    assert not reread.audio[1].default           # the original stays default
    assert reread.audio[0].default


def test_new_track_is_silent_inside_spans_and_untouched_outside(source):
    spans = [(1.0, 1.6), (3.2, 4.0)]
    _, out = build(source, spans)
    original_pcm = samples(source, 0)
    clean = samples(out, 1)
    loud = rms(original_pcm, 0.2, 0.8)
    assert loud > 0.03
    for start, end in spans:
        # Inside the span, clear of the 20ms fades at each edge.
        assert rms(clean, start + 0.05, end - 0.05) < loud * 0.01
    for start, end in [(0.2, 0.9), (2.0, 3.0), (4.4, 5.5)]:
        assert rms(clean, start, end) == pytest.approx(rms(original_pcm, start, end), rel=0.15)


def test_no_spans_still_writes_an_identical_sounding_track(source):
    _, out = build(source, [])
    a, b = samples(source, 0), samples(out, 1)
    assert rms(b, 0.5, 5.0) == pytest.approx(rms(a, 0.5, 5.0), rel=0.15)


def test_verify_accepts_a_good_file(source):
    original, out = build(source, [(1.0, 2.0)])
    got = media.verify_replacement(original, out)
    assert len(got.audio) == 2


def test_verify_refuses_a_file_that_lost_its_track(source):
    original = media.probe(source)
    with pytest.raises(media.MediaError, match="audio tracks, expected 2"):
        media.verify_replacement(original, source)


def test_verify_refuses_a_file_with_no_cleaned_track(source):
    original = media.probe(source)
    with pytest.raises(media.MediaError, match="no track named"):
        media.verify_replacement(original, source, expected_audio=1)


def test_verify_refuses_a_truncated_file(source, tmp_path):
    original, out = build(source, [(1.0, 2.0)])
    short = tmp_path / f"short{source.suffix}"
    subprocess_ok = os.system(
        f"ffmpeg -nostdin -v error -y -i {out} -map 0 -c copy -t 3 {short}") == 0
    assert subprocess_ok
    with pytest.raises(media.MediaError, match="long, the original was"):
        media.verify_replacement(original, short)


def test_verify_refuses_a_file_that_lost_its_picture(source, tmp_path):
    original, out = build(source, [(1.0, 2.0)])
    blind = tmp_path / f"blind{source.suffix}"
    assert os.system(
        f"ffmpeg -nostdin -v error -y -i {out} -map 0 -map -0:v -c copy {blind}") == 0
    with pytest.raises(media.MediaError, match="video streams"):
        media.verify_replacement(original, blind)


def test_check_interleave_passes_a_real_file(source):
    _, out = build(source, [(1.0, 2.0)])
    assert media.packet_offset(out, "a:1", 3.0) is not None
    media.check_interleave(out, 1, media.probe(out).duration)


def test_check_interleave_refuses_a_distant_track(source, monkeypatch):
    _, out = build(source, [(1.0, 2.0)])
    offsets = {"a:1": 600_000_000, "a:0": 1_000}
    monkeypatch.setattr(media, "packet_offset", lambda p, stream, at: offsets[stream])
    with pytest.raises(media.MediaError, match="badly interleaved"):
        media.check_interleave(out, 1, 100.0)


def test_check_interleave_skips_what_it_cannot_measure(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "packet_offset", lambda p, stream, at: None)
    media.check_interleave(tmp_path / "x.mkv", 1, 100.0)
    media.check_interleave(tmp_path / "x.mkv", 1, 0.0)


def test_verify_runs_the_interleave_check(source, monkeypatch):
    original, out = build(source, [(1.0, 2.0)])
    offsets = {"a:1": 600_000_000, "a:0": 0}
    monkeypatch.setattr(media, "packet_offset", lambda p, stream, at: offsets[stream])
    with pytest.raises(media.MediaError, match="badly interleaved"):
        media.verify_replacement(original, out)


def test_reclean_replaces_rather_than_adds(source):
    _, once = build(source, [(1.0, 2.0)], out_name="once")
    first = media.probe(once)
    _, twice = build(once, [(3.0, 4.0)], out_name="twice", drop=(1,))
    got = media.verify_replacement(first, twice, expected_audio=2)
    assert [a.is_cleaned for a in got.audio] == [False, True]
    pcm = samples(twice, 1)
    loud = rms(samples(source, 0), 0.2, 0.8)
    assert rms(pcm, 3.1, 3.9) < loud * 0.01       # new span muted
    assert rms(pcm, 1.1, 1.9) > loud * 0.5        # old span back


def test_remove_cleaned_track_round_trip(source):
    _, out = build(source, [(1.0, 2.0)])
    cleaned = media.probe(out)
    back = out.with_name(f"back{out.suffix}")
    assert media.remove_cleaned_track(cleaned, back) == 1
    got = media.probe(back)
    assert len(got.audio) == 1 and got.cleaned_track is None
    assert got.video_streams == 1
    assert [s["codec_type"] for s in streams(back)].count("subtitle") == 1


def test_track_bytes_measures_the_new_track(source):
    _, out = build(source, [(1.0, 2.0)])
    assert media.track_bytes(out, 1) > 1000


def test_surround_mkv_keeps_its_codec(tmp_path):
    src = make_media(tmp_path / "surround.mkv", seconds=3.0, channels=6,
                     audio_codec="eac3")
    _, out = build(src, [(1.0, 1.5)])
    audio = [s for s in streams(out) if s["codec_type"] == "audio"]
    assert audio[1]["codec_name"] == "eac3"
    assert audio[1]["channels"] == 6


def test_swap_in_keeps_times_and_mode(tmp_path, source):
    _, out = build(source, [(1.0, 2.0)])
    os.chmod(source, 0o640)
    old = time.time() - 86400 * 30
    os.utime(source, (old, old))
    media.swap_in(out, source, keep_backup=True)
    st = source.stat()
    assert abs(st.st_mtime - old) < 1
    assert st.st_mode & 0o777 == 0o640
    assert media.probe(source).cleaned_track is not None
    backup = source.with_suffix(source.suffix + ".censarr-backup")
    assert backup.exists() and media.probe(backup).cleaned_track is None
    assert not out.exists()


def test_extract_for_asr_is_16k_mono(tmp_path, source):
    original = media.probe(source)
    wav = media.extract_for_asr(source, original.audio[0], tmp_path / "a.wav")
    info = [s for s in streams(wav) if s["codec_type"] == "audio"][0]
    assert info["sample_rate"] == "16000" and info["channels"] == 1


def test_a_failing_ffmpeg_is_reported(tmp_path, source):
    original = media.probe(source)
    track = original.audio[0]
    with pytest.raises(media.MediaError, match="ffmpeg failed"):
        media.build_cleaned_file(original, track, [(1, 2)], tmp_path / "no" / "dir.mkv")


def test_verify_accepts_a_removal(source):
    _, out = build(source, [(1.0, 2.0)])
    cleaned = media.probe(out)
    back = out.with_name(f"back{out.suffix}")
    media.remove_cleaned_track(cleaned, back)
    got = media.verify_replacement(cleaned, back, expected_audio=1, expect_cleaned=False)
    assert got.cleaned_track is None
    with pytest.raises(media.MediaError, match="still has a track"):
        media.verify_replacement(cleaned, out, expected_audio=2, expect_cleaned=False)


def test_subtitles_are_found_and_extracted_with_the_audio(source, tmp_path):
    p = media.probe(source)
    assert len(p.subtitles) == 1 and p.subtitles[0].is_text
    chosen = media.pick_subtitles(p)
    assert chosen is not None
    wav, srt = tmp_path / "a.wav", tmp_path / "s.srt"
    media.extract_for_asr(source, p.audio[0], wav, subtitles=chosen, subtitle_dest=srt)
    assert wav.exists()
    assert "line 1" in srt.read_text()


def test_audio_still_comes_out_if_the_subtitles_cannot(source, tmp_path):
    p = media.probe(source)
    bogus = media.SubtitleStream(sub_index=7, codec="subrip", language="eng", title="", forced=False)
    wav, srt = tmp_path / "a.wav", tmp_path / "s.srt"
    media.extract_for_asr(source, p.audio[0], wav, subtitles=bogus, subtitle_dest=srt)
    assert wav.exists() and not srt.exists()


def test_which_subtitle_file_beside_the_media_is_used(tmp_path):
    video = tmp_path / "Show - S01E01.mkv"
    video.write_bytes(b"")
    assert media.find_sidecar_subtitles(video) is None
    for name in ("Show - S01E01.fr.srt", "Show - S01E01.en.forced.srt", "Show - S01E01.srt",
                 "Show - S01E02.en.srt", "Show - S01E01.en.sdh.srt", "Show - S01E01.en.ass"):
        (tmp_path / name).write_text("x")
    assert media.find_sidecar_subtitles(video).name == "Show - S01E01.en.sdh.srt"
    (tmp_path / "Show - S01E01.en.sdh.srt").unlink()
    assert media.find_sidecar_subtitles(video).name == "Show - S01E01.srt"   # untagged
    (tmp_path / "Show - S01E01.srt").unlink()
    assert media.find_sidecar_subtitles(video) is None       # French and forced never


def test_a_late_audio_track_is_measured(tmp_path):
    late = make_media(tmp_path / "late.mkv", seconds=6.0, audio_delay=2.0)
    p = media.probe(late)
    assert media.speech_offset(p, p.audio[0]) == pytest.approx(2.0, abs=0.05)
    on_time = media.probe(make_media(tmp_path / "on.mkv", seconds=6.0))
    assert media.speech_offset(on_time, on_time.audio[0]) == pytest.approx(0.0, abs=0.05)


def test_which_subtitles_are_used():
    def sub(i, codec="subrip", language="eng", forced=False, title=""):
        return media.SubtitleStream(i, codec, language, title, forced)
    p = media.Probe(path=Path("x.mkv"), duration=1, video_streams=1, audio=[], container="mkv")
    p.subtitles = [sub(0, codec="hdmv_pgs_subtitle"), sub(1, forced=True), sub(2, language="fre"),
                   sub(3, language=""), sub(4)]
    assert media.pick_subtitles(p).sub_index == 4           # English, text, not forced
    p.subtitles = [sub(0, title="Commentary"), sub(1, title="Signs & Songs"), sub(2, title="SDH")]
    assert media.pick_subtitles(p).sub_index == 2           # somebody else talking, or a few lines
    p.subtitles = [sub(0, title="English (Forced)"), sub(1, language="")]
    assert media.pick_subtitles(p).sub_index == 1           # untagged beats forced
    p.subtitles = [sub(0, codec="dvd_subtitle")]
    assert media.pick_subtitles(p) is None                  # pictures cannot be read


def test_a_track_marked_by_the_old_name_is_still_ours(tmp_path):
    """Files censored as Cleanarr carry a "cleanarr" tag on the track it wrote."""
    import subprocess
    src = make_media(tmp_path / "e1.mkv", seconds=2.0, subtitles=False)
    out = tmp_path / "old.mkv"
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-i", str(src), "-map", "0:v", "-map", "0:a",
                    "-map", "0:a", "-c", "copy", "-metadata:s:a:1", "title=Whatever",
                    "-metadata:s:a:1", "cleanarr=1", str(out)], check=True)
    p = media.probe(out)
    assert p.audio[1].written_here and not p.audio[0].written_here


def test_censored_subtitles_are_added_ours_and_removed_again(source, tmp_path):
    """The masked subtitles go in beside the censored audio, carry the same
    name, are never read back as the subtitles to check against, and go
    again when the censored track is removed."""
    masked = tmp_path / "censored.srt"
    masked.write_text("1\n00:00:01,000 --> 00:00:02,000\nWhat the ****\n\n", encoding="utf8")
    original = media.probe(source)
    track = media.pick_source_track(original)
    out = source.with_name(f"subs{source.suffix}")
    media.build_cleaned_file(original, track, [(1.0, 2.0)], out, subtitles=masked)
    got = media.verify_replacement(original, out,
                                   expected_subtitles=len(original.subtitles) + 1)
    assert got.our_subtitles == (len(original.subtitles),)
    ours = got.subtitles[-1]
    assert ours.is_text and ours.language == "eng"
    picked = media.pick_subtitles(got)
    assert picked is None or picked.sub_index != ours.sub_index

    # Censoring again replaces it rather than adding a second.
    again = source.with_name(f"again{source.suffix}")
    media.build_cleaned_file(got, media.pick_source_track(got), [(3.0, 4.0)], again,
                             drop_audio=(got.cleaned_track.audio_index,),
                             subtitles=masked, drop_subtitles=got.our_subtitles)
    assert len(media.probe(again).our_subtitles) == 1

    back = source.with_name(f"back{source.suffix}")
    media.remove_cleaned_track(got, back, drop_subtitles=got.our_subtitles)
    gone = media.verify_replacement(got, back, expected_audio=1, expect_cleaned=False,
                                    expected_subtitles=len(original.subtitles))
    assert gone.our_subtitles == ()
