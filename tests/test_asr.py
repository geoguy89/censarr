"""Listening: caching, the remote server, and errors a person can act on."""

from __future__ import annotations

import json
import struct

import pytest

from censarr import asr


@pytest.fixture(autouse=True)
def _fresh():
    asr.unload_model()
    yield
    asr.unload_model()


def wav(path, seconds=0.2):
    frames = b"\x00\x00" * int(16000 * seconds)
    path.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
                     + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
                     + b"data" + struct.pack("<I", len(frames)) + frames)
    return path


def test_gpu_chosen_without_one(monkeypatch):
    pytest.importorskip("faster_whisper")
    monkeypatch.setattr(asr, "_cuda_available", lambda: False)
    with pytest.raises(asr.ModelError, match="can see none"):
        asr.load_model("tiny.en", device="cuda")


def test_unknown_model_name(monkeypatch, tmp_path):
    fw = pytest.importorskip("faster_whisper")

    def refuse(name, **kw):
        raise ValueError(f"Invalid model size '{name}', expected one of: tiny.en")
    monkeypatch.setattr(fw, "WhisperModel", refuse)
    with pytest.raises(asr.ModelError, match="not a speech model"):
        asr.load_model("huge.en", device="cpu", download_root=str(tmp_path))


def test_download_failure_names_the_fix(monkeypatch, tmp_path):
    fw = pytest.importorskip("faster_whisper")

    def offline(name, **kw):
        raise OSError("Network is unreachable")
    monkeypatch.setattr(fw, "WhisperModel", offline)
    with pytest.raises(asr.ModelError, match="huggingface.co"):
        asr.load_model("tiny.en", device="cpu", download_root=str(tmp_path))


def test_out_of_memory_still_reaches_the_retry(monkeypatch, tmp_path):
    fw = pytest.importorskip("faster_whisper")
    calls = []

    def oom(name, **kw):
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("CUDA failed with error out of memory")
        return "model"
    monkeypatch.setattr(fw, "WhisperModel", oom)
    monkeypatch.setattr(asr, "OOM_WAIT_SECONDS", 0)
    monkeypatch.setattr(asr, "OOM_WAIT_AFTER_EVICTION", 0)
    freed = []
    got = asr._load_with_patience("tiny.en", "cpu", "int8", str(tmp_path),
                                  free_vram=lambda: freed.append(1) or ["m"])
    assert got == "model" and freed


def test_transcripts_are_cached(tmp_path, stub):
    stub.route("POST", "/v1/audio/transcriptions", {
        "words": [{"word": "hello", "start": 0.1, "end": 0.4}], "duration": 0.2})
    audio = wav(tmp_path / "a.wav")
    first = asr.transcribe(audio, remote_url=stub.url, remote_model="m",
                           cache_dir=tmp_path / "cache")
    again = asr.transcribe(audio, remote_url=stub.url, remote_model="m",
                           cache_dir=tmp_path / "cache")
    assert not first.cached and again.cached
    assert again.words == first.words
    assert len(stub.requests) == 1
    # A different engine is a different cache entry.
    asr.transcribe(audio, remote_url=stub.url, remote_model="other",
                   cache_dir=tmp_path / "cache")
    assert len(stub.requests) == 2


def test_a_corrupt_cache_entry_is_ignored(tmp_path, stub):
    stub.route("POST", "/v1/audio/transcriptions", {
        "words": [{"word": "hi", "start": 0, "end": 0.1}]})
    audio = wav(tmp_path / "a.wav")
    cache = tmp_path / "cache"
    asr.transcribe(audio, remote_url=stub.url, remote_model="m", cache_dir=cache)
    for f in cache.glob("*.json"):
        f.write_text("{not json")
    got = asr.transcribe(audio, remote_url=stub.url, remote_model="m", cache_dir=cache)
    assert not got.cached and got.words[0]["word"] == "hi"


def test_remote_request_shape(tmp_path, stub):
    stub.route("POST", "/v1/audio/transcriptions", {
        "words": [{"word": "a", "start": 0, "end": 0.1}]})
    asr.transcribe_remote(wav(tmp_path / "a.wav"), stub.url + "/v1", "m", api_key="sk")
    req = stub.requests[0]
    assert req.headers["authorization"] == "Bearer sk"
    body = req.body.decode("latin1")
    assert 'name="timestamp_granularities[]"' in body and "word" in body
    assert 'name="model"' in body and 'name="file"' in body


def test_remote_without_word_timings_is_refused(tmp_path, stub):
    stub.route("POST", "/v1/audio/transcriptions", {"text": "hello", "segments": []})
    with pytest.raises(asr.RemoteError, match="word-level"):
        asr.transcribe_remote(wav(tmp_path / "a.wav"), stub.url, "m")
    assert asr.probe_remote(stub.url, "m")["ok"] is True     # silence says nothing


def test_remote_errors(tmp_path, stub):
    stub.route("POST", "/v1/audio/transcriptions", lambda req: (401, {"error": "key"}))
    assert "401" in asr.probe_remote(stub.url, "m")["error"]
    assert "could not reach" in asr.probe_remote("http://127.0.0.1:9", "m")["error"]


@pytest.mark.parametrize("url, want", [
    ("http://w:8000", "http://w:8000/v1/audio/transcriptions"),
    ("http://w:8000/", "http://w:8000/v1/audio/transcriptions"),
    ("http://w:8000/v1", "http://w:8000/v1/audio/transcriptions"),
    ("http://w:8000/v1/audio/transcriptions", "http://w:8000/v1/audio/transcriptions"),
])
def test_remote_endpoint(url, want):
    assert asr._remote_endpoint(url) == want


def test_fingerprint_changes_with_content(tmp_path):
    a = wav(tmp_path / "a.wav")
    b = tmp_path / "b.wav"
    b.write_bytes(a.read_bytes()[:-2] + b"\x01\x00")
    assert asr.audio_fingerprint(a) != asr.audio_fingerprint(b)
    assert json.dumps(asr.audio_fingerprint(a))


class _PieceModel:
    """Stands in for WhisperModel: one word half a second into each piece."""

    def __init__(self):
        self.lengths = []

    def transcribe(self, samples, **_options):
        from types import SimpleNamespace
        self.lengths.append(len(samples))
        word = SimpleNamespace(word=" w", start=0.5, end=0.9, probability=0.9)
        segment = SimpleNamespace(end=0.9, words=[word])
        return iter([segment]), SimpleNamespace(language="en",
                                                duration=len(samples) / 16000)


def test_long_audio_is_heard_in_pieces_cut_at_a_gap(monkeypatch, tmp_path):
    """Whisper is handed a long recording a piece at a time, so its memory does
    not grow with the length of the film, and each cut falls in a quiet spot."""
    np = pytest.importorskip("numpy")
    rate = 16000
    t = np.arange(rate * 25)
    samples = (np.sin(t / 5) * 8000).astype(np.int16)
    samples[int(8.5 * rate):int(9.0 * rate)] = 0          # the gap to cut at
    frames = samples.tobytes()
    audio = tmp_path / "long.wav"
    audio.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
                      + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
                      + b"data" + struct.pack("<I", len(frames)) + frames)
    model = _PieceModel()
    monkeypatch.setattr(asr, "_load_with_patience", lambda *a, **k: model)
    monkeypatch.setattr(asr, "PIECE_SECONDS", 10)
    monkeypatch.setattr(asr, "SEAM_SEARCH_SECONDS", 3.0)
    seen = []

    got = asr.transcribe(audio, progress=seen.append)

    assert len(model.lengths) == 3
    assert sum(model.lengths) == len(samples)           # nothing lost or heard twice
    assert 8.5 <= model.lengths[0] / rate <= 9.0        # cut inside the gap
    starts = [w["start"] for w in got.words]
    assert starts[0] == 0.5
    assert starts[1] == pytest.approx(model.lengths[0] / rate + 0.5, abs=0.001)
    assert got.duration == 25.0
    assert seen == sorted(seen) and seen[-1] <= 1.0


def test_a_short_file_is_one_piece(monkeypatch, tmp_path):
    pytest.importorskip("numpy")
    model = _PieceModel()
    monkeypatch.setattr(asr, "_load_with_patience", lambda *a, **k: model)
    got = asr.transcribe(wav(tmp_path / "short.wav", seconds=2))
    assert model.lengths == [32000]
    assert got.words[0]["start"] == 0.5
