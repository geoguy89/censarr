"""Shows that clean themselves: catch_up vs new_only, and what counts as done."""

from __future__ import annotations

import time

import pytest

from censarr import config, db, worker


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@pytest.fixture
def sonarr(stub, settings):
    now = time.time()
    stub.route("GET", "/api/v3/episode", [
        {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "title": "Old", "episodeFileId": 11},
        {"id": 2, "seasonNumber": 1, "episodeNumber": 2, "title": "New", "episodeFileId": 12},
        {"id": 3, "seasonNumber": 1, "episodeNumber": 3, "title": "Undated", "episodeFileId": 13},
        {"id": 4, "seasonNumber": 1, "episodeNumber": 4, "title": "Not aired"},
    ])
    stub.route("GET", "/api/v3/episodefile", [
        {"id": 11, "path": "/tv/show/s01e01.mkv", "dateAdded": iso(now - 86400 * 30)},
        {"id": 12, "path": "/tv/show/s01e02.mkv", "dateAdded": iso(now + 60)},
        {"id": 13, "path": "/tv/show/s01e03.mkv", "dateAdded": ""},
    ])
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k", enabled=True)
    config.save(settings)
    return stub


def queued_titles() -> list[str]:
    return [db.get(r["id"])["subtitle"] for r in db.queued_in_order()]


def test_catch_up_queues_everything_on_disk(sonarr, home):
    db.monitor_add("sonarr", "7", "Show", "catch_up")
    w = worker.Worker(home)
    assert w.check_monitored() == 3
    assert sorted(queued_titles()) == ["S01E01 · Old", "S01E02 · New", "S01E03 · Undated"]
    assert sonarr.requests[0].headers["x-api-key"] == "k"
    assert db.monitors("sonarr")[0]["last_checked"] > 0


def test_new_only_leaves_the_back_catalogue_alone(sonarr, home):
    db.monitor_add("sonarr", "7", "Show", "new_only")
    assert worker.Worker(home).check_monitored() == 2
    # An undated file counts as new: never silently skipping one is the rule.
    assert sorted(queued_titles()) == ["S01E02 · New", "S01E03 · Undated"]


def test_nothing_is_queued_twice_or_after_it_finished(sonarr, home):
    db.monitor_add("sonarr", "7", "Show", "catch_up")
    w = worker.Worker(home)
    w.check_monitored()
    assert w.check_monitored() == 0
    for row in db.queued_in_order():
        db.update(row["id"], status="done")
    assert w.check_monitored() == 0


def test_a_failed_episode_waits_for_a_person(sonarr, home):
    db.monitor_add("sonarr", "7", "Show", "catch_up")
    db.enqueue(kind="episode", title="Show", path="/tv/show/s01e01.mkv")
    db.update(db.next_queued()["id"], status="failed")
    assert worker.Worker(home).check_monitored() == 2


def test_new_episodes_go_to_the_front(sonarr, home):
    db.enqueue(kind="episode", title="Backlog", subtitle="backlog", path="/tv/other.mkv")
    db.monitor_add("sonarr", "7", "Show", "new_only")
    worker.Worker(home).check_monitored()
    assert queued_titles()[-1] == "backlog"


def test_cleaning_a_season_does_not_downgrade_catch_up(home):
    db.monitor_add("sonarr", "7", "Show", "catch_up")
    db.monitor_add("sonarr", "7", "Show", "new_only", keep_existing_mode=True)
    assert db.monitors()[0]["mode"] == "catch_up"
    db.monitor_add("sonarr", "7", "Show", "new_only")
    assert db.monitors()[0]["mode"] == "new_only"


def test_an_unreachable_library_is_skipped_quietly(home, settings):
    settings.sonarr = config.ArrConfig(url="http://127.0.0.1:9", api_key="k")
    config.save(settings)
    db.monitor_add("sonarr", "7", "Show", "catch_up")
    assert worker.Worker(home).check_monitored() == 0


def test_no_monitors_means_no_requests(home, settings, stub):
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k")
    config.save(settings)
    assert worker.Worker(home).check_monitored() == 0
    assert stub.requests == []


def test_plex_library_monitors_work_the_same(home, settings, stub):
    now = time.time()
    stub.route("GET", "/library/metadata/55/allLeaves", {"MediaContainer": {"Metadata": [
        {"ratingKey": "1", "parentIndex": 2, "index": 1, "title": "Old",
         "addedAt": int(now - 86400), "Media": [{"Part": [{"file": "/tv/a.mkv"}]}]},
        {"ratingKey": "2", "parentIndex": 2, "index": 2, "title": "New",
         "addedAt": int(now + 60), "Media": [{"Part": [{"file": "/tv/b.mkv"}]}]},
    ]}})
    settings.library_source = "plex"
    settings.plex_url, settings.plex_token = stub.url, "t"
    config.save(settings)
    db.monitor_add("sonarr", "55", "Show", "new_only")
    assert worker.Worker(home).check_monitored() == 1
    assert queued_titles() == ["S02E02 · New"]


@pytest.mark.parametrize("added, since, want", [
    ("", 100.0, True),
    ("garbage", 100.0, True),
    ("1970-01-01T00:01:00Z", 100.0, False),
    ("1970-01-01T00:01:41Z", 100.0, True),
    ("1970-01-01T00:01:41", 100.0, True),          # naive means UTC
])
def test_arrived_after(added, since, want):
    assert worker._arrived_after(added, since) is want


# ---------------------------------------------------------------- films

def film(key: int, title: str, year: int = 2020, added: float | None = None) -> dict:
    row = {"ratingKey": str(key), "title": title, "year": year,
           "Media": [{"Part": [{"file": f"/films/{title}.mkv"}]}]}
    if added is not None:
        row["addedAt"] = int(added)
    return row


@pytest.fixture
def plex_films(stub, settings):
    """A Plex whose film library can be changed between checks."""
    stub.films = []
    stub.route("GET", "/library/sections", {"MediaContainer": {"Directory": [
        {"key": "2", "type": "movie", "title": "Films"}]}})

    def everything(req):
        start = int(req.query.get("X-Plex-Container-Start", ["0"])[0])
        size = int(req.query.get("X-Plex-Container-Size", ["100000"])[0])
        return 200, {"MediaContainer": {"totalSize": len(stub.films),
                                        "Metadata": stub.films[start:start + size]}}
    stub.route("GET", "/library/sections/2/all", everything)
    settings.media_server = "plex"
    settings.plex_url, settings.plex_token = stub.url, "t"
    config.save(settings)
    return stub


def switch_on(home) -> worker.Worker:
    """Turn "Censor new films" on and let it take in the library as it stands."""
    db.monitor_add(*worker.FILM_MONITOR, "New films", "new_only")
    w = worker.Worker(home)
    assert w.check_monitored() == 0
    return w


def queued_films() -> list[str]:
    return [db.get(r["id"])["title"] for r in db.queued_in_order()]


def test_a_film_that_appears_is_queued_and_the_library_is_left_alone(plex_films, home):
    plex_films.films = [film(1, "Old"), film(2, "Older")]
    w = switch_on(home)
    assert db.next_queued() is None and db.monitors("films")[0]["last_checked"] > 0
    plex_films.films.append(film(3, "New", 2024))
    assert w.check_monitored() == 1 and w.last_monitor_films == 1
    job = db.next_queued()
    assert (job["kind"], job["title"], job["subtitle"], job["path"], job["source"],
            job["source_id"]) == ("movie", "New", "2024", "/films/New.mkv", "plex", "3")
    assert w.check_monitored() == 0                       # not twice


def test_new_does_not_go_by_the_date_the_server_gives(plex_films, home):
    # A server's "date added" is often the file's own date: a film that lands
    # today can carry one from years ago, and an old film a date of today.
    now = time.time()
    plex_films.films = [film(1, "Old with a fresh date", added=now + 3600)]
    w = switch_on(home)
    plex_films.films.append(film(2, "New with an old date", added=now - 86400 * 900))
    plex_films.films.append(film(3, "New with no date"))
    assert w.check_monitored() == 2
    assert sorted(queued_films()) == ["New with an old date", "New with no date"]


def test_films_wait_for_the_switch(plex_films, home):
    plex_films.films = [film(1, "New")]
    assert worker.Worker(home).check_monitored() == 0
    assert plex_films.requests == []


def test_a_new_film_goes_ahead_of_the_backlog_and_beside_new_episodes(plex_films, sonarr, home):
    db.enqueue(kind="episode", title="Backlog", path="/tv/other.mkv")
    w = switch_on(home)
    db.monitor_add("sonarr", "7", "Show", "new_only")
    plex_films.films.append(film(1, "New"))
    assert w.check_monitored() == 3 and w.last_monitor_films == 1
    assert queued_films()[0] == "New" and queued_films()[-1] == "Backlog"


@pytest.mark.parametrize("status", ["queued", "done", "skipped", "failed", "cancelled"])
def test_a_film_with_a_job_is_not_queued(plex_films, home, status):
    w = switch_on(home)
    job = db.enqueue(kind="movie", title="New", path="/films/New.mkv")   # by hand, or Radarr
    db.update(job, status=status)
    plex_films.films.append(film(1, "New"))
    assert w.check_monitored() == 0
    assert len(db.recent(10, include_finished=True)) == 1


def test_a_film_is_offered_once(plex_films, home):
    w = switch_on(home)
    plex_films.films.append(film(1, "New"))
    assert w.check_monitored() == 1
    db.update(db.next_queued()["id"], status="cancelled")    # "Empty the queue"
    assert w.check_monitored() == 0
    # Gone for a while and back - a disk offline, a library rescanned - is not new.
    plex_films.films.clear()
    assert w.check_monitored() == 0
    plex_films.films.append(film(1, "New"))
    assert w.check_monitored() == 0


def test_a_better_copy_under_a_new_name_is_new(plex_films, home):
    plex_films.films = [film(1, "Film")]
    w = switch_on(home)
    upgraded = film(1, "Film")
    upgraded["Media"][0]["Part"][0]["file"] = "/films/Film Bluray-1080p.mkv"
    plex_films.films[:] = [upgraded]
    assert w.check_monitored() == 1
    assert db.next_queued()["path"] == "/films/Film Bluray-1080p.mkv"


def test_a_library_that_appears_all_at_once_is_left_alone(plex_films, home, monkeypatch):
    monkeypatch.setattr(worker, "FILM_BURST", 3)
    plex_films.films = [film(1, "Old")]
    w = switch_on(home)
    # Renamed, remounted or imported whole: not films arriving.
    plex_films.films += [film(n, f"Renamed {n}") for n in range(2, 6)]
    assert w.check_monitored() == 0 and db.next_queued() is None
    assert "4 films appeared at once" in w.films_note
    assert w.check_monitored() == 0                       # and they stay left alone
    plex_films.films.append(film(9, "New"))
    assert w.check_monitored() == 1 and w.films_note == ""


def test_switching_films_off_forgets_and_on_starts_from_now(plex_films, home):
    w = switch_on(home)
    plex_films.films.append(film(1, "While it was on"))
    assert w.check_monitored() == 1
    db.monitor_remove(*worker.FILM_MONITOR)
    assert db.films_seen() == set()
    plex_films.films.append(film(2, "While it was off"))
    db.monitor_add(*worker.FILM_MONITOR, "New films", "new_only")
    assert w.check_monitored() == 0                       # the library as it stands
    plex_films.films.append(film(3, "After"))
    assert w.check_monitored() == 1
    assert queued_films()[0] == "After"


def test_jellyfin_films_work_the_same(home, settings, stub):
    films = [{"Id": "aa", "Name": "Old", "Path": "/films/Old.mkv"}]
    stub.route("GET", "/Items", lambda req: (200, {"Items": films,
                                                   "TotalRecordCount": len(films)}))
    settings.media_server = "jellyfin"
    settings.jellyfin_url, settings.jellyfin_api_key = stub.url, "k"
    config.save(settings)
    w = switch_on(home)
    films.append({"Id": "bb", "Name": "New", "ProductionYear": 2023, "Path": "/films/New.mkv",
                  "DateCreated": "2011-03-04T05:06:07.0000000Z"})
    assert w.check_monitored() == 1
    job = db.next_queued()
    assert (job["title"], job["subtitle"], job["source"], job["source_id"]) == (
        "New", "2023", "jellyfin", "bb")


def test_an_unreachable_media_server_is_tried_again(plex_films, home, settings):
    plex_films.films = [film(1, "Old")]
    good = settings.plex_url
    settings.plex_url = "http://127.0.0.1:9"
    config.save(settings)
    db.monitor_add(*worker.FILM_MONITOR, "New films", "new_only")
    w = worker.Worker(home)
    assert w.check_monitored() == 0
    assert db.monitors("films")[0]["last_checked"] == 0    # nothing was taken in
    settings.plex_url = good
    config.save(settings)
    assert w.check_monitored() == 0                        # now it is: Old is not new
    plex_films.films.append(film(2, "New"))
    assert w.check_monitored() == 1


def test_no_media_server_means_no_films(home, settings):
    db.monitor_add(*worker.FILM_MONITOR, "New films", "new_only")
    assert worker.Worker(home).check_monitored() == 0
