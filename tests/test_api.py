"""The HTTP API: settings, the path check, posters and the queue endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from censarr import config, db, main, pipeline


# ---------------------------------------------------------------- settings

def test_secrets_are_masked_and_a_masked_save_keeps_them(client, settings):
    settings.sonarr = config.ArrConfig(url="http://s", api_key="real-key")
    settings.plex_token = "plex-secret"
    settings.jellyfin_api_key = "jf-secret"
    settings.asr_api_key = "asr-secret"
    config.save(settings)
    data = client.get("/api/settings").json()
    assert data["sonarr"]["api_key"] == "********"
    assert data["plex_token"] == "********"
    client.put("/api/settings", json=data)
    s = config.load()
    assert (s.sonarr.api_key, s.plex_token, s.jellyfin_api_key, s.asr_api_key) == (
        "real-key", "plex-secret", "jf-secret", "asr-secret")
    client.put("/api/settings", json={"plex_token": "new-token"})
    assert config.load().plex_token == "new-token"


def test_renaming_the_track_remembers_the_old_name(client, settings):
    client.put("/api/settings", json={"track_title": "English - Censored"})
    client.put("/api/settings", json={"track_title": "Family"})
    s = config.load()
    assert s.track_title == "Family"
    assert s.known_track_titles == ["Censored - English", "English - Censored"]
    client.put("/api/settings", json={"track_title": "  "})
    assert config.load().track_title == "Censored - English"


def test_a_config_from_before_the_rename_keeps_its_tracks_recognised(home):
    config.CONFIG_FILE.write_text("track_title: Cleaned - English\n", encoding="utf8")
    s = config.load()
    assert s.track_title == "Censored - English"
    assert "Cleaned - English" in s.known_track_titles


def test_old_config_keys_are_migrated(home):
    config.CONFIG_FILE.write_text(
        "pause_while_plex_playing: true\npath_map: [{from: /a, to: /b}]\n"
        "plex_url: http://p\nsonarr: {url: http://s, api_key: k}\n", encoding="utf8")
    s = config.load()
    assert s.hold_policy == "video_transcode"
    assert s.media_server == "plex"
    assert s.sonarr.api_key == "k"
    assert not hasattr(s, "path_map")


def test_a_config_from_before_radarr_was_dropped(home):
    config.CONFIG_FILE.write_text(
        "library_source: arr\nradarr: {url: http://r, api_key: k}\n"
        "sonarr: {url: http://s, api_key: k}\n", encoding="utf8")
    s = config.load()
    assert s.library_source == "sonarr" and not hasattr(s, "radarr")
    config.save(s)
    assert "radarr" not in config.CONFIG_FILE.read_text(encoding="utf8")


def test_categories_and_policies_are_offered(client, settings):
    data = client.get("/api/settings").json()
    assert [c["key"] for c in data["available_categories"]] == [
        "strong", "mild", "blasphemy", "slurs_sexual"]
    assert data["hold_policies"][0]["key"] == "never"


def test_service_tests_report_failures_as_data(client, settings):
    r = client.post("/api/settings/test/sonarr")
    assert r.status_code == 200 and r.json() == {
        "ok": False, "error": "Sonarr address and API key are not set"}
    r = client.post("/api/settings/test/plex")
    assert r.json()["ok"] is False
    assert client.post("/api/settings/test/nothing").status_code == 404


# ---------------------------------------------------------------- paths

def test_suggest_finds_the_folder_mounted_elsewhere(tmp_path):
    (tmp_path / "data" / "media" / "tv").mkdir(parents=True)
    root = str(tmp_path / "data")
    matched, found = main._suggest("/mnt/tank/media/tv", [root])
    assert found == str(Path(root, "media", "tv"))
    assert matched == "/mnt/tank/media/tv"


def test_suggest_needs_two_components_to_match(tmp_path):
    (tmp_path / "data" / "tv").mkdir(parents=True)
    # Only "tv" in common: a coincidence, not an answer.
    assert main._suggest("/mnt/other/tv", [str(tmp_path / "data")]) == ("", "")
    # A one-part path is all there is to match.
    (tmp_path / "data" / "movies").mkdir()
    assert main._suggest("/movies", [str(tmp_path / "data")])[1].endswith("movies")


def test_suggest_prefers_the_deepest_match(tmp_path):
    (tmp_path / "r" / "media" / "tv" / "Show").mkdir(parents=True)
    _, found = main._suggest("/x/media/tv/Show", [str(tmp_path / "r")])
    assert found.endswith("media/tv/Show")


def test_missing_explains_which_problem_it_is(tmp_path):
    assert "not found from this container" in pipeline._missing(tmp_path / "nope" / "a.mkv")
    assert "is not there any more" in pipeline._missing(tmp_path / "a.mkv")


def test_path_report(client, settings, stub, tmp_path, monkeypatch):
    here = tmp_path / "media" / "tv"
    here.mkdir(parents=True)
    stub.route("GET", "/api/v3/rootfolder", [{"path": str(here)}, {"path": "/nas/media/tv"}])
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k")
    config.save(settings)
    monkeypatch.setattr(main, "_visible_roots", lambda: [str(tmp_path)])
    rows = client.get("/api/paths").json()["roots"]
    assert rows[0]["ok"] is True
    assert rows[1]["ok"] is False and rows[1]["elsewhere"] == str(here)


# ---------------------------------------------------------------- posters

def test_sonarr_poster_url(client, settings, stub):
    # Sonarr's 500px copy, not the original.
    stub.route("GET", "/api/v3/mediacover/42/poster-500.jpg", lambda req: (200, b"JPEGDATA"))
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k")
    config.save(settings)
    r = client.get("/api/poster", params={"source": "sonarr", "id": "42"})
    assert r.status_code == 200 and r.content == b"JPEGDATA"
    assert "immutable" in r.headers["cache-control"]
    assert stub.requests[0].headers["x-api-key"] == "k"
    # Served from disk the second time.
    client.get("/api/poster", params={"source": "sonarr", "id": "42"})
    assert len(stub.requests) == 1
    assert client.get("/api/poster", params={"source": "sonarr", "id": "43"}).status_code == 404


def test_sonarr_poster_falls_back_to_the_original(client, settings, stub):
    stub.route("GET", "/api/v3/mediacover/7/poster.jpg", lambda req: (200, b"FULLSIZE"))
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k")
    config.save(settings)
    r = client.get("/api/poster", params={"source": "sonarr", "id": "7"})
    assert r.content == b"FULLSIZE"
    assert [q.path for q in stub.requests] == ["/api/v3/mediacover/7/poster-500.jpg",
                                               "/api/v3/mediacover/7/poster.jpg"]


def test_a_poster_asked_for_at_once_is_fetched_once(client, settings, stub):
    import time
    from concurrent.futures import ThreadPoolExecutor

    def slow(req):
        time.sleep(0.2)
        return 200, b"JPEG"
    stub.route("GET", "/api/v3/mediacover/5/poster-500.jpg", slow)
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k")
    config.save(settings)
    with ThreadPoolExecutor(8) as pool:
        got = list(pool.map(lambda _: main._fetch_poster("sonarr", 5, config.load()), range(8)))
    assert got == [True] * 8
    assert len(stub.requests) == 1
    assert not list((main.CACHE_DIR / "posters").glob("*.part"))


def test_a_poster_cached_at_full_size_is_replaced(client, settings, stub):
    stub.route("GET", "/api/v3/mediacover/9/poster-500.jpg", lambda req: (200, b"SMALL"))
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k")
    config.save(settings)
    old = main.CACHE_DIR / "posters" / "sonarr-9.jpg"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"BIG")
    assert client.get("/api/poster", params={"source": "sonarr", "id": "9"}).content == b"SMALL"
    assert not old.exists()


# ---------------------------------------------------------------- jobs

def item(n: int) -> dict:
    return {"kind": "episode", "title": "Show", "subtitle": f"S01E0{n}",
            "path": f"/tv/show/{n}.mkv", "source": "sonarr", "source_id": str(n)}


def test_queueing_is_manual_and_marks_new_only(client, settings):
    r = client.post("/api/jobs", json={
        "items": [item(1), item(2), item(1)],
        "monitor": {"source": "sonarr", "source_id": "7", "title": "Show"}})
    assert r.json()["queued"] == 2 and r.json()["already_queued"] == 1
    assert [dict(m)["mode"] for m in db.monitors()] == ["new_only"]
    assert client.post("/api/jobs", json={"items": []}).status_code == 400


def test_queue_endpoints(client, settings):
    ids = client.post("/api/jobs", json={"items": [item(1), item(2), item(3)]}).json()["ids"]
    assert client.post(f"/api/jobs/{ids[2]}/move", json={"where": "top"}).status_code == 200
    assert client.post(f"/api/jobs/{ids[2]}/move", json={"where": "up"}).status_code == 409
    assert client.post("/api/jobs/move", json={"ids": [ids[0]], "where": "bottom"}).json()["moved"] == 1
    assert [r["id"] for r in db.queued_in_order()] == [ids[2], ids[1], ids[0]]
    assert client.post("/api/jobs/cancel", json={"ids": [ids[1]]}).json() == {"cancelled": 1}
    assert client.delete(f"/api/jobs/{ids[1]}").status_code == 409
    listing = client.get("/api/jobs").json()
    assert listing["cancelled"] == 1
    assert client.post("/api/jobs/retry", json={"ids": [ids[1]]}).json() == {"retrying": 1}
    assert client.delete("/api/queue").json() == {"cancelled": 3}
    assert client.delete("/api/jobs/cancelled").json() == {"removed": 3}
    assert client.get(f"/api/jobs/{ids[0]}").status_code == 404


def test_job_detail_lists_detections(client, settings):
    from censarr import words
    job = db.enqueue(kind="file", title="x", path="/tv/x.mkv")
    db.save_detections(job, [words.Match(1.0, 1.2, "shit", "strong")])
    got = client.get(f"/api/jobs/{job}").json()
    assert got["job"]["id"] == job
    assert got["detections"][0]["text"] == "shit"


def test_remove_all_needs_the_phrase(client, settings):
    assert client.post("/api/history/remove-all", json={}).status_code == 400
    job = db.enqueue(kind="file", title="x", path="/tv/x.mkv")
    db.update(job, status="done")
    assert client.post("/api/history/remove-all", json={"confirm": "remove all"}
                       ).json() == {"queued": 1}
    assert db.next_queued()["action"] == "remove"


def test_webhook_test_event(client, settings):
    r = client.post("/api/webhook/sonarr", json={"eventType": "Test"})
    assert r.json()["message"] == "Cens-arr heard you"
    r = client.post("/api/webhook/radarr", json={"eventType": "Test"})
    assert r.json()["message"] == "Cens-arr heard you"


def settled(calls: list, want: int) -> bool:
    """The sweep runs on its own thread; give it a moment to be counted."""
    import time
    for _ in range(100):
        if len(calls) >= want:
            break
        time.sleep(0.02)
    return len(calls) == want


def radarr_import(path, **extra) -> dict:
    return {"eventType": "Download", "movie": {"id": 12, "title": "Film", "year": 2021,
                                               "folderPath": str(Path(path).parent)},
            "movieFile": {"id": 3, "relativePath": Path(path).name, "path": str(path)},
            **extra}


def test_radarr_webhook_queues_the_film_it_names(client, settings, tmp_path, monkeypatch):
    swept = []
    monkeypatch.setattr(main, "_webhook_check", lambda: swept.append(1))
    monkeypatch.setattr(main, "_films_check", lambda: None)
    film = tmp_path / "Film (2021)" / "Film (2021).mkv"
    film.parent.mkdir()
    film.write_bytes(b"x")

    # The webhook says when; the switch says whether.
    off = client.post("/api/webhook/radarr", json=radarr_import(film)).json()
    assert off["queued"] == 0 and "switched off" in off["message"]
    assert db.next_queued() is None and not swept

    client.post("/api/monitors", json={"source": "films", "source_id": "new",
                                       "title": "New films", "mode": "new_only"})
    assert client.post("/api/webhook/radarr", json=radarr_import(film)).json()["queued"] == 1
    job = db.next_queued()
    assert (job["kind"], job["title"], job["subtitle"], job["path"], job["source"],
            job["source_id"]) == ("movie", "Film", "2021", str(film), "radarr", "12")
    # Told twice (an import and an upgrade arriving together): one job.
    assert client.post("/api/webhook/radarr", json=radarr_import(film)).json()["queued"] == 0
    assert len(db.queued_in_order()) == 1

    # An older Radarr gives the folder and the file name, not the whole path.
    db.update(job["id"], status="done")
    older = radarr_import(film)
    del older["movieFile"]["path"]
    assert client.post("/api/webhook/radarr", json=older).json()["queued"] == 1
    assert settled(swept, 3)


def test_radarr_webhook_leaves_other_news_and_other_mounts_to_the_sweep(
        client, settings, tmp_path, monkeypatch):
    swept = []
    monkeypatch.setattr(main, "_webhook_check", lambda: swept.append(1))
    db.monitor_add("films", "new", "New films", "new_only")
    # Radarr sees the film somewhere this container does not: the media server
    # is asked instead, which the sweep does.
    there = radarr_import("/somewhere/else/Film.mkv")
    assert client.post("/api/webhook/radarr", json=there).json()["queued"] == 0
    assert client.post("/api/webhook/radarr", json={"eventType": "Grab"}).json()["queued"] == 0
    assert client.post("/api/webhook/radarr", content=b"not json").json()["queued"] == 0
    assert db.next_queued() is None and settled(swept, 3)


def test_file_info(client, settings, tmp_path):
    assert client.get("/api/file", params={"path": str(tmp_path / "no.mkv")}).status_code == 404
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"x")
    assert client.get("/api/file", params={"path": str(junk)}).status_code == 400


def test_static_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert r.headers["cache-control"] == "no-cache"
    assert client.get("/static/sw.js").headers["service-worker-allowed"] == "/"


# ---------------------------------------------------------------- test buttons

def test_test_button_uses_what_was_typed(client, settings, stub):
    stub.route("GET", "/api/v3/system/status", lambda req: (
        (200, {"appName": "Sonarr", "version": "4.0"})
        if req.headers.get("x-api-key") == "typed" else (401, {})))
    r = client.post("/api/settings/test/sonarr", json={"url": stub.url, "api_key": "typed"})
    assert r.json() == {"ok": True, "app": "Sonarr", "version": "4.0"}
    assert config.load().sonarr.url == ""                 # testing saves nothing
    bad = client.post("/api/settings/test/sonarr", json={"url": "sonarr:8989"})
    assert "http://" in bad.json()["error"]


def test_masked_key_in_the_form_means_the_saved_one(client, settings, stub):
    stub.route("GET", "/System/Info", lambda req: (
        (200, {"ServerName": "jf", "Version": "10"})
        if 'Token="saved"' in req.headers.get("authorization", "") else (401, {})))
    settings.jellyfin_api_key = "saved"
    config.save(settings)
    r = client.post("/api/settings/test/jellyfin", json={"url": stub.url,
                                                         "api_key": "********"})
    assert r.json()["ok"] is True


@pytest.mark.parametrize("names, wanted, has", [
    (["qwen3.5:9b"], "qwen3.5:9b", True),
    (["qwen3.5:4b"], "qwen3.5:9b", False),
    (["llama3:latest"], "llama3", True),
    (["llama3:8b"], "llama3", False),
    ([], "x", False),
])
def test_has_model(names, wanted, has):
    assert main._has_model(names, wanted) is has


def test_judge_check_ollama_and_openai(client, settings, stub):
    stub.route("GET", "/api/tags", {"models": [{"name": "qwen3.5:9b"}]})
    r = client.post("/api/judge/check", json={"url": stub.url, "model": "qwen3.5:9b"}).json()
    assert r["reachable"] and r["api"] == "ollama" and r["has_selected"] and r["can_pull"]
    stub.routes.pop(("GET", "/api/tags"))
    stub.route("GET", "/v1/models", {"data": [{"id": "gpt-oss"}]})
    r = client.post("/api/judge/check", json={"url": stub.url, "model": "gpt-oss"}).json()
    assert r["reachable"] and r["api"] == "openai" and r["has_selected"]
    assert not r["can_pull"]
    r = client.post("/api/judge/check", json={"url": "http://127.0.0.1:9", "model": "x"}).json()
    assert r["reachable"] is False and r["error"]


def test_remote_models_send_the_key(client, settings, stub):
    stub.route("GET", "/v1/models", lambda req: (
        (200, {"data": [{"id": "b"}, {"id": "a"}]})
        if req.headers.get("authorization") == "Bearer sk" else (401, {})))
    r = client.post("/api/asr/remote-models", json={"url": stub.url + "/v1", "api_key": "sk"})
    assert r.json() == {"ok": True, "models": ["a", "b"]}


def test_asr_test_uses_the_typed_key(client, settings, stub):
    stub.route("POST", "/v1/audio/transcriptions", lambda req: (
        (200, {"words": [{"word": "x", "start": 0, "end": 0.1}]})
        if req.headers.get("authorization") == "Bearer typed" else (401, {})))
    r = client.post("/api/asr/test", json={"url": stub.url, "model": "m", "api_key": "typed"})
    assert r.json()["ok"] is True


def test_plex_shows_count_cleaned_episodes_by_name(client, settings, stub):
    stub.route("GET", "/library/sections", {"MediaContainer": {"Directory": [
        {"key": "1", "type": "show", "title": "TV"}]}})
    stub.route("GET", "/library/sections/1/all", {"MediaContainer": {
        "totalSize": 1, "Metadata": [{"ratingKey": "9", "title": "Show", "leafCount": 3}]}})
    settings.library_source = "plex"
    settings.plex_url, settings.plex_token = stub.url, "t"
    config.save(settings)
    a = db.enqueue(kind="episode", title="Show", path="/tv/show/1.mkv")
    db.update(a, status="done")
    db.enqueue(kind="episode", title="Show", path="/tv/show/2.mkv")
    db.enqueue(kind="episode", title="Other", path="/tv/other/1.mkv")
    show = client.get("/api/series").json()["items"][0]
    assert (show["cleaned"], show["pending"]) == (1, 1)


def test_check_for_new_episodes_and_clean_anyway(client, settings, monkeypatch):
    monkeypatch.setattr(main.worker, "check_monitored", lambda: 2)
    assert client.post("/api/monitors/check").json()["queued"] == 2
    main.worker.last_monitor_films = 1
    said = client.post("/api/monitors/check").json()
    assert (said["queued"], said["episodes"], said["films"]) == (2, 1, 1)
    main.worker.last_monitor_films = 0
    main.worker.holding = "someone is watching"
    assert client.post("/api/queue/clean-anyway").json() == {"override": True}
    assert main.worker.override is True and main.worker.holding == ""
    main.worker.override = False


def test_ollama_pull_is_streamed_and_errors_are_kept(client, settings, stub):
    import time
    settings.judge_url, settings.judge_model = stub.url, "qwen3.5:9b"
    config.save(settings)
    stub.route("POST", "/api/pull", lambda req: (200, b'{"status":"pulling"}\n{"error":"no space left"}\n'))
    assert client.post("/api/judge/pull").json() == {"started": True, "model": "qwen3.5:9b"}
    for _ in range(50):
        state = client.get("/api/judge/pull").json()
        if not state["downloading"] and state["error"]:
            break
        time.sleep(0.05)
    assert "no space left" in state["error"]
    assert stub.seen("/api/pull")[0].json() == {"model": "qwen3.5:9b"}
    main._download_errors.clear()


def test_media_sessions_endpoint(client, settings, stub):
    stub.route("GET", "/Sessions", [{"UserName": "dad", "NowPlayingItem": {"Name": "Film"},
                                     "TranscodingInfo": {"IsVideoDirect": False}}])
    settings.media_server = "jellyfin"
    settings.jellyfin_url, settings.jellyfin_api_key = stub.url, "k"
    config.save(settings)
    data = client.get("/api/media/sessions").json()
    assert data["holding"] is True and data["policy"] == "video_transcode"
    assert data["sessions"][0]["description"].startswith("dad is watching Film")


def test_calendar_needs_sonarr(client, settings):
    settings.library_source = "plex"
    config.save(settings)
    assert client.get("/api/calendar").json()["unavailable"] is True


# ---------------------------------------------------------------- the library views

@pytest.fixture
def arr_library(settings, stub):
    stub.route("GET", "/api/v3/episode", [
        {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "title": "A", "episodeFileId": 10},
        {"id": 2, "seasonNumber": 1, "episodeNumber": 2, "title": "B", "episodeFileId": 11}])
    stub.route("GET", "/api/v3/episodefile", [
        {"id": 10, "path": "/tv/s/1.mkv", "dateAdded": "2024-01-01T00:00:00Z"},
        {"id": 11, "path": "/tv/s/2.mkv", "dateAdded": "2024-01-02T00:00:00Z"}])
    # Films come from the media server.
    stub.route("GET", "/library/sections", {"MediaContainer": {"Directory": [
        {"key": "2", "type": "movie", "title": "Films"}]}})
    stub.route("GET", "/library/sections/2/all", {"MediaContainer": {"totalSize": 1, "Metadata": [
        {"ratingKey": "5", "title": "Film", "year": 2020, "addedAt": 1706745600,
         "Media": [{"Part": [{"file": "/m/f.mkv"}]}]}]}})
    stub.route("GET", "/api/v3/history", {"records": [{
        "seriesId": 3, "episodeId": 2, "date": "2024-01-02T00:00:00Z",
        "data": {"importedPath": "/tv/s/2.mkv"}, "series": {"title": "Show"},
        "episode": {"seasonNumber": 1, "episodeNumber": 2, "title": "B"}}]})
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k")
    settings.media_server = "plex"
    settings.plex_url, settings.plex_token = stub.url, "t"
    config.save(settings)
    done = db.enqueue(kind="episode", title="Show", path="/tv/s/1.mkv")
    db.update(done, status="done", muted=4, added_bytes=9, finished_at=100.0)
    db.enqueue(kind="movie", title="Film", path="/m/f.mkv")
    db.monitor_add("sonarr", "3", "Show", "new_only")
    return done


def test_episodes_carry_their_job(client, arr_library):
    eps = client.get("/api/series/3/episodes").json()["items"]
    assert [(e["job_status"], e["job_id"], e["muted"], e["cleaned_at"], e["added_bytes"])
            for e in eps] == [("done", arr_library, 4, 100.0, 9), ("", None, None, None, None)]


def test_movies_carry_their_job(client, arr_library):
    film = client.get("/api/movies").json()["items"][0]
    assert film["job_status"] == "queued" and film["source"] == "plex" and film["id"] == "5"
    assert "added_bytes" in film          # the Remove dialog says what comes back
    assert film["latest"].startswith("2024-02-01T00:00:00")


def test_the_film_switch_is_reported_and_is_not_a_show(client, arr_library, monkeypatch):
    taken, swept = [], []
    monkeypatch.setattr(main, "_films_check", lambda: taken.append(1))
    monkeypatch.setattr(main.worker, "_check_shows", lambda *a: swept.append(1) or 0)
    assert client.get("/api/movies").json()["auto"] is False
    assert client.get("/api/home").json()["films_auto"] is False
    client.post("/api/monitors", json={"source": "films", "source_id": "new",
                                       "title": "New films", "mode": "new_only"})
    assert client.get("/api/movies").json()["auto"] is True
    home, jobs = client.get("/api/home").json(), client.get("/api/jobs").json()
    assert (home["films_auto"], home["monitors"]) == (True, 1)   # one show, as before
    assert (jobs["films_auto"], jobs["monitors"], jobs["films_note"]) == (True, 1, "")
    # Switching it on takes in the library at once, not at the next sweep -
    # the films only, without a sweep of the shows on the side.
    assert settled(taken, 1)
    assert main.worker.check_films() == 0 and not swept
    assert db.films_seen() == {"/m/f.mkv"} and len(db.queued_in_order()) == 1   # as before
    main.worker.films_note = "9 films appeared at once"
    assert client.get("/api/jobs").json()["films_note"] == "9 films appeared at once"
    assert client.delete("/api/monitors/films/new").json() == {"monitored": False}
    assert client.get("/api/movies").json()["auto"] is False
    assert db.films_seen() == set() and main.worker.films_note == ""


def test_home(client, arr_library):
    home = client.get("/api/home").json()
    assert home["problems"] == []
    ep = home["episodes"][0]
    assert (ep["series"], ep["monitored"], ep["job_status"], ep["source"]) == ("Show", True, "", "sonarr")
    assert home["movies"][0]["job_status"] == "queued"
    assert home["stats"]["cleaned_files"] == 1 and home["monitors"] == 1


def test_home_names_what_did_not_answer(client, settings):
    home = client.get("/api/home").json()
    assert home["problems"] == [
        "Sonarr: Sonarr address and API key are not set",
        "Films: they come from your media server - choose Plex or Jellyfin under Media server"]


def test_startup_requeues_interrupted_jobs(home, monkeypatch):
    from fastapi.testclient import TestClient
    started = []
    monkeypatch.setattr(main.worker, "start", lambda: started.append(True))
    monkeypatch.setattr(main.worker, "stop", lambda: None)
    monkeypatch.setattr(main, "CACHE_DIR", home / "cache")
    job = db.enqueue(kind="file", title="x", path="/x")
    db.update(job, status="running")
    with TestClient(main.app):
        assert db.get(job)["status"] == "queued"
        assert started == [True]


# ---------------------------------------------------------------- speed

def test_home_asks_for_recent_films_not_the_whole_library(client, arr_library, stub):
    stub.route("GET", "/library/sections/2/recentlyAdded", {"MediaContainer": {"Metadata": [
        {"ratingKey": "5", "title": "Film", "year": 2020, "addedAt": 1706745600,
         "Media": [{"Part": [{"file": "/m/f.mkv"}]}]}]}})
    home = client.get("/api/home").json()
    assert home["problems"] == [] and home["movies"][0]["title"] == "Film"
    assert stub.seen("/library/sections/2/all") == []


def test_a_library_list_is_reused_and_refreshed_behind_the_page(client, arr_library, stub, monkeypatch):
    first = client.get("/api/movies").json()["items"]
    asked = len(stub.seen("/library/sections/2/all"))
    again = client.get("/api/movies").json()["items"]
    assert again == first and len(stub.seen("/library/sections/2/all")) == asked
    # Past LIST_FRESH the old answer is still served, and a new one fetched.
    monkeypatch.setattr(main, "LIST_FRESH", 0.0)
    client.get("/api/movies")
    main.POOL.submit(lambda: None).result()
    import time
    for _ in range(50):
        if len(stub.seen("/library/sections/2/all")) > asked:
            break
        time.sleep(0.02)
    assert len(stub.seen("/library/sections/2/all")) > asked


def test_job_statuses_are_fresh_when_the_list_is_cached(client, arr_library):
    assert client.get("/api/movies").json()["items"][0]["job_status"] == "queued"
    [(movie_job,)] = db.connect().execute("SELECT id FROM job WHERE path='/m/f.mkv'").fetchall()
    db.update(movie_job, status="done")
    assert client.get("/api/movies").json()["items"][0]["job_status"] == "done"


def test_changing_the_library_settings_asks_afresh(client, arr_library, stub, settings):
    client.get("/api/movies")
    asked = len(stub.seen("/library/sections/2/all"))
    settings = config.load()
    settings.plex_token = "another"
    config.save(settings)
    client.get("/api/movies")
    assert len(stub.seen("/library/sections/2/all")) == asked + 1


def test_statuses_by_folder():
    under = main._statuses_by_folder({"/tv/Show/Season 01/a.mkv": "done",
                                      "/tv/Show/Season 02/b.mkv": "failed",
                                      "/tv/Other/c.mkv": "queued"})
    assert sorted(under["/tv/Show/"]) == ["done", "failed"]
    assert under["/tv/Other/"] == ["queued"] and len(under["/tv/"]) == 3


def test_lists_are_compressed_and_posters_are_not(client, arr_library, stub):
    r = client.get("/api/movies", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    stub.route("GET", "/api/v3/mediacover/1/poster-500.jpg", lambda req: (200, b"J" * 5000))
    p = client.get("/api/poster", params={"source": "sonarr", "id": "1"},
                   headers={"Accept-Encoding": "gzip"})
    assert p.headers.get("content-encoding") is None
    big = client.get("/static/app.js", headers={"Accept-Encoding": "gzip"})
    assert big.headers.get("content-encoding") == "gzip"
