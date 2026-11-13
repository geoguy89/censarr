"""Sonarr, read-only, plus the media-server calls (refresh, sessions).

Nothing here writes to Sonarr. This is a browser: it asks what you
have and where the files are, and everything after that happens in our own
queue. A bug in this file can waste time; it cannot rename your library.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

import httpx

TIMEOUT = 20.0


class ArrError(RuntimeError):
    pass


def _jf_headers(api_key: str) -> dict:
    """See library.jellyfin_headers - Jellyfin 12 only takes the
    Authorization form, older servers take either."""
    from .library import jellyfin_headers
    return jellyfin_headers(api_key)


@dataclass
class Client:
    url: str
    api_key: str

    @property
    def name(self) -> str:
        return type(self).__name__ if type(self) is not Client else "The service"

    def _get(self, path: str, **params):
        if not self.url or not self.api_key:
            raise ArrError(f"{self.name} address and API key are not set")
        base = self.url.rstrip("/")
        try:
            resp = httpx.get(f"{base}/api/v3/{path}", params=params,
                             headers={"X-Api-Key": self.api_key}, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            raise ArrError(f"could not reach {base}: {exc}") from exc
        if resp.status_code == 401:
            raise ArrError(f"{self.name} rejected the API key")
        if resp.status_code == 404:
            raise ArrError(f"{self.name} answered 404 at {base}/api/v3/{path} - check "
                           f"the address, including any URL base")
        if resp.status_code >= 400:
            raise ArrError(f"{resp.status_code} from {base}/api/v3/{path}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ArrError(f"{base} did not answer like {self.name} - check the "
                           f"address") from exc

    def test(self) -> dict:
        status = self._get("system/status")
        return {"ok": True, "app": status.get("appName", ""),
                "version": status.get("version", "")}

    def root_folders(self) -> list[dict]:
        """Library roots, as this app sees them.

        Used to show, side by side, where the library says the files are and
        where this container can actually reach them.
        """
        try:
            return [{"path": r.get("path", "")} for r in (self._get("rootfolder") or [])]
        except ArrError:
            return []


class Sonarr(Client):
    def series(self) -> list[dict]:
        """Every show Sonarr knows about, including ones with nothing on disk.

        A show that has not aired yet has no files and so nothing to clean
        today - but it is exactly the show worth marking now, so that the
        episodes clean themselves as they arrive.
        """
        return [
            {"id": s["id"], "title": s.get("title", ""),
             "year": s.get("year"), "path": s.get("path", ""),
             "episodes": (s.get("statistics") or {}).get("episodeFileCount", 0),
             "status": s.get("status", ""),
             # When the show was added to Sonarr. Only a fallback for sorting:
             # recent_imports() knows when an episode actually landed.
             "added": s.get("added", ""),
             "poster": _poster(s)}
            for s in self._get("series")
        ]

    def recent_imports(self, limit: int = 60) -> list[dict]:
        """The episodes most recently imported, newest first.

        Sonarr's history is the only record of when a file actually landed
        that can be had in one request - `series` carries no such date, and
        asking each of a thousand shows separately is not an option.
        """
        data = self._get("history", page=1, pageSize=max(limit * 3, 60),
                         sortKey="date", sortDirection="descending",
                         eventType=3,          # downloadFolderImported
                         includeSeries="true", includeEpisode="true")
        out: list[dict] = []
        seen: set[int] = set()
        for record in data.get("records") or []:
            path = (record.get("data") or {}).get("importedPath") or ""
            episode = record.get("episode") or {}
            show = record.get("series") or {}
            key = record.get("episodeId")
            # An upgrade imports the same episode again; keep only the newest.
            if not path or key in seen:
                continue
            seen.add(key)
            out.append({
                "series_id": record.get("seriesId"),
                "episode_id": key,
                "series": show.get("title", ""),
                "season": episode.get("seasonNumber", 0),
                "episode": episode.get("episodeNumber", 0),
                "title": episode.get("title", ""),
                "path": path,
                "added": record.get("date", ""),
                "size": int((record.get("data") or {}).get("size") or 0),
                "quality": ((record.get("quality") or {}).get("quality") or {}).get("name", ""),
            })
            if len(out) >= limit:
                break
        return out

    def calendar(self, days: int = 21, back: int = 1) -> list[dict]:
        """What is due to air, soonest first.

        A day of history is included on purpose: something that aired last
        night and has not been grabbed yet is exactly what you came to look
        for, and it drops off the list the moment Sonarr imports it.
        """
        today = _dt.date.today()
        rows = self._get("calendar",
                         start=(today - _dt.timedelta(days=back)).isoformat(),
                         end=(today + _dt.timedelta(days=days)).isoformat(),
                         includeSeries="true")
        out = []
        for row in rows:
            show = row.get("series") or {}
            out.append({
                "series_id": row.get("seriesId"),
                "episode_id": row.get("id"),
                "file_id": row.get("episodeFileId") or 0,
                "series": show.get("title", ""),
                "network": show.get("network", ""),
                "season": row.get("seasonNumber", 0),
                "episode": row.get("episodeNumber", 0),
                # Sonarr writes "TBA" for an episode whose title is not known
                # yet; better to show nothing than to show that.
                "title": "" if row.get("title") in (None, "TBA") else row["title"],
                "airs": row.get("airDateUtc", "") or row.get("airDate", ""),
                "runtime": row.get("runtime", 0),
                "has_file": bool(row.get("hasFile")),
                "wanted": bool(row.get("monitored")),
                "downloaded_at": "",     # filled in below
            })
        out.sort(key=lambda e: (e["airs"], e["series"], e["season"], e["episode"]))
        self._attach_download_dates(out)
        return out

    def _attach_download_dates(self, episodes: list[dict]) -> None:
        """Say when each already-downloaded episode actually landed.

        The calendar tells you what airs and whether a file exists, but not
        when it arrived - and "it aired Tuesday, it was grabbed Tuesday night"
        is the thing you want to see when half the list is already downloaded.

        Fetched in one request for the whole page rather than one per episode.
        Sonarr wants the ids as a REPEATED parameter; a comma-separated list
        is rejected with a 400, which is why this passes a list to httpx.
        """
        ids = [e["file_id"] for e in episodes if e["file_id"]]
        if not ids:
            return
        dates: dict[int, str] = {}
        # Chunked: the ids go in the query string, and a two-month calendar can
        # name several hundred files.
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            try:
                for f in self._get("episodefile", episodeFileIds=chunk):
                    if f.get("id"):
                        dates[f["id"]] = f.get("dateAdded", "")
            except ArrError:
                return          # a missing date is not worth failing the page
        for episode in episodes:
            episode["downloaded_at"] = dates.get(episode["file_id"], "")

    def latest_import_dates(self, pages: int = 2) -> dict[int, str]:
        """{series id: when its newest file landed}, for sorting the poster wall.

        Deliberately lighter than recent_imports(): no series or episode bodies,
        just the date, because this covers hundreds of rows and only ever
        answers "which show got something most recently".
        """
        newest: dict[int, str] = {}
        for page in range(1, pages + 1):
            data = self._get("history", page=page, pageSize=500, sortKey="date",
                             sortDirection="descending", eventType=3)
            records = data.get("records") or []
            for record in records:
                series_id = record.get("seriesId")
                if series_id is not None and series_id not in newest:
                    newest[series_id] = record.get("date", "")
            if len(records) < 500:
                break
        return newest

    def episodes(self, series_id: int) -> list[dict]:
        episodes = self._get("episode", seriesId=series_id)
        files = {f["id"]: f for f in self._get("episodefile", seriesId=series_id)}
        out = []
        for ep in episodes:
            f = files.get(ep.get("episodeFileId") or -1)
            if not f:
                continue
            out.append({
                "id": ep["id"],
                "season": ep.get("seasonNumber", 0),
                "episode": ep.get("episodeNumber", 0),
                "title": ep.get("title", ""),
                "path": f.get("path", ""),
                "size": f.get("size", 0),
                "quality": ((f.get("quality") or {}).get("quality") or {}).get("name", ""),
                # When the file landed, so a show can be set to clean only what
                # arrives from now on rather than its whole back catalogue.
                "added": f.get("dateAdded", ""),
            })
        out.sort(key=lambda e: (e["season"], e["episode"]))
        return out


class Radarr(Client):
    """Asked only about films that are not downloaded yet. Films on disk come
    from the media server, which knows every version and its subtitles."""

    @staticmethod
    def _release(m: dict) -> tuple[str, str]:
        """The release worth knowing about: the next one to come, else the
        latest that has been. (date, what kind)."""
        today = _dt.date.today().isoformat()
        dates = sorted((str(m[k])[:10], label) for k, label in (
            ("inCinemas", "in cinemas"), ("digitalRelease", "digital"),
            ("physicalRelease", "on disc")) if m.get(k))
        coming = [d for d in dates if d[0] >= today]
        return coming[0] if coming else (dates[-1] if dates else ("", ""))

    def wanted(self) -> list[dict]:
        """Every film Radarr has with no file yet, soonest release first."""
        out = []
        for m in self._get("movie") or []:
            if m.get("hasFile"):
                continue
            released, kind = self._release(m)
            out.append({
                "id": m.get("id"), "title": m.get("title", ""), "year": m.get("year"),
                "status": m.get("status", ""),       # announced | inCinemas | released
                "wanted": bool(m.get("monitored")),  # Radarr is looking for it
                "available": bool(m.get("isAvailable")),
                "released": released, "release_kind": kind,
                "added": m.get("added", ""),
            })
        out.sort(key=lambda m: (m["released"] or "9999", (m["title"] or "").lower()))
        return out

    def calendar(self, days: int = 21, back: int = 1) -> list[dict]:
        """Films whose digital release falls in the window - the day a film
        can actually be downloaded, so the day it will need censoring. A
        cinema or disc date says nothing about that. Only the films Radarr is
        looking for, as Sonarr's calendar shows only wanted shows."""
        today = _dt.date.today()
        start = (today - _dt.timedelta(days=back)).isoformat()
        end = (today + _dt.timedelta(days=days)).isoformat()
        rows = self._get("calendar", start=start, end=end, unmonitored="false")
        out = []
        for m in rows or []:
            day = str(m.get("digitalRelease") or "")[:10]
            if not day or not start <= day <= end:
                continue
            out.append({"id": m.get("id"), "title": m.get("title", ""),
                        "year": m.get("year"), "airs": day, "release_kind": "digital release",
                        "has_file": bool(m.get("hasFile")),
                        "wanted": bool(m.get("monitored", True))})
        out.sort(key=lambda f: (f["airs"], (f["title"] or "").lower()))
        return out

    def movie(self, movie_id) -> dict:
        """Whether one film has a file now, and where Radarr says it is."""
        m = self._get(f"movie/{movie_id}") or {}
        file = m.get("movieFile") or {}
        path = file.get("path") or ""
        if not path and m.get("path") and file.get("relativePath"):
            path = f"{str(m['path']).rstrip('/')}/{file['relativePath']}"
        return {"id": m.get("id"), "title": m.get("title", ""), "year": m.get("year"),
                "has_file": bool(m.get("hasFile")) and bool(path), "path": path}


def _poster(item: dict) -> str:
    for image in item.get("images") or []:
        if image.get("coverType") == "poster":
            return image.get("remoteUrl") or image.get("url") or ""
    return ""


@dataclass
class PlexSession:
    who: str
    what: str
    state: str
    video_decision: str     # transcode | copy | directplay
    audio_decision: str
    hardware: bool

    @property
    def transcoding_video(self) -> bool:
        return self.video_decision == "transcode"

    @property
    def transcoding(self) -> bool:
        return "transcode" in (self.video_decision, self.audio_decision)

    def describe(self) -> str:
        if self.transcoding_video:
            how = "transcoding video" + (" on the GPU" if self.hardware else "")
        elif self.transcoding:
            how = "transcoding audio only"
        else:
            how = "direct playing"
        return f"{self.who} is watching {self.what} ({how})"


def plex_sessions(plex_url: str, token: str) -> list[PlexSession]:
    """Who is watching what, and whether Plex is working to deliver it.

    A direct play costs the server nothing but disk reads, so it is no reason
    to stop cleaning. Only a video transcode competes for the GPU, and only
    that is worth holding a queue for.
    """
    if not plex_url or not token:
        return []
    try:
        resp = httpx.get(f"{plex_url.rstrip('/')}/status/sessions",
                         headers={"X-Plex-Token": token, "Accept": "application/json"},
                         timeout=10.0)
        resp.raise_for_status()
        container = (resp.json() or {}).get("MediaContainer") or {}
    except (httpx.HTTPError, ValueError):
        return []   # a check that cannot run is not a reason to stop working

    out: list[PlexSession] = []
    for item in container.get("Metadata") or []:
        state = str(((item.get("Player") or {}).get("state") or "")).lower()
        if state == "paused":
            continue
        ts = item.get("TranscodeSession") or {}
        video = str(ts.get("videoDecision") or "").lower()
        audio = str(ts.get("audioDecision") or "").lower()
        if not ts:
            # No transcode session at all: Plex is handing over the file as-is.
            for media in item.get("Media") or []:
                for part in media.get("Part") or []:
                    video = video or str(part.get("decision") or "directplay").lower()
        title = item.get("grandparentTitle") or item.get("title") or "something"
        if item.get("grandparentTitle") and item.get("title"):
            title = f"{item['grandparentTitle']} - {item['title']}"
        out.append(PlexSession(
            who=(item.get("User") or {}).get("title", "someone"),
            what=title, state=state or "playing",
            video_decision=video or "directplay",
            audio_decision=audio or "copy",
            hardware=bool(ts.get("transcodeHwEncoding") or ts.get("transcodeHwDecoding")),
        ))
    return out


# What counts as a reason to wait.
# The policies read back the name of whichever server is configured. They used
# to say "Plex" regardless, which on a Jellyfin install looked like the option
# simply was not there - the behaviour was always server-agnostic, only the
# wording was not.
HOLD_POLICIES = {
    "never": "Never wait - censor whenever there is work",
    "video_transcode": "Wait only while {server} is transcoding video (it needs the GPU)",
    "any_transcode": "Wait while {server} is transcoding anything",
    "playing": "Wait while anything at all is playing",
}

SERVER_NAMES = {"plex": "Plex", "jellyfin": "Jellyfin", "none": "your media server"}


def hold_policies(media_server: str = "none") -> list[dict]:
    """The wait options, named for the server actually in use."""
    name = SERVER_NAMES.get(media_server, SERVER_NAMES["none"])
    return [{"key": key, "label": label.format(server=name)}
            for key, label in HOLD_POLICIES.items()]


def should_hold(sessions: list[PlexSession], policy: str) -> tuple[bool, str]:
    """Whether to hold the queue, and the sentence explaining it."""
    if policy == "never" or not sessions:
        return False, ""
    if policy == "playing":
        blocking = sessions
    elif policy == "any_transcode":
        blocking = [s for s in sessions if s.transcoding]
    else:
        blocking = [s for s in sessions if s.transcoding_video]
    if not blocking:
        return False, ""
    extra = f" (+{len(blocking) - 1} more)" if len(blocking) > 1 else ""
    return True, blocking[0].describe() + extra


def plex_refresh(plex_url: str, token: str, path: str) -> str:
    """Ask Plex to look at the folder that changed.

    Plex notices new files on its own but not always a file that changed size
    in place, and waiting for the nightly scan to reveal the new track is a
    poor demonstration of a thing you just ran.
    """
    if not plex_url or not token:
        return "not configured"
    base = plex_url.rstrip("/")
    try:
        sections = httpx.get(f"{base}/library/sections",
                             headers={"X-Plex-Token": token, "Accept": "application/json"},
                             timeout=TIMEOUT).json()
        containers = ((sections.get("MediaContainer") or {}).get("Directory") or [])
        for section in containers:
            for location in section.get("Location") or []:
                root = location.get("path", "")
                if root and path.startswith(root):
                    httpx.get(f"{base}/library/sections/{section['key']}/refresh",
                              params={"path": str(path.rsplit("/", 1)[0])},
                              headers={"X-Plex-Token": token}, timeout=TIMEOUT)
                    return f"Plex: refreshed {section.get('title', section['key'])}"
        return "Plex: no library contains that path"
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return f"refresh failed: {exc}"


# ---------------------------------------------------------------------------
#  Jellyfin
# ---------------------------------------------------------------------------
#  The same two jobs Plex does - tell it a file changed, and say whether it is
#  busy enough to be worth waiting for - against a different API.
#
#  The cleaned track itself needs nothing special: it is a standard extra audio
#  stream in the same container, with a title. Jellyfin lists audio tracks and
#  shows their titles exactly as Plex does - once it has read the file again,
#  which is what jellyfin_refresh is for.

def jellyfin_sessions(url: str, api_key: str) -> list[PlexSession]:
    """Who is watching, as the same shape the Plex path returns.

    Reusing PlexSession is deliberate: the hold policy, the wording on the
    Queue page and the settings dropdown are all written against it, and a
    parallel type would mean saying all of that twice.
    """
    if not url or not api_key:
        return []
    try:
        resp = httpx.get(f"{url.rstrip('/')}/Sessions",
                         headers={**_jf_headers(api_key),
                                  "Accept": "application/json"},
                         timeout=10.0)
        resp.raise_for_status()
        rows = resp.json() or []
    except (httpx.HTTPError, ValueError):
        return []       # a check that cannot run is not a reason to stop work

    out: list[PlexSession] = []
    for row in rows:
        item = row.get("NowPlayingItem") or {}
        if not item:
            continue
        play = row.get("PlayState") or {}
        if play.get("IsPaused"):
            continue

        # Jellyfin reports what it is doing per stream. Absent TranscodingInfo
        # means a direct play, which costs the server nothing.
        info = row.get("TranscodingInfo") or {}
        if not info:
            video = audio = "directplay"
        else:
            video = "copy" if info.get("IsVideoDirect") else "transcode"
            audio = "copy" if info.get("IsAudioDirect") else "transcode"

        title = item.get("Name") or "something"
        if item.get("SeriesName"):
            title = f"{item['SeriesName']} - {title}"

        out.append(PlexSession(
            who=row.get("UserName") or "someone",
            what=title,
            state="playing",
            video_decision=video,
            audio_decision=audio,
            hardware=bool(info.get("HardwareAccelerationType")),
        ))
    return out


def _jellyfin_reprobe(url: str, api_key: str, item_id: str) -> bool:
    """Ask Jellyfin to read one item's file again. True when it agreed to."""
    try:
        resp = httpx.post(
            f"{url.rstrip('/')}/Items/{item_id}/Refresh",
            # FullRefresh is what makes Jellyfin probe the file whatever its
            # date says. ReplaceAllMetadata is left off on purpose: it would
            # overwrite a title or description edited by hand. Images are not
            # asked for at all.
            params={"metadataRefreshMode": "FullRefresh"},
            headers=_jf_headers(api_key), timeout=TIMEOUT)
    except httpx.HTTPError:
        return False
    return resp.status_code < 300


def jellyfin_refresh(url: str, api_key: str, path: str, item_ids=()) -> str:
    """Have Jellyfin read one file's streams again.

    A notice that a path changed (/Library/Media/Updated) only queues
    Jellyfin's ordinary refresh, and that probes a file again only when its
    modification time has moved. The swap keeps that time on purpose (see
    media.swap_in), so after the notice alone Jellyfin goes on listing the
    audio tracks the file had before. The item is therefore refreshed by id.

    `item_ids` are the ids the item may have, likeliest first; each may be a
    callable that looks one up, so a search is only made when it is needed.
    The path notice remains the fallback for a file Jellyfin does not know as
    an item yet: it scans the folder and reads a new file in full.
    """
    if not url or not api_key:
        return "not configured"
    tried = set()
    for candidate in item_ids:
        if callable(candidate):
            try:
                candidate = candidate()
            except Exception as exc:  # noqa: BLE001 - a lookup must not fail a finished job
                print(f"[censarr] finding the item in Jellyfin: {exc}", flush=True)
                continue
        candidate = str(candidate or "")
        if not candidate or candidate in tried:
            continue
        tried.add(candidate)
        if _jellyfin_reprobe(url, api_key, candidate):
            return "Jellyfin: asked it to read the file again"
    try:
        resp = httpx.post(
            f"{url.rstrip('/')}/Library/Media/Updated",
            headers=_jf_headers(api_key),
            json={"Updates": [{"Path": path, "UpdateType": "Modified"}]},
            timeout=TIMEOUT)
        if resp.status_code >= 400:
            return f"refresh failed: {resp.status_code}"
        return "Jellyfin: told it the file changed"
    except httpx.HTTPError as exc:
        return f"refresh failed: {exc}"


def refresh_target(settings) -> str:
    """Which server gets told that a file changed.

    The explicit choice first. Failing that, the library - when the library is
    Plex or Jellyfin its address and token are already here, and they are the
    same server that has to re-read the file. Left to "neither", a Plex-only
    install cleans a file perfectly and nothing appears in Plex until its next
    scheduled scan, which reads as the job having done nothing at all.

    Waiting for transcodes is not inferred the same way: that one stops work,
    so it stays the explicit choice it looks like.
    """
    chosen = getattr(settings, "media_server", "none")
    if chosen in ("plex", "jellyfin"):
        return chosen
    source = getattr(settings, "library_source", "sonarr")
    return source if source in ("plex", "jellyfin") else "none"


def sessions_for(settings) -> list[PlexSession]:
    """Whichever media server was chosen, or nothing.

    The library is deliberately not used as a fallback here - see
    refresh_target.
    """
    if settings.media_server == "jellyfin":
        return jellyfin_sessions(settings.jellyfin_url, settings.jellyfin_api_key)
    if settings.media_server == "plex":
        return plex_sessions(settings.plex_url, settings.plex_token)
    return []


def refresh_for(settings, path: str, kind: str = "", source: str = "",
                source_id: str = "", title: str = "") -> str:
    """Tell the media server a file changed. `kind`, `source`, `source_id` and
    `title` are the job's, and are how the item is found in Jellyfin."""
    target = refresh_target(settings)
    if target == "jellyfin":
        from . import library

        def by_path() -> str:
            return library.server(settings, "jellyfin").find(kind, path, title)

        # The job's own id is Jellyfin's when the listing came from Jellyfin;
        # a stale one is refused and the search by path takes over.
        from_jellyfin = (source == "jellyfin"
                         or library.source_name(settings) == "jellyfin")
        return jellyfin_refresh(
            settings.jellyfin_url, settings.jellyfin_api_key, path,
            item_ids=(source_id if from_jellyfin else "", by_path))
    if target == "plex":
        return plex_refresh(settings.plex_url, settings.plex_token, path)
    return "no media server configured"
