"""The queue: one job at a time, forever.

One at a time is deliberate. The GPU is shared with Plex transcodes and
Ollama, and two jobs would each take twice as long while making the box worse
at the thing it is actually for.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import arr, asr, config, db, library, pipeline

# How long an idle queue holds the speech model before handing its ~2GB back.
# Was ten minutes, which left the card two gigabytes down long after the last
# episode finished - and on an 8GB card shared with Plex and Ollama that is the
# difference between the next thing fitting and not. Ninety seconds costs
# nothing in practice: a queue with work in it never reaches this branch, so
# back-to-back jobs still never reload. It only applies once the queue has
# actually drained, and reloading after that costs a few seconds, once.
IDLE_UNLOAD_SECONDS = 90
WATCHING_RECHECK_SECONDS = 60
MONITOR_INTERVAL_SECONDS = 600

# "Censor new films" is one switch for the whole film library, kept as a row
# beside the shows': when it was switched on, and when films were last looked
# for.
FILM_MONITOR = ("films", "new")
# More new films than this in one check is not films arriving: it is a library
# renamed, remounted or imported whole. They are left alone, and the Queue
# page says so - the alternative is one switch quietly queueing a library.
FILM_BURST = 50


def _arrived_after(added: str, since: float) -> bool:
    """Did Sonarr import this file after the show was marked?

    An unreadable or missing date counts as new, because the alternative is
    silently never cleaning an episode.
    """
    if not added:
        return True
    try:
        stamp = datetime.fromisoformat(str(added).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp() > since
    except (TypeError, ValueError):
        return True


def films_watched() -> bool:
    return db.is_monitored(*FILM_MONITOR)


def enqueue_film(title: str, year, path: str, source: str, source_id) -> int | None:
    """Queue a film that just arrived, ahead of any back catalogue: it is the
    one someone means to watch tonight."""
    return db.enqueue(kind="movie", title=title or Path(path).stem,
                      subtitle=str(year or ""), path=path, source=source,
                      source_id=str(source_id or ""), front=True)


class Worker:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self._thread: threading.Thread | None = None
        self._watch_thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancel_current: int | None = None
        self.current_job: int | None = None
        self.holding: str = ""     # why the queue is waiting, for the UI
        self.last_monitor_check: float = 0.0
        self.last_monitor_queued: int = 0
        self.last_monitor_films: int = 0
        self.films_note: str = ""      # a burst of films left alone, for the UI
        # "Clean anyway": ignore the hold until the queue runs dry, then go
        # back to normal. Deliberately not permanent - it answers "I know, do
        # it now", not "stop asking".
        self.override: bool = False

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="censarr-worker",
                                        daemon=True)
        self._thread.start()
        # Watching for new episodes runs on its own thread, because the worker
        # thread spends most of its life inside a single job. Sharing them
        # meant an episode that landed during a long queue waited for the whole
        # queue before anyone noticed it - the opposite of "new episodes first".
        self._watch_thread = threading.Thread(target=self._watch_loop,
                                              name="censarr-monitor", daemon=True)
        self._watch_thread.start()

    def _watch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.check_monitored():
                    self.nudge()
            except Exception as exc:  # noqa: BLE001
                print(f"[censarr] monitor check failed: {exc}", flush=True)
            self._stop.wait(timeout=MONITOR_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def nudge(self) -> None:
        """Something was queued; look again now rather than at the next tick."""
        self._wake.set()

    def cancel(self, job_id: int) -> bool:
        if self.current_job == job_id:
            self._cancel_current = job_id
            return True
        return db.cancel_queued(job_id)

    # -- the loop ---------------------------------------------------------
    # -- shows that clean themselves -------------------------------------
    def check_monitored(self) -> int:
        """Queue any episode of a monitored show that has not been cleaned.

        Sonarr is the source of truth for what exists; this service's own job
        history is the record of what has been done. Anything in the first and
        not the second gets queued - which covers an episode that arrived an
        hour ago and one that arrived while the container was off.
        """
        settings = config.load()
        self.last_monitor_check = time.time()
        watched = db.monitors("sonarr")
        films = db.monitors(FILM_MONITOR[0])
        waiting = db.monitors("radarr")
        if not watched and not films and not waiting:
            return 0
        known = db.job_paths()
        queued = self._check_shows(settings, watched, known) if watched else 0
        new_films = self._check_films(settings, films[0], known) if films else 0
        new_films += self._check_waiting(settings, waiting, known) if waiting else 0
        self.last_monitor_queued = queued + new_films
        self.last_monitor_films = new_films
        if queued:
            print(f"[censarr] queued {queued} new episode(s) from monitored shows",
                  flush=True)
        if new_films:
            print(f"[censarr] queued {new_films} new film(s)", flush=True)
        return queued + new_films

    def _check_shows(self, settings, watched, known: dict) -> int:
        # Whichever source the library comes from answers this the same way:
        # here are the show's episodes, and when each one arrived.
        source = library.build(settings)
        queued = 0
        for row in watched:
            try:
                episodes = source.episodes(row["source_id"])
            except (arr.ArrError, library.LibraryError, ValueError):
                continue
            mode = (row["mode"] if "mode" in row.keys() else "catch_up") or "catch_up"
            for episode in episodes:
                if known.get(episode["path"]) in ("done", "skipped", "queued",
                                                  "running", "failed"):
                    continue
                # "From now on" means exactly that: files that were already on
                # disk when the show was marked are left alone.
                if mode == "new_only" and not _arrived_after(episode.get("added"),
                                                             row["added_at"]):
                    continue
                # An episode that just landed goes to the front: it is the one
                # someone is waiting to watch tonight, while a back-catalogue
                # queue can wait another hour.
                job_id = db.enqueue(
                    kind="episode", title=row["title"] or "Monitored show",
                    subtitle=f"S{episode['season']:02d}E{episode['episode']:02d}"
                             f" · {episode['title']}",
                    path=episode["path"], source="sonarr",
                    source_id=str(episode["id"]), front=True)
                if job_id:
                    queued += 1
            db.monitor_touch("sonarr", row["source_id"])
        return queued

    # -- films marked before they downloaded ------------------------------
    def _check_waiting(self, settings, rows, known: dict) -> int:
        """Queue each film marked in Radarr that has a file now.

        Radarr says where the file is, as Radarr sees it. That path is used
        when it opens here; otherwise the media server is asked for the same
        file (same name, same folder), and until it lists it the film waits
        for the next check. Once queued - or already censored - the mark has
        done its job and goes.
        """
        radarr = arr.Radarr(settings.radarr.url, settings.radarr.api_key)
        name = library.film_source(settings)
        server = library.server(settings, name) if name else None
        server_films: list[dict] | None = None
        queued = 0
        for row in rows:
            try:
                movie = radarr.movie(row["source_id"])
            except arr.ArrError:
                continue
            if not movie["has_file"]:
                continue
            path, source, source_id = movie["path"], "radarr", str(movie["id"])
            if not Path(path).is_file():
                if server is None:
                    continue
                if server_films is None:
                    try:
                        server_films = server.movies()
                    except (arr.ArrError, library.LibraryError, ValueError):
                        server_films = []
                match = next((f for f in server_films
                              if library.same_file(f["path"], path)), None)
                if match is None:
                    continue
                path, source, source_id = match["path"], name, match["id"]
            if known.get(path) is None and enqueue_film(
                    movie["title"] or row["title"], movie["year"], path, source, source_id):
                queued += 1
            # A film is also seen by "Censor new films" when that is on: it
            # has a job now, so it is not offered twice.
            db.films_mark_seen([path])
            db.monitor_remove("radarr", row["source_id"])
        return queued

    # -- films that censor themselves -------------------------------------
    def check_films(self) -> int:
        """The film half of check_monitored on its own. Switching "Censor new
        films" on asks for this, so the library is taken in there and then
        without sweeping every marked show as a side effect."""
        rows = db.monitors(FILM_MONITOR[0])
        if not rows:
            return 0
        return self._check_films(config.load(), rows[0], db.job_paths())

    def _check_films(self, settings, row, known: dict) -> int:
        """Queue the films the media server lists now and did not list before.

        Films come from the media server, so it is the one asked. What is new
        is decided by the list itself, not by a date: a server's "date added"
        is often the file's own date, and a film downloaded today can carry
        one from years ago. So the first check after the switch goes on only
        remembers every film there is, and each later one queues the films it
        has not seen - which also catches a film replaced by a better copy
        under a new name.

        A film is offered once. Whatever happens to its job - cancelled
        included - it is not queued again from here, and a film this service
        already has a job for is not queued at all.
        """
        name = library.film_source(settings)
        server = library.server(settings, name) if name else None
        if server is None:
            return 0
        try:
            films = server.movies()
        except (arr.ArrError, library.LibraryError, ValueError):
            return 0
        seen = db.films_seen()
        fresh = [film for film in films if film["path"] not in seen]
        first = not row["last_checked"]
        db.films_mark_seen(film["path"] for film in fresh)
        db.monitor_touch(*FILM_MONITOR)
        if first:
            return 0
        if len(fresh) > FILM_BURST:
            self.films_note = (f"{len(fresh)} films appeared at once and were left "
                               f"alone - censor them from Movies if you want them")
            print(f"[censarr] {self.films_note}", flush=True)
            return 0
        queued = 0
        for film in fresh:
            if film["path"] in known:
                continue
            if enqueue_film(film["title"], film.get("year"), film["path"],
                            name, film["id"]):
                queued += 1
        if queued:
            self.films_note = ""
        return queued

    def _loop(self) -> None:
        last_job_at = time.time()
        while not self._stop.is_set():
            job = db.next_queued()
            if job is None:
                self.holding = ""
                self.override = False      # nothing left to push through
                # Hand the VRAM back after a quiet spell.
                if time.time() - last_job_at > IDLE_UNLOAD_SECONDS:
                    asr.unload_model()
                    last_job_at = time.time()
                self._wake.wait(timeout=5)
                self._wake.clear()
                continue

            # Plex is working for someone: leave the GPU alone and look again
            # shortly. A direct play is not a reason to wait - see should_hold.
            settings = config.load()
            if not self.override:
                sessions = arr.sessions_for(settings)
                hold, why = arr.should_hold(sessions, settings.hold_policy)
                if hold:
                    self.holding = why
                    asr.unload_model()
                    self._wake.wait(timeout=WATCHING_RECHECK_SECONDS)
                    self._wake.clear()
                    continue
            self.holding = ""

            job_id = int(job["id"])
            self.current_job = job_id
            self._cancel_current = None
            try:
                pipeline.run_job(
                    job_id, settings, self.cache_dir,
                    should_cancel=lambda: self._cancel_current == job_id
                    or self._stop.is_set())
            finally:
                self.current_job = None
                last_job_at = time.time()

    @property
    def busy(self) -> bool:
        return self.current_job is not None
