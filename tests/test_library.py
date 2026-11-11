"""The library clients, against stand-ins for Sonarr, Plex and Jellyfin."""

from __future__ import annotations

import httpx
import pytest

from censarr import arr, config, library


# ---------------------------------------------------------------- Sonarr

def test_sonarr_episodes_join_files(stub):
    stub.route("GET", "/api/v3/episode", [
        {"id": 2, "seasonNumber": 1, "episodeNumber": 2, "title": "B", "episodeFileId": 20},
        {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "title": "A", "episodeFileId": 10},
        {"id": 3, "seasonNumber": 1, "episodeNumber": 3, "title": "C"},
    ])
    stub.route("GET", "/api/v3/episodefile", [
        {"id": 10, "path": "/tv/a.mkv", "size": 5, "dateAdded": "2024-01-01T00:00:00Z",
         "quality": {"quality": {"name": "HDTV-720p"}}},
        {"id": 20, "path": "/tv/b.mkv", "size": 6},
    ])
    got = arr.Sonarr(stub.url, "k").episodes(7)
    assert [(e["id"], e["path"]) for e in got] == [(1, "/tv/a.mkv"), (2, "/tv/b.mkv")]
    assert got[0]["quality"] == "HDTV-720p"
    assert stub.requests[0].query["seriesId"] == ["7"]


def test_arr_errors_are_plain(stub):
    stub.route("GET", "/api/v3/system/status", lambda req: (401, {}))
    with pytest.raises(arr.ArrError, match="Sonarr rejected the API key"):
        arr.Sonarr(stub.url, "bad").test()
    with pytest.raises(arr.ArrError, match="Sonarr address and API key are not set"):
        arr.Sonarr("", "").test()
    with pytest.raises(arr.ArrError, match="could not reach"):
        arr.Sonarr("http://127.0.0.1:9", "k").test()
    with pytest.raises(arr.ArrError, match="URL base"):
        arr.Sonarr(stub.url, "k").series()
    stub.route("GET", "/api/v3/series", lambda req: (200, b"<html>login</html>"))
    with pytest.raises(arr.ArrError, match="did not answer like Sonarr"):
        arr.Sonarr(stub.url, "k").series()


def test_sonarr_library_roots(stub):
    stub.route("GET", "/api/v3/rootfolder", [{"path": "/data/tv"}])
    lib = library.SonarrLibrary(arr.Sonarr(stub.url, "k"), None)
    assert lib.roots() == [{"library": "Sonarr", "kind": "show", "path": "/data/tv"}]


def test_with_sonarr_the_films_come_from_the_media_server(stub):
    stub.route("GET", "/api/v3/rootfolder", [{"path": "/data/tv"}])
    stub.route("GET", "/library/sections", {"MediaContainer": {"Directory": [
        {"key": "1", "type": "show", "title": "TV", "Location": [{"path": "/plex/tv"}]},
        {"key": "2", "type": "movie", "title": "Films", "Location": [{"path": "/data/movies"}]}]}})
    stub.route("GET", "/library/sections/2/all", {"MediaContainer": {"totalSize": 1, "Metadata": [
        {"ratingKey": "9", "title": "A film", "Media": [{"Part": [{"file": "/data/movies/a.mkv"}]}]}]}})
    lib = library.SonarrLibrary(arr.Sonarr(stub.url, "k"), library.PlexLibrary(stub.url, "t"))
    assert [(m["id"], m["path"]) for m in lib.movies()] == [("9", "/data/movies/a.mkv")]
    # TV folders are Sonarr's; Plex's own TV folder is not the one in use.
    assert lib.roots() == [{"library": "Sonarr", "kind": "show", "path": "/data/tv"},
                           {"library": "Films", "kind": "movie", "path": "/data/movies"}]


def test_with_sonarr_and_no_media_server_films_say_why():
    lib = library.SonarrLibrary(arr.Sonarr("", ""), None)
    with pytest.raises(library.LibraryError, match="come from your media server"):
        lib.movies()


# ---------------------------------------------------------------- Plex

def test_plex_pages_through_a_section(stub, monkeypatch):
    stub.route("GET", "/library/sections", {"MediaContainer": {"Directory": [
        {"key": "1", "type": "show", "title": "TV", "Location": [{"path": "/data/tv"}]},
        {"key": "2", "type": "artist", "title": "Music"}]}})
    shows = [{"ratingKey": str(i), "title": f"S{i}", "leafCount": i} for i in range(5)]

    def page(req):
        start = int(req.query["X-Plex-Container-Start"][0])
        size = int(req.query["X-Plex-Container-Size"][0])
        return 200, {"MediaContainer": {"totalSize": 5, "Metadata": shows[start:start + size]}}
    stub.route("GET", "/library/sections/1/all", page)
    real = library.PlexLibrary._all

    def small_pages(self, section, **params):
        # Same loop, page size 2, to exercise paging without 500 items.
        out, start = [], 0
        while True:
            c = self._get(f"/library/sections/{section}/all",
                          **{**params, "X-Plex-Container-Start": start,
                             "X-Plex-Container-Size": 2})
            batch = c.get("Metadata") or []
            out.extend(batch)
            start += 2
            if len(batch) < 2 or start >= int(c.get("totalSize") or len(out)):
                return out
    monkeypatch.setattr(library.PlexLibrary, "_all", small_pages)
    got = library.PlexLibrary(stub.url, "tok").shows()
    assert [s["title"] for s in got] == [f"S{i}" for i in range(5)]
    assert all(r.query["X-Plex-Token"] == ["tok"] for r in stub.requests)
    monkeypatch.setattr(library.PlexLibrary, "_all", real)
    assert library.PlexLibrary(stub.url, "tok").roots() == [
        {"library": "TV", "kind": "show", "path": "/data/tv"}]


def test_plex_errors(stub):
    stub.route("GET", "/library/sections", lambda req: (401, {}))
    with pytest.raises(library.LibraryError, match="rejected the token"):
        library.PlexLibrary(stub.url, "bad").test()
    with pytest.raises(library.LibraryError, match="not set"):
        library.PlexLibrary("", "").test()


def test_plex_episodes_skip_missing_files(stub):
    stub.route("GET", "/library/metadata/9/allLeaves", {"MediaContainer": {"Metadata": [
        {"ratingKey": "2", "parentIndex": 1, "index": 2, "title": "B",
         "Media": [{"Part": [{"file": "/tv/b.mkv", "size": 3}]}]},
        {"ratingKey": "1", "parentIndex": 1, "index": 1, "title": "A",
         "Media": [{"Part": [{"file": "/tv/a.mkv"}]}]},
        {"ratingKey": "3", "parentIndex": 1, "index": 3, "title": "no file"},
    ]}})
    got = library.PlexLibrary(stub.url, "t").episodes("9")
    assert [e["title"] for e in got] == ["A", "B"]


# ---------------------------------------------------------------- Jellyfin

def test_jellyfin_uses_the_mediabrowser_header(stub):
    stub.route("GET", "/System/Info", {"ServerName": "jf", "Version": "10.9"})
    assert library.JellyfinLibrary(stub.url, "key").test() == {
        "ok": True, "app": "jf", "version": "10.9"}
    headers = stub.requests[0].headers
    assert headers["authorization"].startswith('MediaBrowser Token="key"')


def test_jellyfin_errors(stub):
    stub.route("GET", "/System/Info", lambda req: (401, {}))
    with pytest.raises(library.LibraryError, match="rejected the API key"):
        library.JellyfinLibrary(stub.url, "bad").test()


def test_jellyfin_episodes_and_movies(stub):
    def items(req):
        kind = req.query["IncludeItemTypes"][0]
        if kind == "Episode":
            return 200, {"TotalRecordCount": 2, "Items": [
                {"Id": "e2", "ParentIndexNumber": 1, "IndexNumber": 2, "Name": "B",
                 "Path": "/tv/b.mkv", "DateCreated": "2024-02-02T10:00:00.0000000Z"},
                {"Id": "e1", "ParentIndexNumber": 1, "IndexNumber": 1, "Name": "A",
                 "Path": "/tv/a.mkv", "MediaSources": [{"Size": 7}]}]}
        return 200, {"TotalRecordCount": 1, "Items": [
            {"Id": "m1", "Name": "Film", "Path": "/m/f.mkv"}]}
    stub.route("GET", "/Items", items)
    lib = library.JellyfinLibrary(stub.url, "k")
    eps = lib.episodes("abc")
    assert [e["id"] for e in eps] == ["e1", "e2"]
    assert eps[1]["added"] == "2024-02-02T10:00:00"
    assert eps[0]["size"] == 7
    assert [m["title"] for m in lib.movies()] == ["Film"]


def test_jellyfin_roots(stub):
    stub.route("GET", "/Library/VirtualFolders", [
        {"Name": "TV", "CollectionType": "tvshows", "Locations": ["/data/tv"]},
        {"Name": "Music", "CollectionType": "music", "Locations": ["/data/music"]}])
    assert library.JellyfinLibrary(stub.url, "k").roots() == [
        {"library": "TV", "kind": "show", "path": "/data/tv"}]


def test_build_picks_the_source():
    s = config.Settings()
    assert library.build(s).name == "sonarr"
    assert library.build(s).films is None
    s.media_server = "jellyfin"
    assert isinstance(library.build(s).films, library.JellyfinLibrary)
    s.library_source = "arr"                  # an old config: Sonarr and Radarr
    assert library.build(s).name == "sonarr"
    s.library_source = "plex"
    assert isinstance(library.build(s), library.PlexLibrary)
    s.library_source = "jellyfin"
    assert isinstance(library.build(s), library.JellyfinLibrary)
    assert not library.build(s).has_calendar


# ---------------------------------------------------------------- refresh

def test_plex_refresh_targets_the_right_section(stub):
    stub.route("GET", "/library/sections", {"MediaContainer": {"Directory": [
        {"key": "3", "title": "TV", "Location": [{"path": "/data/tv"}]}]}})
    stub.route("GET", "/library/sections/3/refresh", {})
    assert arr.plex_refresh(stub.url, "t", "/data/tv/show/e1.mkv") == "Plex: refreshed TV"
    assert stub.seen("/library/sections/3/refresh")[0].query["path"] == ["/data/tv/show"]
    assert "no library" in arr.plex_refresh(stub.url, "t", "/elsewhere/x.mkv")


def test_jellyfin_refresh_names_the_file(stub):
    # No item to name: the path notice, which is all a new file needs.
    stub.route("POST", "/Library/Media/Updated", {})
    assert "told it" in arr.jellyfin_refresh(stub.url, "k", "/tv/a.mkv")
    req = stub.seen("/Library/Media/Updated")[0]
    assert req.json() == {"Updates": [{"Path": "/tv/a.mkv", "UpdateType": "Modified"}]}
    assert req.headers["authorization"].startswith("MediaBrowser")


def jellyfin(stub, known=("e1",)):
    """A Jellyfin that refreshes the items it knows and takes path notices."""
    for item in known:
        stub.route("POST", f"/Items/{item}/Refresh", lambda req: (204, b""))
    stub.route("POST", "/Library/Media/Updated", lambda req: (204, b""))
    return stub


def test_jellyfin_reads_a_known_item_again(stub):
    # The swap keeps the file's date, and a path notice makes Jellyfin probe
    # only a file whose date moved - so the item is refreshed in full by id.
    jellyfin(stub)
    note = arr.jellyfin_refresh(stub.url, "k", "/tv/a.mkv", item_ids=("e1",))
    assert note == "Jellyfin: asked it to read the file again"
    req = stub.seen("/Items/e1/Refresh")[0]
    assert req.query == {"metadataRefreshMode": ["FullRefresh"]}   # nothing replaced
    assert req.headers["authorization"].startswith("MediaBrowser")
    assert not stub.seen("/Library/Media/Updated")


def test_jellyfin_refresh_looks_the_item_up_only_when_it_has_to(stub):
    jellyfin(stub)
    looked = []

    def by_path():
        looked.append(1)
        return "e1"
    assert "read the file again" in arr.jellyfin_refresh(
        stub.url, "k", "/tv/a.mkv", item_ids=("e1", by_path))
    assert not looked
    # An id Jellyfin no longer has is refused, and the search takes over.
    assert "read the file again" in arr.jellyfin_refresh(
        stub.url, "k", "/tv/a.mkv", item_ids=("gone", by_path))
    assert looked == [1]
    assert len(stub.seen("/Items/gone/Refresh")) == 1


def test_jellyfin_refresh_falls_back_to_the_path(stub):
    jellyfin(stub, known=())

    def broken():
        raise library.LibraryError("could not reach Jellyfin")
    note = arr.jellyfin_refresh(stub.url, "k", "/tv/a.mkv",
                                item_ids=("", "gone", broken, lambda: ""))
    assert note == "Jellyfin: told it the file changed"
    assert len(stub.seen("/Library/Media/Updated")) == 1


def jellyfin_with_a_show(stub, path):
    def items(req):
        kind = req.query["IncludeItemTypes"][0]
        if kind == "Series":
            named = req.query.get("SearchTerm") == ["Show"]
            return 200, {"Items": [{"Id": "s1", "Name": "Show"}] if named else []}
        if kind == "Episode":
            return 200, {"Items": [
                {"Id": "e1", "ParentIndexNumber": 1, "IndexNumber": 1, "Path": path}]}
        return 200, {"Items": []}
    stub.route("GET", "/Items", items)
    return jellyfin(stub)


def test_refresh_finds_a_sonarr_episode_in_jellyfin(stub):
    # Sonarr lists the shows, Jellyfin plays them, and each mounts the
    # library its own way: the item is found by show and file.
    jellyfin_with_a_show(stub, "/media/tv/Show/e1.mkv")
    s = config.Settings()
    s.media_server, s.jellyfin_url, s.jellyfin_api_key = "jellyfin", stub.url, "k"
    note = arr.refresh_for(s, "/data/tv/Show/e1.mkv", kind="episode",
                           source="sonarr", source_id="77", title="Show (2020)")
    assert "read the file again" in note
    assert not stub.seen("/Items/77/Refresh")              # Sonarr's id is not Jellyfin's
    assert [r.query["SearchTerm"] for r in stub.seen("/Items")
            if "SearchTerm" in r.query] == [["Show (2020)"], ["Show"]]


def test_refresh_uses_the_id_a_jellyfin_library_gave(stub):
    jellyfin(stub)
    s = config.Settings()
    s.library_source, s.jellyfin_url, s.jellyfin_api_key = "jellyfin", stub.url, "k"
    # The page files every episode under "sonarr", whatever listed it.
    assert "read the file again" in arr.refresh_for(
        s, "/tv/a.mkv", kind="episode", source="sonarr", source_id="e1", title="Show")
    assert not stub.seen("/Items")                         # no search needed
    assert "told it" in arr.refresh_for(s, "/tv/new.mkv", kind="file")


def test_refresh_target_falls_back_to_the_library():
    s = config.Settings()
    assert arr.refresh_target(s) == "none"
    s.library_source = "plex"
    assert arr.refresh_target(s) == "plex"
    s.media_server = "jellyfin"
    assert arr.refresh_target(s) == "jellyfin"
    assert arr.refresh_for(config.Settings(), "/x") == "no media server configured"


# ---------------------------------------------------------------- versions and subtitles

def test_plex_lists_every_version_with_its_english_text_subtitles(stub):
    stub.route("GET", "/library/metadata/5", {"MediaContainer": {"Metadata": [{"Media": [
        {"Part": [{"file": "/m/Film WEBRip.mkv", "Stream": [
            {"streamType": 2, "key": "/x"},
            {"streamType": 3, "codec": "srt", "languageCode": "eng", "key": "/library/streams/1",
             "displayTitle": "English (SRT External)"},
            {"streamType": 3, "codec": "srt", "languageCode": "eng", "key": "/library/streams/2",
             "forced": True},
            {"streamType": 3, "codec": "pgs", "languageCode": "eng", "key": "/library/streams/3"},
            {"streamType": 3, "codec": "srt", "languageCode": "fre", "key": "/library/streams/4"},
            {"streamType": 3, "codec": "srt", "languageCode": "eng"}]}]},
        {"Part": [{"file": "/m/Film WEBDL.mkv"}]}]}]}})
    got = library.PlexLibrary(stub.url, "t").versions("5")
    assert got == [
        {"path": "/m/Film WEBRip.mkv",
         "subtitles": [("English (SRT External)", f"{stub.url}/library/streams/1")]},
        {"path": "/m/Film WEBDL.mkv", "subtitles": []}]


def test_jellyfin_lists_versions_and_serves_subtitles_as_srt(stub):
    stub.route("GET", "/Items", {"Items": [{"MediaSources": [
        {"Id": "a1", "Path": "/m/one.mkv", "MediaStreams": [
            {"Type": "Subtitle", "Index": 3, "IsExternal": True, "IsTextSubtitleStream": True,
             "Language": "eng", "DisplayTitle": "English"},
            {"Type": "Subtitle", "Index": 4, "IsExternal": False, "IsTextSubtitleStream": True,
             "Language": "eng"}]},
        {"Id": "b2", "Path": "/m/two.mkv", "MediaStreams": []}]}]})
    got = library.JellyfinLibrary(stub.url, "k").versions("77")
    assert got[0]["subtitles"] == [("English", f"{stub.url}/Videos/77/a1/Subtitles/3/Stream.srt")]
    assert [v["path"] for v in got] == ["/m/one.mkv", "/m/two.mkv"]
    assert stub.requests[0].query["Ids"] == ["77"]


def test_plex_finds_an_episode_by_its_show_and_file(stub):
    stub.route("GET", "/library/sections", {"MediaContainer": {"Directory": [
        {"key": "1", "type": "show", "title": "TV"}]}})
    stub.route("GET", "/library/sections/1/all", {"MediaContainer": {"totalSize": 1, "Metadata": [
        {"ratingKey": "70", "title": "Chance"}]}})
    stub.route("GET", "/library/metadata/70/allLeaves", {"MediaContainer": {"Metadata": [
        {"ratingKey": "71", "index": 1, "parentIndex": 2, "Media": [{"Part": [{"file": "/tv/c/1.mkv"}]}]},
        {"ratingKey": "72", "index": 2, "parentIndex": 2, "Media": [{"Part": [{"file": "/tv/c/2.mkv"}]}]}]}})
    plex = library.PlexLibrary(stub.url, "t")
    assert plex.find("episode", "/tv/c/2.mkv", "Chance") == "72"
    assert stub.seen("/library/sections/1/all")[0].query["title"] == ["Chance"]
    assert plex.find("episode", "/tv/c/9.mkv", "Chance") == ""


def test_plex_search_attaches_the_best_match(stub):
    stub.route("GET", "/library/metadata/72/subtitles", {"MediaContainer": {"Stream": [
        {"key": "/sub/a", "score": 90, "displayTitle": "English (a)"},
        {"key": "/sub/b", "score": 70, "perfectMatch": True, "displayTitle": "English (b)"},
        {"key": "/sub/c", "score": 99, "forced": True}]}})
    stub.route("PUT", "/library/metadata/72/subtitles", {})
    assert library.PlexLibrary(stub.url, "t").search_subtitles("72") == "English (b)"
    asked = stub.seen("/library/metadata/72/subtitles")
    assert asked[0].query["language"] == ["en"]
    assert asked[1].method == "PUT" and asked[1].query["key"] == ["/sub/b"]


def test_plex_search_that_finds_nothing_attaches_nothing(stub):
    stub.route("GET", "/library/metadata/72/subtitles", {"MediaContainer": {"size": 0}})
    assert library.PlexLibrary(stub.url, "t").search_subtitles("72") == ""
    assert [r.method for r in stub.seen("/library/metadata/72/subtitles")] == ["GET"]


def test_jellyfin_search_downloads_the_best_match(stub):
    stub.route("GET", "/Items/5/RemoteSearch/Subtitles/eng", [
        {"Id": "x", "Name": "plain", "DownloadCount": 900},
        {"Id": "y", "Name": "hash match", "IsHashMatch": True, "DownloadCount": 3},
        {"Id": "z", "Name": "forced", "IsForced": True, "IsHashMatch": True}])
    stub.route("POST", "/Items/5/RemoteSearch/Subtitles/y", {})
    assert library.JellyfinLibrary(stub.url, "k").search_subtitles("5") == "hash match"
    assert stub.seen("/Items/5/RemoteSearch/Subtitles/y")[0].method == "POST"


def test_the_same_file_under_another_mount():
    assert library.same_file("/data/tv/Show/Season 01/a.mkv", "/tv/Show/Season 01/a.mkv")
    assert library.same_file("D:\\TV\\Show\\Season 01\\a.mkv", "/tv/Show/Season 01/a.mkv")
    assert not library.same_file("/tv/Show/Season 01/a.mkv", "/tv/Show/Season 02/a.mkv")
    assert not library.same_file("/a.mkv", "/b/a.mkv")


def test_another_copys_path_is_translated_to_this_container():
    assert library.local_path("/data/movies/Film/b.mkv", "/data/movies/Film/a.mkv",
                              "/movies/Film/a.mkv") == "/movies/Film/b.mkv"
    assert library.local_path("D:\\Movies\\Film\\b.mkv", "D:\\Movies\\Film\\a.mkv",
                              "/movies/Film/a.mkv") == "/movies/Film/b.mkv"
    assert library.local_path("/elsewhere/b.mkv", "/data/movies/Film/a.mkv",
                              "/movies/Film/a.mkv") == "/elsewhere/b.mkv"      # left alone


def test_show_names_are_tried_without_a_year():
    assert library.title_variants("Doctor Who (2005)") == ["Doctor Who (2005)", "Doctor Who"]
    assert library.title_variants("The Office (US)") == ["The Office (US)", "The Office"]
    assert library.title_variants("Chance") == ["Chance"]
    assert library.title_variants("") == []


def test_plex_finds_an_episode_mounted_elsewhere(stub):
    stub.route("GET", "/library/sections", {"MediaContainer": {"Directory": [
        {"key": "1", "type": "show", "title": "TV"}]}})
    stub.route("GET", "/library/sections/1/all", {"MediaContainer": {"totalSize": 1, "Metadata": [
        {"ratingKey": "70", "title": "Chance"}]}})
    stub.route("GET", "/library/metadata/70/allLeaves", {"MediaContainer": {"Metadata": [
        {"ratingKey": "71", "Media": [{"Part": [{"file": "/data/tv/Chance/Season 02/1.mkv"}]}]},
        {"ratingKey": "72", "Media": [{"Part": [{"file": "/data/tv/Chance/Season 02/2.mkv"}]}]}]}})
    plex = library.PlexLibrary(stub.url, "t")
    assert plex.find("episode", "/tv/Chance/Season 02/2.mkv", "Chance (2016)") == "72"
    asked = [r.query["title"] for r in stub.seen("/library/sections/1/all")]
    assert asked[0] == ["Chance (2016)"]


def test_jellyfin_search_waits_longer_than_a_listing(stub, monkeypatch):
    seen = []
    real = httpx.get

    def get(url, **kw):
        seen.append((url, kw.get("timeout")))
        return real(url, **kw)
    monkeypatch.setattr(library.httpx, "get", get)
    stub.route("GET", "/Items/5/RemoteSearch/Subtitles/eng", [])
    assert library.JellyfinLibrary(stub.url, "k").search_subtitles("5") == ""
    assert seen[0][1] == library.SEARCH_TIMEOUT


def test_plex_posters_are_scaled_by_plex(stub):
    stub.route("GET", "/photo/:/transcode", lambda req: (200, b"SMALL"))
    assert library.PlexLibrary(stub.url, "t").poster("72") == b"SMALL"
    asked = stub.seen("/photo/:/transcode")[0].query
    assert asked["url"] == ["/library/metadata/72/thumb"] and asked["width"] == ["400"]


def test_plex_poster_falls_back_to_the_original(stub):
    stub.route("GET", "/library/metadata/72", {"MediaContainer": {"Metadata": [{"thumb": "/t/72"}]}})
    stub.route("GET", "/t/72", lambda req: (200, b"FULL"))
    assert library.PlexLibrary(stub.url, "t").poster("72") == b"FULL"


def test_plex_recent_films(stub):
    stub.route("GET", "/library/sections", {"MediaContainer": {"Directory": [
        {"key": "2", "type": "movie", "title": "Films"}]}})
    stub.route("GET", "/library/sections/2/recentlyAdded", {"MediaContainer": {"Metadata": [
        {"ratingKey": "5", "title": "Old", "addedAt": 100, "Media": [{"Part": [{"file": "/m/a.mkv"}]}]},
        {"ratingKey": "6", "title": "New", "addedAt": 200, "Media": [{"Part": [{"file": "/m/b.mkv"}]}]},
        {"ratingKey": "7", "title": "No file", "addedAt": 300}]}})
    got = library.PlexLibrary(stub.url, "t").recent_movies(2)
    assert [m["title"] for m in got] == ["New", "Old"]
    assert stub.seen("/library/sections/2/recentlyAdded")[0].query["X-Plex-Container-Size"] == ["2"]


def test_jellyfin_recent_films(stub):
    stub.route("GET", "/Items", {"Items": [
        {"Id": "a", "Name": "New", "Path": "/m/a.mkv", "DateCreated": "2024-02-01T00:00:00Z"}]})
    got = library.JellyfinLibrary(stub.url, "k").recent_movies(5)
    assert [m["title"] for m in got] == ["New"]
    q = stub.seen("/Items")[0].query
    assert q["SortBy"] == ["DateCreated"] and q["SortOrder"] == ["Descending"] and q["Limit"] == ["5"]
