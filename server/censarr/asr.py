"""Listening to the episode.

faster-whisper with word timestamps. Word-level timing is the whole point: a
line-level subtitle says a swear happened somewhere in four seconds of speech,
which is four seconds of silence in the cleaned track. Words give ~50ms.

Transcripts are cached against the audio's content hash, so changing the word
list or the padding re-cleans a file without listening to it again - the slow
part runs once per episode, not once per attempt.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_model = None
_model_key: tuple | None = None
_lock = threading.Lock()


@dataclass
class Transcript:
    words: list[dict]
    language: str
    duration: float
    model: str
    cached: bool = False

    def to_json(self) -> dict:
        return {"words": self.words, "language": self.language,
                "duration": self.duration, "model": self.model}


def audio_fingerprint(path: Path) -> str:
    """Identify the audio without reading gigabytes: size plus a sample of the
    middle. Two different episodes colliding on this is not realistic, and a
    re-encode changes it, which is what matters."""
    st = path.stat()
    h = hashlib.sha256(f"{st.st_size}".encode())
    with path.open("rb") as fh:
        for offset in (0, max(0, st.st_size // 2), max(0, st.st_size - 65536)):
            fh.seek(offset)
            h.update(fh.read(65536))
    return h.hexdigest()[:32]


def load_model(name: str, device: str = "auto", compute_type: str = "auto",
               download_root: str | None = None):
    """One model, held between jobs. Loading costs seconds and a GB of VRAM."""
    global _model, _model_key
    from faster_whisper import WhisperModel  # imported late: heavy, and absent in tests

    if device == "auto":
        device = "cuda" if _cuda_available() else "cpu"
    elif device == "cuda" and not _cuda_available():
        raise ModelError(
            "Settings → Listening says to use the NVIDIA GPU, but this container "
            "can see none. Choose \"Whatever is available\" or CPU, or give the "
            "container the GPU (see Hardware in the README)")
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    key = (name, device, compute_type)
    with _lock:
        if _model is None or _model_key != key:
            try:
                _model = WhisperModel(name, device=device, compute_type=compute_type,
                                      download_root=download_root)
            except RuntimeError:
                raise          # out of memory: _load_with_patience handles it
            except ValueError as exc:
                if "invalid model size" in str(exc).lower():
                    raise ModelError(
                        f"“{name}” is not a speech model faster-whisper knows. "
                        f"Set model: in config.yaml to medium.en") from exc
                raise ModelError(_load_failed(name, exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise ModelError(_load_failed(name, exc)) from exc
            _model_key = key
    return _model


class ModelError(Exception):
    """The speech model cannot be loaded, for a reason a person can fix."""


def _load_failed(name: str, exc: Exception) -> str:
    return (f"could not load the speech model {name} ({type(exc).__name__}: {exc}). "
            f"If it is not downloaded yet, this container has to reach "
            f"huggingface.co - or download it first under Settings → Listening")


def _cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def unload_model() -> None:
    """Give the VRAM back. The same GPU also runs Plex transcodes and Ollama,
    so holding 2GB between jobs is not neighbourly."""
    global _model, _model_key
    with _lock:
        _model = None
        _model_key = None
    try:
        import gc
        gc.collect()
    except Exception:  # noqa: BLE001
        pass


OOM_RETRIES = 4
OOM_WAIT_SECONDS = 45
OOM_WAIT_AFTER_EVICTION = 6     # freeing VRAM takes a moment, not a minute


def _load_with_patience(name: str, device: str, compute_type: str,
                        download_root: str | None, free_vram=None):
    """Load the model, clearing the card if something else has filled it.

    The GPU is shared with Plex and Ollama. Waiting alone was the first answer
    and it was wrong: a Plex transcode does finish, but an Ollama model sits
    resident for as long as its keep-alive says, and the OCR watcher asks for
    hours. Three 45-second waits against a four-hour lease is just a slower
    way to fail, which is exactly how it failed.

    So on "out of memory" we ask Ollama to let go (`free_vram`) and try again
    straight away; that costs Ollama a few seconds of reloading next time it is
    asked for something. If nothing could be freed, the cause is more likely a
    transcode, and then waiting really is the right move.
    """
    last: Exception | None = None
    for attempt in range(OOM_RETRIES):
        try:
            return load_model(name, device, compute_type, download_root)
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower():
                raise
            last = exc
            unload_model()
            freed = []
            if free_vram is not None:
                try:
                    freed = free_vram() or []
                except Exception as err:    # noqa: BLE001
                    print(f"[censarr] could not ask Ollama to free the card: {err}",
                          flush=True)
            wait = OOM_WAIT_AFTER_EVICTION if freed else OOM_WAIT_SECONDS
            print(f"[censarr] GPU full"
                  + (f", unloaded {', '.join(freed)}" if freed else "")
                  + f"; retrying in {wait}s "
                  f"(attempt {attempt + 1} of {OOM_RETRIES})", flush=True)
            time.sleep(wait)
    raise last if last else RuntimeError("could not load the speech model")



# ---------------------------------------------------------------------------
#  Somebody else's Whisper
# ---------------------------------------------------------------------------
#  Not everyone wants a 4GB CUDA image and a 1.5GB model on the box running
#  this. Plenty of people already have a Whisper server - speaches,
#  faster-whisper-server, LocalAI - and would rather point at it.
#
#  The hard requirement is WORD-level timestamps. Everything downstream mutes
#  half a second around one word; given only segment timestamps the best it
#  could do is blank four seconds of dialogue to catch one syllable, which is
#  the thing this project exists to avoid. So the remote must speak the OpenAI
#  transcription API with `timestamp_granularities[]=word`, and a server that
#  answers without word timings is rejected loudly rather than quietly
#  degraded.

class RemoteError(RuntimeError):
    pass


def _remote_endpoint(url: str) -> str:
    """Accept whatever form of the address someone pastes in.

    People paste the base address, or the /v1, or the whole path. All three
    should work rather than producing a 404 they have to guess at.
    """
    base = url.strip().rstrip("/")
    if base.endswith("/audio/transcriptions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/audio/transcriptions"
    return f"{base}/v1/audio/transcriptions"


def transcribe_remote(audio: Path, url: str, model: str, api_key: str = "",
                      timeout: float = 3600.0) -> Transcript:
    """Hand the audio to a Whisper server and get words with timings back."""
    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    # An hour: a feature film on a modest server genuinely takes that long, and
    # a timeout in the middle throws away all the work done so far.
    with audio.open("rb") as handle:
        files = {"file": (audio.name, handle, "audio/wav")}
        data = {
            "model": model,
            "language": "en",
            "response_format": "verbose_json",
            # A list value, which httpx emits as a repeated field. That is how
            # the OpenAI API spells an array in multipart, and servers copying
            # it expect the same.
            #
            # This was a list of 2-tuples, which httpx does not accept
            # alongside `files=` - it raised TypeError before the request was
            # ever sent, so pointing Cens-arr (then Cleanarr) at a Whisper server failed every
            # time with a message about bytes-like objects.
            "timestamp_granularities[]": ["word"],
        }
        try:
            resp = httpx.post(_remote_endpoint(url), files=files, data=data,
                              headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise RemoteError(f"could not reach the Whisper server: {exc}") from exc

    if resp.status_code >= 400:
        raise RemoteError(f"the Whisper server said {resp.status_code}: "
                          f"{resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise RemoteError("the Whisper server did not return JSON") from exc

    words = body.get("words")
    if not words:
        # Some servers accept the parameter, ignore it, and return segments.
        # Falling back to segment timings would silently make every mute four
        # seconds long, so this stops instead.
        raise RemoteError(
            "the server returned no word-level timestamps. Cens-arr needs them "
            "to mute single words - check it supports "
            "timestamp_granularities[]=word")

    out = []
    for w in words:
        row = {"word": str(w.get("word", "")),
               "start": round(float(w.get("start", 0.0)), 3),
               "end": round(float(w.get("end", 0.0)), 3)}
        # The OpenAI API has no per-word confidence; some servers add one.
        sure = w.get("probability", w.get("confidence"))
        if isinstance(sure, (int, float)):
            row["probability"] = round(float(sure), 3)
        out.append(row)
    duration = float(body.get("duration") or (out[-1]["end"] if out else 0.0))
    return Transcript(words=out, language=str(body.get("language", "en")),
                      duration=duration, model=f"remote:{model}")


def probe_remote(url: str, model: str, api_key: str = "") -> dict:
    """Is it there, and does it give word timings? Used by the Test button.

    Sends a second of silence rather than trusting a capabilities endpoint,
    because whether the server honours timestamp_granularities is exactly the
    thing that cannot be taken on trust.
    """
    import struct
    import tempfile

    rate, seconds = 16000, 1
    with tempfile.TemporaryDirectory() as folder:
        sample = Path(folder) / "probe.wav"
        frames = b"\x00\x00" * rate * seconds
        header = (b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
                  + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
                  + b"data" + struct.pack("<I", len(frames)))
        sample.write_bytes(header + frames)
        try:
            transcribe_remote(sample, url, model, api_key, timeout=120.0)
        except RemoteError as exc:
            message = str(exc)
            # Silence legitimately transcribes to nothing, so "no words" here
            # means it worked - it just had nothing to say.
            if "no word-level timestamps" in message:
                return {"ok": True, "note": "reachable; sent silence so it had "
                                            "nothing to transcribe"}
            return {"ok": False, "error": message}
    return {"ok": True, "note": "reachable, and it returns word timings"}


# ---------------------------------------------------------------------------
#  A film in pieces
# ---------------------------------------------------------------------------
#  faster-whisper turns the whole recording into a spectrogram before it
#  hears a word, and on the way it holds several full-length copies of it at
#  once - the framed samples, a double-precision FFT and its single-precision
#  copy. For a 64-minute episode that is about 3 GB; for a 2h20m film it
#  passed 6 GB within seconds and the kernel killed the container. Handing it
#  ten minutes at a time keeps that under half a gigabyte whatever the length.
#
#  Each piece ends at the quietest moment near its end rather than at an exact
#  time, so a cut does not land in the middle of a word.

PIECE_SECONDS = 600
SEAM_SEARCH_SECONDS = 20.0     # how far back from a piece's end to look for a gap
SEAM_WINDOW_SECONDS = 0.1      # the loudness is compared over windows this long


def _pieces(audio: Path, piece_seconds: float | None = None):
    """(offset in seconds, float32 samples, whole length in seconds) for each
    piece of a 16-bit mono WAV, or None for anything else."""
    import wave

    piece_seconds = piece_seconds or PIECE_SECONDS

    try:
        handle = wave.open(str(audio), "rb")
    except (wave.Error, EOFError, OSError):
        return None
    if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
        handle.close()
        return None
    return _read_pieces(handle, piece_seconds)


def _read_pieces(handle, piece_seconds: float):
    import numpy as np

    rate = handle.getframerate()
    total = handle.getnframes()
    length = total / rate
    piece = int(piece_seconds * rate)
    window = max(1, int(SEAM_WINDOW_SECONDS * rate))
    search = int(SEAM_SEARCH_SECONDS * rate)
    start = 0
    with handle:
        while start < total:
            handle.setpos(start)
            # The last piece takes whatever is left, rather than leaving a
            # few seconds to be heard on their own.
            want = total - start if total - start <= piece + search else piece
            block = np.frombuffer(handle.readframes(want), dtype=np.int16)
            if not len(block):
                break
            cut = len(block)
            if start + cut < total and cut > search:
                tail = block[cut - search:].astype(np.float32)
                usable = len(tail) // window * window
                loudness = np.abs(tail[:usable]).reshape(-1, window).mean(axis=1)
                quiet = int(np.argmin(loudness))
                cut = cut - search + quiet * window + window // 2
            samples = block[:cut].astype(np.float32) / 32768.0
            yield start / rate, samples, length
            start += int(cut)


def transcribe(audio: Path, *, model_name: str = "medium.en",
               device: str = "auto", compute_type: str = "auto",
               trim_silence: bool = False, free_vram=None,
               remote_url: str = "", remote_model: str = "", remote_key: str = "",
               cache_dir: Path | None = None, download_root: str | None = None,
               progress=None) -> Transcript:
    """Words with timestamps, from cache when we have heard this audio before.

    `trim_silence` is Whisper's voice-activity filter, which drops the passages
    it judges to be silent before transcribing. It is off by default because it
    is not free: measured on one 43-minute episode it lost 19 real profanities
    - five "fucking", a "bitch", three "hell" - for no saving at all (1.9 min
    either way). The cache key changes with it, so a file heard one way is
    never served from the other.
    """
    cache_file = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        variant = "" if trim_silence else ".full"
        # The engine is part of the key: a transcript made by a remote
        # server is not interchangeable with one made here, and serving
        # the wrong one after a backend change would be baffling.
        engine = f"remote-{remote_model}" if remote_url else model_name
        cache_file = cache_dir / f"{audio_fingerprint(audio)}.{engine}{variant}.json"
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text(encoding="utf8"))
                return Transcript(cached=True, **data)
            except (json.JSONDecodeError, TypeError, ValueError):
                cache_file.unlink(missing_ok=True)

    # A remote server does the listening instead, and then none of the local
    # model, the GPU or the VRAM juggling below is involved at all.
    if remote_url:
        transcript = transcribe_remote(audio, remote_url, remote_model, remote_key)
        if cache_file:
            cache_file.write_text(json.dumps(transcript.to_json()), encoding="utf8")
        return transcript

    model = _load_with_patience(model_name, device, compute_type, download_root,
                                free_vram=free_vram)
    options = dict(
        language="en",
        word_timestamps=True,
        vad_filter=trim_silence,
        vad_parameters={"min_silence_duration_ms": 500} if trim_silence else None,
        beam_size=5,
        condition_on_previous_text=False,
    )

    words: list[dict] = []
    language = "en"
    pieces = _pieces(audio)
    if pieces is None:
        # Not a WAV this can read in pieces: hand Whisper the file whole.
        pieces = iter([(0.0, str(audio), 0.0)])
    duration = 0.0
    for offset, samples, total in pieces:
        segments, info = model.transcribe(samples, **options)
        language = str(getattr(info, "language", language) or language)
        duration = total or float(getattr(info, "duration", 0.0) or 0.0)
        for segment in segments:  # generator: this is where the time goes
            for w in (segment.words or []):
                words.append({"word": w.word,
                              "start": round(float(w.start) + offset, 3),
                              "end": round(float(w.end) + offset, 3),
                              "probability": round(float(getattr(w, "probability", 1.0)), 3)})
            if progress and duration:
                progress(min(1.0, (float(segment.end) + offset) / duration))

    transcript = Transcript(words=words, language=language,
                            duration=duration, model=model_name)
    if cache_file:
        cache_file.write_text(json.dumps(transcript.to_json()), encoding="utf8")
    return transcript
