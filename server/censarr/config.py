"""Settings, kept in one YAML file under /config so it survives the container."""

from __future__ import annotations

from . import env

import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

# The words a fresh install double-checks in context. Kept here rather than in
# judge.py so config has no import of its own to tangle with.
DEFAULT_CHECK_IN_CONTEXT = {
    "cock", "cocks", "dick", "dicks", "tit", "tits", "coon", "spade",
    "faggot", "jesus", "christ",
}

CONFIG_DIR = Path(env.get("CONFIG", "/config"))
CONFIG_FILE = CONFIG_DIR / "config.yaml"
_lock = threading.Lock()


@dataclass
class ArrConfig:
    url: str = ""
    api_key: str = ""
    enabled: bool = False


@dataclass
class Settings:
    # Where the list of shows and films comes from.
    #
    # "sonarr" is the default: shows from Sonarr, which alone can say what has
    # not aired yet and so fill the Upcoming calendar, and films from the media
    # server. "plex" or "jellyfin" take the shows from the server as well, for
    # people who run nothing else.
    #
    # One of: sonarr | plex | jellyfin. Films always come from the media
    # server - Radarr was dropped, it knew nothing the server does not. The
    # Plex and Jellyfin credentials are the same ones used for refreshing and
    # the transcode hold - there is no second set to keep in step.
    library_source: str = "sonarr"

    sonarr: ArrConfig = field(default_factory=ArrConfig)
    # Optional, and never the film library: films on disk come from the media
    # server. Radarr is asked only what it is still waiting for, so a film can
    # be marked to censor itself the moment it downloads.
    radarr: ArrConfig = field(default_factory=ArrConfig)

    # Which categories are silenced. Set from the first-run questions.
    categories: list[str] = field(
        default_factory=lambda: ["strong", "mild", "blasphemy", "slurs_sexual"])
    custom_words: list[str] = field(default_factory=list)
    allow_words: list[str] = field(default_factory=list)   # never silence these
    # Never silence these, in one show or film only - keyed by its name, which
    # is what every job for it is titled. A character called Dick is a reason
    # to leave "dick" alone in that show, not everywhere.
    allow_words_by_title: dict[str, list[str]] = field(default_factory=dict)

    # Muting
    pad_start: float = 0.12
    pad_end: float = 0.12
    fade: float = 0.02

    # Listening
    #
    # builtin: the model runs inside this container, on this machine's GPU.
    # remote:  the audio is sent to a Whisper server you already run, and this
    #          container needs no GPU and downloads no model. The server has to
    #          return WORD-level timestamps - see asr.py - so it must be an
    #          OpenAI-compatible one (speaches, faster-whisper-server, LocalAI).
    asr_backend: str = "builtin"
    asr_url: str = ""
    asr_api_key: str = ""
    # What to ask the remote server for. Its names are its own - often
    # "Systran/faster-whisper-medium.en" or just "whisper-1".
    asr_remote_model: str = "Systran/faster-whisper-medium.en"

    model: str = "medium.en"
    device: str = "auto"
    compute_type: str = "auto"
    # Whisper's voice-activity filter, which throws away what it judges to be
    # silence before listening. Off: measured on one episode it skipped 19 real
    # profanities and saved no time at all doing it.
    trim_silence: bool = False

    # The second opinion on ambiguous words - see judge.py. Empty URL turns it
    # off, and then every ambiguous word is muted.
    # Blank by default: the second opinion is optional, most people should
    # leave it off, and a default pointing at somebody else's network
    # would be both useless and rude.
    judge_url: str = ""
    judge_model: str = "qwen3.5:9b"
    # How many CPU threads Ollama may use for our questions, and how long it
    # keeps the model in VRAM afterwards. Both exist to keep the server usable
    # while a file is being cleaned - measured numbers are in the README.
    judge_threads: int = 2
    judge_keep_alive: str = "30s"
    # Which words get the second opinion instead of being muted outright.
    # The list means exactly what it says, including when it is empty: no
    # words checked, no model involved. ("Empty means the built-in default"
    # was the first design and it made clearing the box impossible.)
    check_in_context: list[str] = field(
        default_factory=lambda: sorted(DEFAULT_CHECK_IN_CONTEXT))
    # The subtitles are always the first check (see subtitles.py), and the
    # media server is always asked to search for them when a file has none.
    # What is optional is a second opinion on a word the subtitles would leave
    # in because it sounds like the swear: another Whisper server listening to
    # that moment again, or the model above.
    recheck_url: str = ""
    recheck_model: str = "Systran/faster-whisper-large-v3"
    recheck_api_key: str = ""

    # Bitrate for the cleaned track, per channel layout. The cleaned track is
    # a re-encode either way; this decides how much the file grows.
    bitrate_surround: str = "384k"
    bitrate_stereo: str = "192k"
    # ffmpeg is given a thread cap for the same reason as Ollama: a cleaning
    # job should never be the reason a film stops playing.
    ffmpeg_threads: int = 2

    # Files
    keep_backup: bool = False
    # What the added track is called. Changing it names the tracks written
    # from then on; the ones already written keep the name they were given.
    track_title: str = "Censored - English"
    # Every name this install has used before the current one. Kept so a file
    # cleaned under an older name is still recognised - otherwise renaming the
    # track would mean re-cleaning adds a second one and removing finds none.
    known_track_titles: list[str] = field(default_factory=list)

    # Plex is told to look again once a file changes, so the new track appears
    # without waiting for a scheduled scan.
    # Which media server to tell about a changed file, and to wait for.
    # One of: none | plex | jellyfin. Neither is required - the cleaned track
    # appears either way, just after that server next scans on its own.
    media_server: str = "none"
    plex_url: str = ""
    plex_token: str = ""
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    # When to hold the queue for Plex. Only a VIDEO transcode competes for the
    # GPU that transcribing and judging use; a direct play costs the server
    # nothing, so waiting for one would be waiting for no reason.
    # One of: never | video_transcode | any_transcode | playing
    hold_policy: str = "video_transcode"

    # Who may use the web interface.
    #
    # Empty username means no login at all, which is the default and is right
    # for a box on your own LAN - demanding a password before the thing will
    # show you anything is a poor first five minutes. Set a username and it is
    # on from the next request, no restart.
    auth_user: str = ""
    auth_hash: str = ""
    auth_salt: str = ""
    # Signs session cookies. Rotating it signs everybody out, which is how
    # "log out everywhere" works.
    auth_secret: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def public(self) -> dict:
        """For the UI: everything except the secrets themselves."""
        data = self.to_dict()
        if data["sonarr"].get("api_key"):
            data["sonarr"]["api_key"] = "********"
        if data["radarr"].get("api_key"):
            data["radarr"]["api_key"] = "********"
        if data.get("plex_token"):
            data["plex_token"] = "********"
        if data.get("asr_api_key"):
            data["asr_api_key"] = "********"
        if data.get("recheck_api_key"):
            data["recheck_api_key"] = "********"
        if data.get("jellyfin_api_key"):
            data["jellyfin_api_key"] = "********"
        # The hash, its salt and the cookie key never leave the server. The
        # username is fine to show - the settings page displays who is signed
        # in - but everything that could be used to forge a session is dropped
        # rather than masked, so it cannot be echoed back by a careless save.
        for secret in ("auth_hash", "auth_salt", "auth_secret"):
            data.pop(secret, None)
        data["auth_enabled"] = bool(self.auth_user)
        return data


def load() -> Settings:
    if not CONFIG_FILE.exists():
        return Settings()
    raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf8")) or {}
    settings = Settings()
    # The old boolean became a policy once "someone is watching" turned out to
    # be the wrong question - a direct play needs nothing from this machine.
    if "pause_while_plex_playing" in raw and "hold_policy" not in raw:
        raw["hold_policy"] = ("video_transcode" if raw.pop("pause_while_plex_playing")
                              else "never")
    raw.pop("pause_while_plex_playing", None)
    # Path rewriting was removed: the media has to be mounted here either way,
    # and mounting it at the path the library reports is the whole fix. An old
    # config file keeps the key until the next save, when it is dropped.
    raw.pop("path_map", None)
    # media_server arrived when Jellyfin did. An install that already had a
    # Plex address configured meant Plex, so say so rather than silently
    # switching it off.
    if "media_server" not in raw and raw.get("plex_url"):
        raw["media_server"] = "plex"
    # "arr" meant shows from Sonarr and films from Radarr. Films now always
    # come from the media server, so it becomes Sonarr; an old Radarr address
    # is kept, since it is used again for films not downloaded yet. With no
    # media server chosen, films will not show, and the setup checklist says so.
    if raw.get("library_source") == "arr":
        raw["library_source"] = "sonarr"
    # The default track name changed with the rename. A config that saved the
    # old default switches to the new one; the old name stays recognised, so a
    # re-censor replaces that track rather than adding a second.
    if raw.get("track_title") == "Cleaned - English":
        raw["track_title"] = "Censored - English"
        known = list(raw.get("known_track_titles") or [])
        if "Cleaned - English" not in known:
            raw["known_track_titles"] = [*known, "Cleaned - English"]
    for key, value in raw.items():
        if key in ("sonarr", "radarr") and isinstance(value, dict):
            known = {k: v for k, v in value.items() if k in ("url", "api_key", "enabled")}
            setattr(settings, key, ArrConfig(**known))
        elif hasattr(settings, key):
            setattr(settings, key, value)
    return settings


def save(settings: Settings) -> None:
    with _lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(settings.to_dict(), sort_keys=False),
                       encoding="utf8")
        tmp.replace(CONFIG_FILE)

