"""A made-up library for the UI tests and the README screenshots.

Stand-ins for Sonarr and Plex serve fictional shows and films, the media
files exist (as empty files) in a temporary folder so the path check passes,
and the job history is seeded with one of everything: done, failed, queued,
running and cancelled.

    python tests/demo.py      # serve it on http://127.0.0.1:8477 to look at
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))
sys.path.insert(0, str(HERE))

SHOWS = [
    # title, year, seasons, episodes per season, days since last import
    ("The Quiet Harbour", 2021, 3, 8, 0),
    ("Signal Hill", 2023, 2, 10, 0),
    ("Paper Moons", 2019, 4, 6, 1),
    ("Night Shift Bakery", 2024, 1, 8, 2),
    ("Orbital", 2022, 2, 8, 3),
    ("Low Tide", 2020, 3, 6, 5),
    ("Copper Canyon", 2018, 5, 10, 9),
    ("The Long Field", 2024, 1, 6, 12),
    ("Starling Road", 2017, 6, 12, 20),
    ("Kettle & Co", 2023, 1, 10, 30),
    ("Ember Street", 2025, 1, 0, 40),
    ("North Light", 2016, 2, 8, 60),
]
FILMS = [
    ("The Last Lighthouse", 2022, 0),
    ("Glass Garden", 2019, 1),
    ("Midnight Diner", 2023, 3),
    ("Paper Planes", 2021, 6),
    ("Tall Grass", 2020, 10),
    ("Blue Hour", 2024, 15),
    ("Winter Orchard", 2018, 22),
    ("The Clockmaker", 2017, 40),
]
EPISODE_TITLES = ["Pilot", "Low Water", "The Letter", "Harbour Lights", "Fog",
                  "Crossings", "The Keeper", "Salt", "Undertow", "Homecoming",
                  "Driftwood", "Anchors"]
COLOURS = ["0x2f4858", "0x33658a", "0x86bbd8", "0x758e4f", "0xf6ae2d", "0xf26419",
           "0x6d597a", "0xb56576", "0x355070", "0x2a9d8f", "0xe76f51", "0x264653"]
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"


def iso(days_ago: float) -> str:
    stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def poster(title: str, index: int, folder: Path) -> bytes:
    """A gradient with the title on it, made by ffmpeg. Plain if drawtext is missing."""
    out = folder / f"poster-{index}.jpg"
    if not out.exists():
        c0, c1 = COLOURS[index % len(COLOURS)], COLOURS[(index + 5) % len(COLOURS)]
        base = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                f"gradients=s=300x450:c0={c0}:c1={c1}:x0=0:y0=0:x1=300:y1=450:d=1"]
        words = title.split()
        lines = []
        for w in words:
            if lines and len(lines[-1]) + len(w) < 12:
                lines[-1] += f" {w}"
            else:
                lines.append(w)
        text = folder / f"poster-{index}.txt"
        text.write_text("\n".join(lines), encoding="utf8")
        draw = (f"drawtext=fontfile={FONT}:textfile={text}:fontcolor=white:fontsize=34:"
                f"line_spacing=8:x=(w-tw)/2:y=h-th-48:shadowcolor=black@0.5:shadowx=2:shadowy=2")
        ok = Path(FONT).exists() and subprocess.run(
            [*base, "-vf", draw, "-frames:v", "1", str(out)], capture_output=True).returncode == 0
        if not ok:
            subprocess.run([*base, "-frames:v", "1", str(out)], capture_output=True, check=True)
    return out.read_bytes()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Demo:
    """Everything a UI test needs: stub services, files, settings, jobs, the app."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or tempfile.mkdtemp(prefix="censarr-demo-"))
        self.config = self.root / "config"
        self.media = self.root / "media"
        self.art = self.root / "art"
        for folder in (self.config, self.media / "tv", self.media / "movies", self.art):
            folder.mkdir(parents=True, exist_ok=True)
        self.url = ""
        self.server = None

    # ------------------------------------------------------------ library
    def build_library(self):
        self.series, self.episodes, self.files, self.history = [], {}, {}, []
        next_ep, next_file = 1000, 5000
        for sid, (title, year, seasons, per, last) in enumerate(SHOWS, start=1):
            folder = self.media / "tv" / title
            eps, files = [], []
            count = 0
            for season in range(1, seasons + 1):
                for number in range(1, per + 1):
                    next_ep += 1
                    next_file += 1
                    path = folder / f"Season {season:02d}" / f"{title} - S{season:02d}E{number:02d}.mkv"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                    added = last + (seasons - season) * 60 + (per - number) * 7
                    eps.append({"id": next_ep, "seasonNumber": season, "episodeNumber": number,
                                "title": EPISODE_TITLES[(number - 1) % len(EPISODE_TITLES)],
                                "episodeFileId": next_file})
                    files.append({"id": next_file, "path": str(path), "size": 480_000_000 + number * 9_000_000,
                                  "dateAdded": iso(added),
                                  "quality": {"quality": {"name": "WEBDL-1080p"}}})
                    count += 1
            self.series.append({"id": sid, "title": title, "year": year, "path": str(folder),
                                "status": "continuing", "added": iso(400 + sid),
                                "statistics": {"episodeFileCount": count}})
            self.episodes[sid] = eps
            self.files[sid] = files
            if eps:
                last_ep, last_file = eps[-1], files[-1]
                self.history.append({
                    "seriesId": sid, "episodeId": last_ep["id"], "date": last_file["dateAdded"],
                    "data": {"importedPath": last_file["path"], "size": last_file["size"]},
                    "quality": {"quality": {"name": "WEBDL-1080p"}},
                    "series": {"title": title},
                    "episode": {"seasonNumber": last_ep["seasonNumber"],
                                "episodeNumber": last_ep["episodeNumber"], "title": last_ep["title"]},
                })
        self.history.sort(key=lambda r: r["date"], reverse=True)
        self.movies = []
        for mid, (title, year, days) in enumerate(FILMS, start=1):
            path = self.media / "movies" / f"{title} ({year})" / f"{title} ({year}).mkv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            self.movies.append({"id": mid, "title": title, "year": year,
                                "movieFile": {"path": str(path), "size": 2_100_000_000 + mid * 1e8,
                                              "dateAdded": iso(days),
                                              "quality": {"quality": {"name": "Bluray-1080p"}}}})

    def calendar(self):
        rows = []
        for sid, (title, _year, _s, _p, _l) in enumerate(SHOWS[:7], start=1):
            for k in range(2):
                airs = dt.datetime.now(dt.timezone.utc).replace(hour=1, minute=0, second=0) \
                    + dt.timedelta(days=sid - 2 + k * 7)
                has = airs < dt.datetime.now(dt.timezone.utc)
                rows.append({"seriesId": sid, "id": 9000 + sid * 10 + k,
                             "episodeFileId": 0, "seasonNumber": 2, "episodeNumber": 3 + k,
                             "title": EPISODE_TITLES[(sid + k) % len(EPISODE_TITLES)],
                             "airDateUtc": airs.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "runtime": 45, "hasFile": has, "monitored": True,
                             "series": {"title": title, "network": ["Northwind", "Bluebird TV", "Channel 9"][sid % 3]}})
        return rows

    def start_services(self):
        from stubs import Stub
        self.build_library()
        self.sonarr = Stub().start()
        self.plex = Stub().start()
        s, p = self.sonarr, self.plex
        s.route("GET", "/api/v3/system/status", {"appName": "Sonarr", "version": "4.0.10"})
        s.route("GET", "/api/v3/series", self.series)
        s.route("GET", "/api/v3/rootfolder", [{"path": str(self.media / "tv")}])
        s.route("GET", "/api/v3/episode",
                lambda req: (200, self.episodes.get(int(req.query["seriesId"][0]), [])))

        def files(req):
            if "seriesId" in req.query:
                return 200, self.files.get(int(req.query["seriesId"][0]), [])
            return 200, []
        s.route("GET", "/api/v3/episodefile", files)
        s.route("GET", "/api/v3/history", lambda req: (200, {"records": self.history}))
        s.route("GET", "/api/v3/calendar", lambda req: (200, self.calendar()))
        # Radarr: the films on disk, two still to come, and a calendar.
        self.radarr = r = Stub().start()
        soon = lambda days: (dt.date.today() + dt.timedelta(days=days)).isoformat()  # noqa: E731
        waiting = [{"id": 901, "title": "Northern Lights", "year": 2027, "hasFile": False,
                    "monitored": True, "status": "announced"},
                   {"id": 902, "title": "Paper Lanterns", "year": 2026, "hasFile": False,
                    "monitored": True, "status": "inCinemas", "inCinemas": "2026-10-02",
                    "digitalRelease": soon(3)}]
        on_disk = [{"id": m["id"], "title": m["title"], "year": m["year"],
                    "hasFile": True, "monitored": True} for m in self.movies]
        r.route("GET", "/api/v3/system/status", {"appName": "Radarr", "version": "5.9"})
        r.route("GET", "/api/v3/movie", on_disk + waiting)
        out_on_disc = next(m for m in on_disk if m["title"] == "Blue Hour")
        r.route("GET", "/api/v3/calendar", lambda req: (200, [
            waiting[1], {**out_on_disc, "physicalRelease": soon(2)}]))
        for n, film in enumerate([*waiting, out_on_disc]):
            r.route("GET", f"/api/v3/mediacover/{film['id']}/poster-500.jpg",
                    lambda req, t=film["title"], n=n: (200, poster(t, n + 40, self.art)))
        # Films come from the media server, as they do for real.
        p.route("GET", "/library/sections", {"MediaContainer": {"Directory": [
            {"key": "2", "type": "movie", "title": "Films",
             "Location": [{"path": str(self.media / "movies")}]}]}})
        p.route("GET", "/library/sections/2/all", {"MediaContainer": {
            "totalSize": len(self.movies), "Metadata": [{
                "ratingKey": str(m["id"]), "title": m["title"], "year": m["year"],
                "addedAt": int(dt.datetime.fromisoformat(
                    m["movieFile"]["dateAdded"].replace("Z", "+00:00")).timestamp()),
                "Media": [{"Part": [{"file": m["movieFile"]["path"],
                                     "size": int(m["movieFile"]["size"])}]}]}
                for m in self.movies]}})
        for i, show in enumerate(self.series):
            s.route("GET", f"/api/v3/mediacover/{show['id']}/poster.jpg",
                    lambda req, t=show["title"], n=i: (200, poster(t, n, self.art)))
        for i, film in enumerate(self.movies):
            p.route("GET", f"/library/metadata/{film['id']}",
                    {"MediaContainer": {"Metadata": [{"thumb": f"/thumb/{film['id']}"}]}})
            p.route("GET", f"/thumb/{film['id']}",
                    lambda req, t=film["title"], n=i: (200, poster(t, n + 20, self.art)))

    # ------------------------------------------------------------ the app
    def configure(self, **overrides):
        from censarr import config
        settings = config.Settings()
        settings.sonarr = config.ArrConfig(url=self.sonarr.url, api_key="demo-sonarr-key")
        settings.radarr = config.ArrConfig(url=self.radarr.url, api_key="demo-radarr-key")
        settings.media_server = "plex"
        settings.plex_url, settings.plex_token = self.plex.url, "demo-plex-token"
        settings.check_in_context = ["cock", "christ", "jesus"]
        settings.allow_words_by_title = {"Signal Hill": ["dick"]}
        for key, value in overrides.items():
            setattr(settings, key, value)
        config.save(settings)
        return settings

    def point_app_here(self):
        """Aim the app's module-level paths at this demo's folders."""
        from censarr import config, db, main
        config.CONFIG_DIR = self.config
        config.CONFIG_FILE = self.config / "config.yaml"
        db.DB_PATH = self.config / "censarr.sqlite"
        db._local = threading.local()
        db._schema_ready = False
        main.CACHE_DIR = self.config / "cache"
        (self.config / "cache").mkdir(exist_ok=True)
        # A downloaded model, as far as the page can tell.
        model = main._model_dir("medium.en") / "blobs"
        model.mkdir(parents=True, exist_ok=True)
        (model / "model.bin").write_bytes(b"\0" * 1024)

    def seed_jobs(self):
        from censarr import db, words
        now = time.time()

        def job(show_idx, season, number, status, muted=0, message="", size=0, ago=0.0):
            sid = show_idx + 1
            f = next(x for x in self.files[sid]
                     if f"S{season:02d}E{number:02d}" in x["path"])
            ep = next(e for e in self.episodes[sid] if e["episodeFileId"] == f["id"])
            job_id = db.enqueue(kind="episode", title=SHOWS[show_idx][0],
                                subtitle=f"S{season:02d}E{number:02d} · {ep['title']}",
                                path=f["path"], source="sonarr", source_id=str(ep["id"]))
            db.update(job_id, status=status, muted=muted, message=message, added_bytes=size,
                      duration=1500.0, started_at=now - ago - 90, finished_at=now - ago
                      if status not in ("queued", "running") else None)
            return job_id

        detections = [
            words.Match(62.4, 62.8, "shit", "strong", confidence=0.93,
                        subtitle="Oh, shit.", subtitle_state="agrees"),
            words.Match(301.2, 301.6, "damn", "mild", confidence=0.88,
                        subtitle="Damn it, Ellie.", subtitle_state="agrees"),
            words.Match(302.0, 302.4, "Dick,", "slurs_sexual", confidence=0.71,
                        subtitle="Dick, get the ropes!", subtitle_state="agrees"),
            words.Match(730.1, 730.5, "god", "blasphemy", True,
                        "the Lord's name used as an exclamation (in “oh my god”)",
                        confidence=0.9, subtitle="Oh, my God.", subtitle_state="agrees"),
            words.Match(1205.0, 1205.4, "pussy.", "slurs_sexual", True,
                        "muted from the subtitles: Whisper heard “posse”",
                        subtitle="Don't turn your back on me, you pussy.",
                        subtitle_state="subtitle_only"),
            # Unsure, but the subtitles have it: settled, not worth a listen.
            words.Match(1210.0, 1210.4, "damn", "mild", confidence=0.34,
                        subtitle="Damn it.", subtitle_state="agrees"),
        ]
        left = [words.Match(880.0, 880.4, "cock", "slurs_sexual", True,
                            "left in: the subtitles have “caulk” here, which sounds like it",
                            confidence=0.62, subtitle="More caulk here.", subtitle_state="soundalike")]
        for n in range(1, 9):
            j = job(0, 1, n, "done", muted=5 + n, size=88_000_000 + n * 1_000_000,
                    message=f"added “Censored - English” · {5 + n} muted", ago=86400 * (10 - n))
            db.save_detections(j, detections[: 2 + n % 4], left if n == 2 else [])
        for n in range(1, 4):
            job(1, 1, n, "done", muted=3 * n, size=92_000_000, ago=3600 * n,
                message=f"added “Censored - English” · {3 * n} muted")
        job(2, 1, 1, "skipped", message="nothing to mute - no profanity found", ago=7200)
        self.running = job(1, 1, 4, "running", ago=0)
        db.update(self.running, stage="listening", progress=0.38, message="listening for profanity")
        for n in range(5, 10):
            job(1, 1, n, "queued")
        job(3, 1, 2, "failed", message="MediaError: the new file is 1450.2s long, the original was 1500.0s")
        job(4, 1, 1, "cancelled", message="cancelled")
        db.monitor_add("sonarr", "1", SHOWS[0][0], "new_only")
        db.monitor_add("sonarr", "2", SHOWS[1][0], "catch_up")

    def serve(self, port: int | None = None):
        import uvicorn

        from censarr import main
        port = port or free_port()
        # No lifespan: the worker must not start, or it would run the queued
        # demo jobs against empty files.
        config = uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning",
                                lifespan="off")
        self.server = uvicorn.Server(config)
        threading.Thread(target=self.server.run, daemon=True).start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    def stop(self):
        if self.server:
            self.server.should_exit = True
        for stub in (getattr(self, "sonarr", None), getattr(self, "plex", None),
                     getattr(self, "radarr", None)):
            if stub:
                stub.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def up(self, port: int | None = None, **settings):
        self.start_services()
        self.point_app_here()
        self.configure(**settings)
        self.seed_jobs()
        return self.serve(port)


if __name__ == "__main__":
    os.environ.setdefault("CENSARR_WEB", str(HERE.parent / "web"))
    demo = Demo()
    url = demo.up(port=int(os.environ.get("PORT", "8477")))
    print(f"demo running at {url} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        demo.stop()
