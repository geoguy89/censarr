"""Checking a settings save before it is written.

A value the pipeline cannot use - a padding of "abc", an address with no
http:// - used to be saved as given and fail later, inside a job, with an
error that named neither the setting nor the fix. Here it is refused at save
time with a message that names both.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import arr, words

CHOICES = {
    "library_source": ("sonarr", "plex", "jellyfin"),
    "media_server": ("none", "plex", "jellyfin"),
    "asr_backend": ("builtin", "remote"),
    "device": ("auto", "cuda", "cpu"),
    "compute_type": ("auto", "int8", "int8_float16", "float16", "float32"),
    "hold_policy": tuple(arr.HOLD_POLICIES),
}

URLS = {
    "sonarr.url": "Sonarr address",
    "radarr.url": "Radarr address",
    "plex_url": "Plex address",
    "jellyfin_url": "Jellyfin address",
    "asr_url": "Whisper server address",
    "judge_url": "Second-opinion address",
    "recheck_url": "Second Whisper server address",
}

# (lowest, highest) in seconds.
NUMBERS = {
    "pad_start": ("Silence starts early by", 0.0, 2.0),
    "pad_end": ("Silence ends late by", 0.0, 2.0),
    "fade": ("Fade", 0.0, 0.5),
}

INTEGERS = {
    "judge_threads": ("CPU threads for the second opinion", 0, 256),
    "ffmpeg_threads": ("ffmpeg threads", 0, 256),
}

WORD_LISTS = ("custom_words", "allow_words", "check_in_context")
BOOLEANS = ("keep_backup", "trim_silence")

_BITRATE = re.compile(r"^\d+(\.\d+)?[km]?$", re.I)
_KEEP_ALIVE = re.compile(r"^-?\d+(\.\d+)?(ms|s|m|h)?$")


def url(value, label: str) -> tuple[str, str]:
    """(cleaned, error). Blank is allowed: it means "not set"."""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return "", ""
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        return text, f"{label} must start with http:// or https://"
    if not parts.hostname:
        return text, f"{label} has no host name in it"
    try:
        parts.port            # noqa: B018 - raises on a port that is not a number
    except ValueError:
        return text, f"{label} has a port that is not a number"
    return text, ""


def _lines(value) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, (list, tuple)):
        raise TypeError
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def check(payload: dict) -> tuple[dict, dict[str, str]]:
    """(cleaned payload, {field: problem}). Unknown keys pass through untouched."""
    clean = dict(payload)
    errors: dict[str, str] = {}

    # A page loaded before Radarr was dropped still says "arr" for Sonarr.
    if clean.get("library_source") == "arr":
        clean["library_source"] = "sonarr"
    for key, allowed in CHOICES.items():
        if key in clean and clean[key] not in allowed:
            errors[key] = f"{key} must be one of: {', '.join(allowed)}"

    for key, label in URLS.items():
        section, _, name = key.partition(".")
        if name:
            block = payload.get(section)
            if not isinstance(block, dict) or name not in block:
                continue
            fixed, problem = url(block[name], label)
            clean[section] = {**block, name: fixed}
        else:
            if key not in payload:
                continue
            fixed, problem = url(payload[key], label)
            clean[key] = fixed
        if problem:
            errors[key] = problem

    for key, (label, low, high) in NUMBERS.items():
        if key not in payload:
            continue
        try:
            number = float(payload[key])
        except (TypeError, ValueError):
            errors[key] = f"{label} must be a number of seconds"
            continue
        if not low <= number <= high:
            errors[key] = f"{label} must be between {low:g} and {high:g} seconds"
        clean[key] = number

    for key, (label, low, high) in INTEGERS.items():
        if key not in payload:
            continue
        try:
            number = int(payload[key])
        except (TypeError, ValueError):
            errors[key] = f"{label} must be a whole number"
            continue
        if not low <= number <= high:
            errors[key] = f"{label} must be between {low} and {high}"
        clean[key] = number

    for key in ("bitrate_surround", "bitrate_stereo"):
        if key in payload:
            text = str(payload[key] or "").strip()
            if not _BITRATE.match(text):
                errors[key] = "a bitrate looks like 384k"
            clean[key] = text.lower()

    if "judge_keep_alive" in payload:
        text = str(payload["judge_keep_alive"] or "").strip()
        if not _KEEP_ALIVE.match(text):
            errors["judge_keep_alive"] = "keep-alive looks like 30s, 5m or 0"
        clean["judge_keep_alive"] = text

    for key in WORD_LISTS:
        if key in payload:
            try:
                clean[key] = _lines(payload[key])
            except TypeError:
                errors[key] = "must be a list of words"

    if "allow_words_by_title" in payload:
        value = payload["allow_words_by_title"]
        if not isinstance(value, dict):
            errors["allow_words_by_title"] = "must map a show or film to its words"
        else:
            try:
                clean["allow_words_by_title"] = {
                    str(title).strip(): kept for title, listed in value.items()
                    if str(title).strip() and (kept := _lines(listed))}
            except TypeError:
                errors["allow_words_by_title"] = "each show needs a list of words"

    if "categories" in payload:
        try:
            chosen = _lines(payload["categories"])
        except TypeError:
            chosen = None
        if chosen is None or any(c not in words.CATEGORIES for c in chosen):
            errors["categories"] = (
                f"categories must be from: {', '.join(words.CATEGORIES)}")
        else:
            clean["categories"] = chosen

    for key in BOOLEANS:
        if key in payload:
            value = payload[key]
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            clean[key] = bool(value)

    if "track_title" in payload:
        title = str(payload["track_title"] or "").strip()
        if "\n" in title or len(title) > 80:
            errors["track_title"] = "the track name must be one line of at most 80 characters"
        clean["track_title"] = title

    for key in ("judge_model", "asr_remote_model", "model"):
        if key in payload:
            clean[key] = str(payload[key] or "").strip()

    return clean, errors
