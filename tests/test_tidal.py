"""Tests for the Tidal service (no network needed): a small invented catalogue answers the calls.

    python -m unittest discover -s tests -v
"""
import base64
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dynamicswar import core, discover, editions, history, server, tidal
from dynamicswar.client import Halted, Refused

TOKEN = "TESTTOKEN-1234567890"
SONGS = [f"Song {c}" for c in "ABCDEFGH"]


def jwt(**said):
    body = base64.urlsafe_b64encode(json.dumps(said).encode()).decode().rstrip("=")
    return f"aaa.{body}.zzz"


class FakeTidal:
    """Answers like api.tidal.com/v1 for an album in several releases:
    100  Tide Tables                    16/44.1  loud master
    200  Tide Tables (Remastered)       24/96    quieter, more dynamic master
    400  Tide Tables (Deluxe Edition)   16/44.1  the master of 100, plus six bonus tracks
    500  Tide Tables (Live)             a live album with the same songs - a candidate, no edition
    300  Tide Tables                    a single"""

    def __init__(self, refuse_after=None, refuse_with=(403, '{"status":403,"subStatus":4005,"userMessage":"Asset is not ready for playback"}')):
        self.sent = []                  # (time, path, query)
        self.refuse_after, self.refuse_with = refuse_after, refuse_with
        self.unknown = set()            # first path parts answered with 404
        artist = {"id": 7, "name": "Band", "type": "MAIN"}

        def release(one, title, year, tags=("LOSSLESS",), kind="ALBUM", count=8, cover="aaaa-bbbb-cccc"):
            return {"id": one, "title": title, "version": None, "type": kind, "artist": artist, "artists": [artist],
                    "numberOfTracks": count, "numberOfVolumes": 1, "releaseDate": f"{year}-03-04",
                    "streamStartDate": "2015-01-01T00:00:00.000+0000", "copyright": f"(P) {year} Label",
                    "cover": cover, "upc": str(one), "mediaMetadata": {"tags": list(tags)}}

        self.albums = {
            100: release(100, "Tide Tables", 1991),
            200: release(200, "Tide Tables (Remastered)", 2011, ("LOSSLESS", "HIRES_LOSSLESS"), cover="dddd-eeee-ffff"),
            400: release(400, "Tide Tables (Deluxe Edition)", 2001, count=14),
            500: release(500, "Tide Tables (Live)", 1993),
            300: release(300, "Tide Tables", 1991, kind="SINGLE", count=1, cover=None),
        }
        self.tracks, self.play = {}, {}
        specs = {100: (-9.0, 0.99, 16, 44100, "LOSSLESS", SONGS, 0),
                 200: (-4.0, 0.80, 24, 96000, "HI_RES_LOSSLESS", SONGS, 0),
                 400: (-9.0, 0.99, 16, 44100, "LOSSLESS", SONGS + [f"Bonus {c}" for c in "KLMNOP"], 0),
                 500: (-7.0, 0.90, 16, 44100, "LOSSLESS", SONGS, 60)}
        for album, (gain, peak, bits, rate, quality, names, longer) in specs.items():
            rows = []
            for n, name in enumerate(names, 1):
                one = album * 100 + n
                live = album == 500
                rows.append({"id": one, "title": name, "version": "Live" if live else None,
                             "duration": 200 + n + longer, "trackNumber": n, "volumeNumber": 1,
                             "isrc": f"US{'LIV' if live else 'AAA'}91{n:05d}", "replayGain": gain - 1, "peak": peak,
                             "audioQuality": "LOSSLESS", "streamReady": True, "allowStreaming": True,
                             "album": {"id": album, "title": self.albums[album]["title"],
                                       "cover": self.albums[album]["cover"]},
                             "artist": artist, "artists": [artist]})
                self.play[one] = {"trackId": one, "audioQuality": quality, "audioMode": "STEREO",
                                  "bitDepth": bits, "sampleRate": rate, "trackReplayGain": gain - 0.1 * (n % 3),
                                  "trackPeakAmplitude": peak, "albumReplayGain": gain - 1,
                                  "albumPeakAmplitude": peak, "manifestMimeType": "application/dash+xml",
                                  "manifest": "STREAM-ACCESS-SECRET", "manifestHash": "HASH-SECRET"}
            self.tracks[album] = rows

    def playback(self):
        return [p for _, p, _ in self.sent if p.endswith("playbackinfo")]

    def paths(self):
        return [p for _, p, _ in self.sent]

    def __call__(self, url, headers=None, data=None, timeout=30):
        if (headers or {}).get("Authorization") != "Bearer " + TOKEN:
            err = core.ApiError(401, "Unauthorized")
            err.body = '{"status":401,"subStatus":11002,"userMessage":"Token could not be verified"}'
            raise err
        path, _, query = url.partition("?")
        path = path.split("/v1/")[1]
        q = dict(urllib.parse.parse_qsl(query))
        self.sent.append((time.monotonic(), path, q))
        part = path.split("/")
        if part[0] in self.unknown or path in self.unknown:
            raise core.ApiError(404, "Not Found")

        def page(items):
            off, limit = int(q.get("offset", 0)), int(q.get("limit", 50))
            return {"limit": limit, "offset": off, "totalNumberOfItems": len(items), "items": items[off:off + limit]}

        if path == "search":
            kind = q["types"]
            if kind == "ALBUMS":
                return json.dumps({"albums": page(list(self.albums.values()))}).encode()
            if kind == "ARTISTS":
                return json.dumps({"artists": page([{"id": 7, "name": "Band"}])}).encode()
            if kind == "TRACKS":
                rows = [t for rows in self.tracks.values() for t in rows if t["title"].lower() in q["query"].lower()]
                # like the real search: other songs of the artist come along
                other = {"id": 60001, "title": "Another Song", "version": None, "duration": 180, "isrc": "USZZZ9100001",
                         "album": {"id": 600, "title": "Another Album", "cover": None},
                         "artist": {"id": 7, "name": "Band"}}
                return json.dumps({"tracks": page(rows + [other])}).encode()
        if part[0] == "albums" and len(part) == 2:
            return json.dumps(self.albums[int(part[1])]).encode()
        if part[0] == "albums" and part[2] == "tracks":
            return json.dumps(page(self.tracks[int(part[1])])).encode()
        if part[0] == "artists" and part[2] == "albums":
            return json.dumps(page(list(self.albums.values()))).encode()
        if path == "tracks" and "isrc" in q:
            rows = [t for rows in self.tracks.values() for t in rows if t["isrc"] == q["isrc"]]
            return json.dumps(page(rows)).encode()
        if part[0] == "tracks" and part[-1] == "playbackinfo":
            if self.refuse_after is not None and len(self.playback()) > self.refuse_after:
                err = core.ApiError(self.refuse_with[0], "refused")
                err.body = self.refuse_with[1]
                raise err
            return json.dumps(self.play[int(part[1])]).encode()
        if part[0] == "users" and part[2:] == ["favorites", "albums"]:
            return json.dumps(page([{"created": "2026-01-01", "item": self.albums[200]},
                                    {"created": "2025-01-01", "item": self.albums[100]}])).encode()
        raise core.ApiError(404, "Not Found")


def connected(fake, **kw):
    cfg = {"tidal": {"token": TOKEN, "country": "DE", "user_id": 42}}
    kw.setdefault("playback_rate", None)
    client = tidal.TidalClient(cfg, fetch=fake, pause=0, **kw)
    client.BASE_RATE = None             # the tests do not wait between calls
    return client


def whole(client, *ids):
    """The albums with the loudness of all their tracks, as the ranking of a plain album needs it."""
    albums = [client.album(i) for i in ids]
    client.measure([(a, [t["id"] for t in a["tracks"]["items"]]) for a in albums])
    return albums


class TidalClientTests(unittest.TestCase):
    def test_album_comes_without_the_delicate_call(self):
        fake = FakeTidal()
        client = connected(fake)
        album = client.album(200)
        self.assertEqual(fake.playback(), [])
        self.assertEqual((album["id"], album["title"], album["artist"]), ("200", "Tide Tables (Remastered)",
                                                                           {"id": 7, "name": "Band"}))
        self.assertEqual(album["url"], "https://tidal.com/album/200")
        self.assertEqual(album["image"]["large"], "https://resources.tidal.com/images/dddd/eeee/ffff/640x640.jpg")
        first = album["tracks"]["items"][0]
        self.assertEqual(first["audio_info"], {"replaygain_track_gain": None, "replaygain_track_peak": 0.80})
        self.assertEqual((first["isrc"], first["duration"]), ("USAAA9100001", 201))
        self.assertEqual((album["maximum_bit_depth"], album["maximum_sampling_rate"]), (None, None))   # not guessed
        client.album(200)
        self.assertEqual(fake.paths().count("albums/200/tracks"), 1)       # kept in memory

    def test_measured_album_in_the_form_the_comparison_reads(self):
        fake = FakeTidal()
        client = connected(fake)
        album, = whole(client, 200)
        self.assertEqual((album["maximum_bit_depth"], album["maximum_sampling_rate"]), (24, 96.0))
        self.assertEqual(album["tracks"]["items"][0]["audio_info"],
                         {"replaygain_track_gain": -4.1, "replaygain_track_peak": 0.80})
        d = core.describe(album)
        self.assertEqual((d["format"], d["year"], len(d["tracks"])), ("24/96", "2011", 8))
        self.assertIsNotNone(d["tracks"]["song a"]["plr"])
        self.assertEqual(d["album_gain"], -5.0)             # Tidal's album value, not derived from tracks
        self.assertEqual(len(fake.playback()), 8)           # one loudness call per track

    def test_only_the_album_tracks_of_real_editions_are_read(self):
        fake = FakeTidal()
        client = connected(fake)
        albums, _, _ = discover.discover(client, "Band", "Tide Tables", 7, ["100"], workers=2)
        self.assertEqual(sorted(a["id"] for a in albums), ["100", "200", "400", "500"])   # the live album is a candidate
        self.assertEqual(fake.playback(), [])               # finding editions costs no loudness call
        plan = editions.needed(albums, "Tide Tables")
        self.assertEqual(sorted(a["id"] for a, _ in plan), ["100", "200", "400"])
        self.assertEqual([len(ids) for _, ids in plan], [8, 8, 8])
        client.measure(plan)
        asked = sorted(int(p.split("/")[1]) for p in fake.playback())
        self.assertEqual(len(asked), 24)                    # 3 editions x 8 album tracks
        self.assertFalse([t for t in asked if t // 100 == 500])             # nothing of the live album
        self.assertFalse([t for t in asked if t // 100 == 400 and t % 100 > 8])    # no bonus track
        result = editions.compare(albums, "Tide Tables", "Tidal")
        ranked = result["groups"][0]["ranked"]
        self.assertEqual([v["id"] for v in ranked], ["200", "400", "100"])
        self.assertEqual([v["format"] for v in ranked], ["24/96", "16/44.1", "16/44.1"])
        self.assertGreater(ranked[0]["dynamics"] - ranked[1]["dynamics"], 2.5)
        self.assertEqual(ranked[0]["url"], "https://tidal.com/album/200")
        self.assertEqual([v["id"] for v in result["excluded"]], ["500"])

    def test_format_is_that_of_the_album_tracks_and_the_lowest_when_mixed(self):
        fake = FakeTidal()
        for n in range(9, 15):                              # the bonus tracks of the deluxe edition: 24/96
            fake.play[40000 + n].update(bitDepth=24, sampleRate=96000, audioQuality="HI_RES_LOSSLESS")
        fake.play[20003].update(bitDepth=16, sampleRate=44100, audioQuality="LOSSLESS")     # one track of 200 in CD quality
        client = connected(fake)
        deluxe, remaster = whole(client, 400, 200)          # every track is read, bonus material too
        album_ids = [t["id"] for t in deluxe["tracks"]["items"][:8]]
        client.measure([(deluxe, album_ids)])
        self.assertEqual((deluxe["maximum_bit_depth"], deluxe["maximum_sampling_rate"]), (16, 44.1))
        self.assertIsNone(deluxe["format_mixed"])
        self.assertEqual((remaster["maximum_bit_depth"], remaster["maximum_sampling_rate"]), (16, 44.1))
        self.assertEqual(remaster["format_mixed"], ["16/44.1", "24/96"])

    def test_release_played_from_other_releases_is_not_ranked(self):
        fake = FakeTidal()
        for n, gain in zip(range(1, 9), (-2.0, -5.0, -2.0, -5.0, -9.5, -5.0, -2.0, -5.0)):
            fake.play[20000 + n]["albumReplayGain"] = gain  # 200 gets its tracks from three releases
        client = connected(fake)
        albums = [client.album(i) for i in (100, 200)]
        client.measure(editions.needed(albums, "Tide Tables"))
        self.assertEqual({t["source"] for t in albums[0]["tracks"]["items"]}, {-10.0})
        self.assertEqual(albums[0]["loudest_gain"], -10.0)
        self.assertIsNone(albums[1]["loudest_gain"])        # the values of the other releases say nothing here
        self.assertEqual(core.describe(albums[1])["album_gain"], -4.2)     # its own loudest track
        result = editions.compare(albums, "Tide Tables", "Tidal")
        self.assertEqual([v["id"] for v in result["groups"][0]["ranked"] + result["groups"][0]["unranked"]], ["100"])
        self.assertIn("tracks of 3 different releases", result["excluded"][0]["excluded_reason"])

    def test_albums_seen_in_a_list_need_one_call_less(self):
        fake = FakeTidal()
        client = connected(fake)
        client.find_albums("Band Tide Tables")
        client.album(100)
        self.assertNotIn("albums/100", fake.paths())
        self.assertIn("albums/100/tracks", fake.paths())

    def test_stream_access_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = tidal.TrackStore(Path(tmp) / "tidal.json")
            album, = whole(connected(FakeTidal(), store=store), 100)
            text = json.dumps(album) + (Path(tmp) / "tidal.json").read_text(encoding="utf-8")
            for word in ("SECRET", "manifest", TOKEN):
                self.assertNotIn(word, text)

    def test_every_track_is_read_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tidal.json"
            whole(connected(FakeTidal(), store=tidal.TrackStore(path)), 100)
            fake = FakeTidal()                              # next session: the file answers
            client = connected(fake, store=tidal.TrackStore(path))
            album = client.album(100)                       # values are there without measuring
            self.assertEqual(album["tracks"]["items"][0]["audio_info"]["replaygain_track_gain"], -9.1)
            whole(client, 100)
            self.assertEqual(album["maximum_sampling_rate"], 44.1)
            self.assertEqual(fake.playback(), [])
            self.assertEqual(client.diagnostics()["tracks_from_file"], 8)

    def test_loudness_calls_keep_their_distance(self):
        fake = FakeTidal()
        client = connected(fake, playback_rate=25.0)        # 40 ms apart, to keep the test short
        whole(client, 100, 200)
        times = sorted(t for t, p, _ in fake.sent if p.endswith("playbackinfo"))
        self.assertEqual(len(times), 16)
        self.assertGreaterEqual(min(b - a for a, b in zip(times, times[1:])), 0.039)

    def test_two_loudness_calls_a_second_by_default(self):
        self.assertEqual(tidal.PLAYBACK_RATE, 2.0)
        fake = FakeTidal()
        client = tidal.TidalClient({"tidal": {"token": TOKEN}}, fetch=fake, pause=0)   # as the app runs it
        album = client.album(100)
        mark = client.log.mark()
        client.measure([(album, [t["id"] for t in album["tracks"]["items"][:5]])])
        times = [t for t, p, _ in fake.sent if p.endswith("playbackinfo")]
        self.assertGreaterEqual(min(b - a for a, b in zip(times, times[1:])), 0.49)
        self.assertLess(times[-1] - times[0], 2.3)          # five calls: four gaps of half a second
        self.assertLessEqual(client.log.since(mark)["peak_per_second"], 2)

    def test_first_refusal_stops_the_loudness_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tidal.json"
            fake = FakeTidal(refuse_after=3)
            client = connected(fake, store=tidal.TrackStore(path))
            mark = client.log.mark()
            with self.assertRaises(Refused) as caught:
                whole(client, 100)
            self.assertNotIsInstance(caught.exception, Halted)
            self.assertIn("HTTP 403", caught.exception.message)
            self.assertIn("4005", caught.exception.message)
            self.assertEqual(len(fake.playback()), 4)       # three answered, the fourth refused
            self.assertGreater(client.blocked(), 800)
            with self.assertRaises(Refused):
                whole(client, 200)
            self.assertEqual(len(fake.playback()), 4)       # nothing more was sent
            # the three values are kept, and a restart does not lift the pause
            again = tidal.TrackStore(path)
            self.assertEqual(len(again.tracks), 3)
            self.assertGreater(connected(FakeTidal(), store=again).blocked(), 800)
            # what Tidal said is in the log
            self.assertIn("4005", client.log.since(mark)["events"][-1]["detail"])

    def test_expired_token_asks_for_login_and_does_not_block(self):
        fake = FakeTidal(refuse_after=0, refuse_with=(
            401, '{"status":401,"subStatus":11003,"userMessage":"The token has expired. (Expired on time)"}'))
        client = connected(fake)
        with self.assertRaises(core.ApiError) as caught:
            whole(client, 100)
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIsInstance(caught.exception, Refused)
        self.assertEqual(client.blocked(), 0)

    def test_server_error_is_no_refusal(self):
        client = connected(FakeTidal(refuse_after=0, refuse_with=(503, "busy")))
        with self.assertRaises(Refused):
            whole(client, 100)
        self.assertEqual(client.blocked(), 0)

    def test_stop_ends_the_reading_and_keeps_what_was_read(self):
        fake = FakeTidal()
        client = connected(fake)
        album = client.album(100)
        seen = []
        with self.assertRaises(Halted):
            client.measure([(album, [t["id"] for t in album["tracks"]["items"]])],
                           progress=lambda stage, done, total: seen.append((stage, done, total)),
                           stop=lambda: len(fake.playback()) >= 3)
        self.assertEqual(len(fake.playback()), 3)
        self.assertEqual(seen, [("measure", 0, 8), ("measure", 1, 8), ("measure", 2, 8)])
        self.assertEqual(len(client.store.tracks), 3)
        self.assertEqual(client.blocked(), 0)

    def test_limit_per_comparison(self):
        fake = FakeTidal()
        client = connected(fake, max_calls=5)
        with self.assertRaises(Halted) as caught:
            whole(client, 100)
        self.assertIn("needs 8 loudness requests", caught.exception.message)
        self.assertEqual(len(fake.playback()), 5)
        whole(client, 100)                                  # the second start reads the rest
        self.assertEqual(len(fake.playback()), 8)

    def test_token_from_the_web_player(self):
        fake, saved = FakeTidal(), []
        client = tidal.TidalClient({}, fetch=fake, save=saved.append, pause=0)
        self.assertFalse(client.connected())
        with self.assertRaises(core.ApiError):
            client.set_token("Bearer wrong")
        self.assertFalse(client.connected())
        self.assertEqual(saved, [])
        client.set_token(f'Authorization: Bearer {TOKEN}')
        self.assertTrue(client.connected())
        self.assertEqual(client.cfg["tidal"]["token"], TOKEN)
        client.disconnect()
        self.assertNotIn("tidal", client.cfg)

    def test_token_says_country_user_and_expiry(self):
        self.assertEqual(tidal.claims(jwt(uid=42, cc="AT", exp=99))["cc"], "AT")
        self.assertEqual(tidal.claims("no-jwt"), {})
        fake = FakeTidal()
        client = tidal.TidalClient({}, fetch=fake, pause=0)
        with self.assertRaises(core.ApiError) as caught:
            client.set_token(jwt(uid=42, cc="AT", exp=time.time() - 60))
        self.assertIn("already expired", caught.exception.message)
        self.assertEqual(fake.sent, [])                     # nothing was sent for it
        cfg = {"tidal": {"token": TOKEN, "country": "AT", "expires": time.time() - 5}}
        self.assertFalse(tidal.TidalClient(cfg, fetch=fake).connected())

    def test_lists_leave_out_singles_and_name_no_sampling_rate(self):
        client = connected(FakeTidal())
        found = client.find_albums("Band Tide Tables")
        self.assertEqual([a["id"] for a in found], ["100", "200", "400", "500"])
        self.assertEqual([(a["maximum_bit_depth"], a["maximum_sampling_rate"]) for a in found][:2],
                         [(None, None), (24, None)])
        group = discover.group_albums(found)[0]
        self.assertEqual((group["ids"], group["format"], group["year"]), (["100", "200", "400", "500"], "24-bit", 1991))

    def test_calls_tidal_does_not_know_are_tried_once(self):
        fake = FakeTidal()
        fake.unknown.add("artists")
        client = connected(fake)
        self.assertEqual(client.releases(None, "Band", 7), [])
        self.assertEqual(client.releases(None, "Band", 7), [])
        self.assertEqual(sum(1 for p in fake.paths() if p.startswith("artists/")), 1)
        self.assertEqual(client.diagnostics()["unknown_calls"], {"artist albums": 404})

    def test_track_search_isrc_and_favourites(self):
        fake = FakeTidal()
        client = connected(fake)
        self.assertEqual({a["id"] for a in client.albums_with_track("Band Song A")}, {"100", "200", "400", "500"})
        self.assertEqual({a["id"] for a in client.albums_with_track("USAAA9100001")}, {"100", "200", "400"})
        self.assertIn("tracks", fake.paths())
        items, shape = client.favourite_albums()
        self.assertEqual([a["id"] for a in items], ["200", "100"])
        self.assertEqual(shape["row_keys"], ["created", "item"])
        self.assertIn("users/42/favorites/albums", fake.paths())

    def test_never_more_than_four_calls_a_second(self):
        fake = FakeTidal()
        client = tidal.TidalClient({"tidal": {"token": TOKEN}}, fetch=fake, pause=0)
        mark = client.log.mark()
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda n: client.find_albums("x"), range(7)))
        self.assertLessEqual(client.log.since(mark)["peak_per_second"], 4)

    def test_endpoint_names_carry_no_ids_and_no_query(self):
        name = tidal.endpoint_of
        self.assertEqual(name("https://api.tidal.com/v1/tracks/1781828/playbackinfo?countryCode=DE"),
                         "tracks/{id}/playbackinfo")
        self.assertEqual(name("https://api.tidal.com/v1/albums/1781800?countryCode=DE"), "albums/{id}")
        self.assertEqual(name("https://api.tidal.com/v1/search?query=x"), "search")


class TidalServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeTidal()
        root = Path(self.tmp.name)
        self.cfg = {"user_auth_token": "QOBUZ", "app_id": "1", "tidal": {"token": TOKEN, "country": "DE"}}
        self.state = server.State(cfg=self.cfg, save=lambda cfg: None, fetch=self.fake, service="tidal",
                                  report_dir=root / "debug", library_path=root / "library.json",
                                  history_path=root / "history.json", tidal_path=root / "tidal.json")
        self.state.client.playback_rate = self.state.client.BASE_RATE = None
        self.httpd, self.base = server.serve(self.state)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base.rstrip("/") + path, data=data,
                                     headers={"X-Session": self.state.session})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def compare(self):
        status, started = self.call("/api/compare", {"artist": "Band", "artist_id": 7, "title": "Tide Tables",
                                                      "ids": ["100", "200"]})
        self.assertEqual(status, 200)
        for _ in range(500):
            job = self.call("/api/job/" + started["job"])[1]
            if job["state"] != "running":
                return job
            time.sleep(0.02)
        self.fail("comparison did not end")

    def test_status_names_the_service(self):
        status = self.call("/api/status")[1]
        self.assertEqual(status["service"], {"name": "tidal", "label": "Tidal", "library": True,
                                             "loudness_rate": None})        # switched off in setUp
        self.assertTrue(status["connected"])

    def test_search_compare_report_and_timeline(self):
        groups = self.call("/api/search?q=Band%20Tide%20Tables")[1]["groups"]
        self.assertEqual(groups[0]["ids"], ["100", "200", "400", "500"])
        job = self.compare()
        self.assertEqual(job["state"], "done", job.get("error"))
        result = job["result"]
        self.assertEqual(result["service"], "tidal")
        self.assertEqual([v["id"] for v in result["groups"][0]["ranked"]], ["200", "400", "100"])
        # the report stands next to a Qobuz report of the same album, without token or stream access
        path = Path(self.tmp.name) / "debug" / "Band - Tide Tables [tidal].json"
        text = path.read_text(encoding="utf-8")
        for word in (TOKEN, "SECRET", "manifest"):
            self.assertNotIn(word, text)
        report = json.loads(text)
        self.assertEqual(report["service"], "tidal")
        self.assertEqual(report["requests"]["tracks/{id}/playbackinfo"], 24)     # 3 editions x 8 album tracks
        self.assertEqual(report["service_notes"]["loudness_calls"], 24)
        self.assertEqual(report["albums"][0]["tracks"]["items"][0]["tidal"]["bits"], 16)
        self.assertIn("measure", result["timing"])
        # the timeline keeps the services apart
        albums = self.call("/api/timeline")[1]["albums"]
        self.assertEqual([(a["service"], a["best"]["id"]) for a in albums], [("tidal", "200")])
        self.assertEqual(self.state.history.albums("qobuz"), [])
        # ... and a report read in again lands with its service
        fresh = history.Store()
        history.import_reports(fresh, path.parent)
        self.assertEqual([a["service"] for a in fresh.albums()], ["tidal"])
        # second comparison: every value comes from the file
        self.state.client.forget()
        before = len(self.fake.playback())
        self.assertEqual(self.compare()["state"], "done")
        self.assertEqual(len(self.fake.playback()), before)
        # ... and the report counts this comparison, not the session
        notes = json.loads(path.read_text(encoding="utf-8"))["service_notes"]
        self.assertEqual((notes["loudness_calls"], notes["tracks_from_file"]), (0, 24))
        self.assertEqual(notes["session_loudness_calls"], 24)

    def test_refusal_ends_the_comparison_with_a_clear_message(self):
        self.fake.refuse_after = 9
        job = self.compare()
        self.assertEqual(job["state"], "error")
        self.assertIn("Tidal turned down a loudness request (HTTP 403", job["error"]["message"])
        self.assertGreater(self.call("/api/status")[1]["throttled"], 800)
        names = [p.name for p in (Path(self.tmp.name) / "debug").iterdir()]
        self.assertTrue(any(n.startswith("_tidal refused") for n in names), names)
        self.assertFalse(any("[tidal]" in n for n in names))
        self.assertEqual(self.state.history.albums(), [])

    def test_two_windows_share_the_timeline_file(self):
        path = Path(self.tmp.name) / "shared.json"
        one, two = history.Store(path), history.Store(path)     # the Qobuz window and the Tidal window
        entry = lambda key, service: {"key": key, "service": service, "artist": "A", "album": key, "year": 1990,
                                      "best": {"id": key}, "when": "2026-11-25 20:00:00"}
        one.add(entry("a", "qobuz"))
        two.add(entry("b|tidal", "tidal"))
        one.add(entry("c", "qobuz"))
        self.assertEqual(sorted(history.Store(path).items), ["a", "b|tidal", "c"])

    def test_a_comparison_can_be_stopped(self):
        self.state.client.playback_rate = 20.0              # 24 tracks would take more than a second
        status, started = self.call("/api/compare", {"artist": "Band", "artist_id": 7, "title": "Tide Tables",
                                                      "ids": ["100", "200"]})
        for _ in range(400):
            job = self.call("/api/job/" + started["job"])[1]
            if job["stage"] == "measure" or job["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual((job["state"], job["stage"], job["total"]), ("running", "measure", 24))
        self.call("/api/compare/stop", {"job": started["job"]})
        for _ in range(200):
            job = self.call("/api/job/" + started["job"])[1]
            if job["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(job["state"], "error")
        self.assertIn("stopped", job["error"]["message"])
        self.assertLess(len(self.fake.playback()), 24)
        names = [p.name for p in (Path(self.tmp.name) / "debug").iterdir()] if (Path(self.tmp.name) / "debug").exists() else []
        self.assertEqual(names, [])                         # stopping is no refusal and no result

    def test_closing_the_app_ends_the_reading(self):
        self.state.client.playback_rate = 20.0
        started = self.call("/api/compare", {"artist": "Band", "artist_id": 7, "title": "Tide Tables",
                                              "ids": ["100"]})[1]
        for _ in range(400):
            if self.call("/api/job/" + started["job"])[1]["stage"] == "measure":
                break
            time.sleep(0.01)
        self.state.closing = True
        time.sleep(0.3)
        sent = len(self.fake.playback())
        time.sleep(0.3)
        self.assertEqual(len(self.fake.playback()), sent)   # nothing goes out any more
        self.assertLess(sent, 24)

    def run_library(self, ids):
        status, started = self.call("/api/library/check", {"ids": ids})
        self.assertEqual(status, 200, started)
        for _ in range(600):
            job = self.call("/api/job/" + started["job"])[1]
            if job["state"] != "running":
                return job
            time.sleep(0.02)
        self.fail("library run did not end")

    def test_library_reads_the_favourites_and_checks_them(self):
        self.cfg["tidal"]["user_id"] = 42
        self.state.library_pause, self.state.library_rate = 0, 0
        status, lib = self.call("/api/library")
        self.assertEqual(status, 200, lib)
        self.assertEqual([a["id"] for a in lib["albums"]], ["200", "100"])
        self.assertEqual((lib["lazy"], lib["budget"], lib["paused"]), (True, 300, 0))
        self.assertEqual([a["format"] for a in lib["albums"]], ["24-bit", ""])     # no invented sampling rate
        job = self.run_library(["100", "200"])
        self.assertEqual((job["state"], job["done"]), ("done", 2), job)
        checks = {r["id"]: r["check"] for r in job["results"]}
        self.assertEqual((checks["100"]["kind"], checks["100"]["best"]["id"]), ("master", "200"))
        self.assertGreater(checks["100"]["gain"], 2.5)
        self.assertEqual(checks["200"]["kind"], "best")
        self.assertEqual(len(self.fake.playback()), 24)     # the second album needed no new values
        # kept in a file of its own, next to the Qobuz results
        root = Path(self.tmp.name)
        self.assertEqual(sorted(json.loads((root / "library-tidal.json").read_text())["checked"]), ["100", "200"])
        self.assertFalse((root / "library.json").exists())
        report = next(p for p in (root / "debug").iterdir() if p.name.startswith("_library run"))
        self.assertTrue(report.name.endswith("[tidal].json"))
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual((data["service"], data["loudness_calls"]), ("tidal", 24))
        self.assertEqual([r["loudness_calls"] for r in data["albums"]], [24, 0])
        self.assertNotIn(TOKEN, report.read_text(encoding="utf-8"))

    def test_library_run_ends_after_its_share_of_loudness_requests(self):
        self.cfg["tidal"]["user_id"] = 42
        self.state.library_pause, self.state.library_rate = 0, 0
        self.state.client.LIBRARY_CALLS = 10
        self.call("/api/library")
        job = self.run_library(["100", "200"])
        self.assertEqual((job["state"], job["done"]), ("done", 1))
        self.assertIn("ends after 10 loudness requests (24 were sent)", job["message"])
        self.assertEqual([r["id"] for r in job["results"]], ["100"])

    def test_library_run_stops_when_tidal_turns_a_request_down(self):
        self.cfg["tidal"]["user_id"] = 42
        self.state.library_pause, self.state.library_rate = 0, 0
        self.fake.refuse_after = 5
        self.call("/api/library")
        job = self.run_library(["100", "200"])
        self.assertEqual((job["state"], job["results"]), ("stopped", []))
        self.assertIn("Tidal turned a request down after 0 of 2 albums", job["message"])
        self.assertEqual(len(self.fake.playback()), 6)      # nothing was sent after the refusal
        self.assertGreater(self.call("/api/library")[1]["paused"], 800)
        status, answer = self.call("/api/library/check", {"ids": ["100"]})
        self.assertEqual(status, 400)
        self.assertIn("paused", answer["error"])

    def test_small_gain_on_a_192_khz_release_is_marked_as_uncertain(self):
        from dynamicswar import library

        def result(mine_rate, best_rate, gain):
            ed = lambda i, rank, rate, dyn: {"id": i, "rank": rank, "label": "Album", "format": f"24/{rate:g}", "bits": 24,
                                             "rate": rate, "year": "1990", "dynamics": dyn, "total": 10, "same_as": None}
            return {"service": "tidal", "groups": [{"ranked": [ed("b", 1, best_rate, 9.0), ed("m", 2, mine_rate, 9.0 - gain)],
                                                   "unranked": []}], "excluded": [], "partials": []}
        self.assertIn("192 kHz", library.outcome(result(192, 96, 1.1), "m", "Tidal")["unsure"])
        self.assertNotIn("unsure", library.outcome(result(192, 96, 2.4), "m", "Tidal"))     # a clear difference
        self.assertNotIn("unsure", library.outcome(result(96, 44.1, 1.1), "m", "Tidal"))    # no 192 kHz release
        self.assertNotIn("unsure", library.outcome(dict(result(192, 96, 1.1), service="qobuz"), "m"))

    def test_login_and_logout_touch_only_tidal(self):
        self.assertEqual(self.call("/api/logout", {})[1]["connected"], False)
        self.assertEqual(self.cfg["user_auth_token"], "QOBUZ")
        self.assertEqual(self.call("/api/login", {"token": "Bearer nope"})[0], 401)
        status, answer = self.call("/api/login", {"token": "Bearer " + TOKEN})
        self.assertEqual((status, answer["connected"]), (200, True))
        self.assertEqual(self.cfg["user_auth_token"], "QOBUZ")

    def test_two_windows_do_not_undo_each_others_sign_in(self):
        import os
        from dynamicswar import config
        with tempfile.TemporaryDirectory() as home:
            old_home, os.environ["DYNAMICSWAR_HOME"] = os.environ.get("DYNAMICSWAR_HOME"), home
            try:
                config.save({"user_auth_token": "Q1", "app_id": "1"})
                qobuz = server.State(fetch=self.fake, tidal_path=Path(home) / "t.json")
                other = server.State(fetch=self.fake, service="tidal", tidal_path=Path(home) / "t.json")
                other.login({"token": TOKEN})                   # sign in to Tidal in the second window
                qobuz.cfg["app_id"] = "2"
                qobuz.client.save(qobuz.cfg)                    # the first window saves something of its own
                self.assertEqual(config.load()["tidal"]["token"], TOKEN)
                self.assertEqual(config.load()["app_id"], "2")
                other.logout({})
                self.assertEqual(config.load(), {"user_auth_token": "Q1", "app_id": "2"})
            finally:
                if old_home is None:
                    os.environ.pop("DYNAMICSWAR_HOME", None)
                else:
                    os.environ["DYNAMICSWAR_HOME"] = old_home

    def test_settings_list_both_services_and_switch(self):
        self.cfg["tidal"]["expires"] = time.time() + 3600
        status, d = self.call("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual((d["active"], d["busy"]), ("tidal", False))
        by = {s["name"]: s for s in d["services"]}
        self.assertEqual((by["qobuz"]["active"], by["qobuz"]["connected"], by["qobuz"]["library"], by["qobuz"]["expires"]),
                         (False, True, True, None))
        self.assertEqual((by["tidal"]["active"], by["tidal"]["connected"], by["tidal"]["library"]), (True, True, True))
        self.assertGreater(by["tidal"]["expires"], time.time())
        self.assertEqual(by["tidal"]["stored_tracks"], 0)
        text = json.dumps(d)
        self.assertNotIn(TOKEN, text)                       # the settings never hand a token to the page
        self.assertNotIn("QOBUZ", text)
        # switch to Qobuz and back
        status, answer = self.call("/api/service", {"service": "qobuz"})
        self.assertEqual((status, answer["service"]["name"], self.cfg["service"]), (200, "qobuz", "qobuz"))
        self.assertEqual(self.call("/api/settings")[1]["active"], "qobuz")
        self.assertEqual(self.call("/api/service", {"service": "nope"})[0], 400)
        self.assertEqual(self.call("/api/service", {"service": "tidal"})[1]["service"]["name"], "tidal")

    def test_no_switch_while_a_comparison_runs(self):
        self.state.client.playback_rate = 20.0
        started = self.call("/api/compare", {"artist": "Band", "artist_id": 7, "title": "Tide Tables", "ids": ["100"]})[1]
        status, answer = self.call("/api/service", {"service": "qobuz"})
        self.assertEqual(status, 400)
        self.assertIn("is running", answer["error"])
        self.call("/api/compare/stop", {"job": started["job"]})
        for _ in range(300):
            if self.call("/api/job/" + started["job"])[1]["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(self.call("/api/service", {"service": "qobuz"})[0], 200)

    def test_sign_in_to_the_service_that_is_not_in_use(self):
        self.call("/api/service", {"service": "qobuz"})
        self.assertEqual(self.call("/api/logout", {"service": "tidal"})[1]["service"]["name"], "qobuz")
        self.assertNotIn("tidal", self.cfg)
        self.assertEqual(self.cfg["user_auth_token"], "QOBUZ")
        status, answer = self.call("/api/login", {"service": "tidal", "token": "Bearer nope"})
        self.assertEqual(status, 401)
        self.assertIn("Tidal rejected the token", answer["error"])      # names the service that was asked
        self.assertEqual(self.call("/api/login", {"service": "tidal", "token": TOKEN})[0], 200)
        self.assertEqual(self.cfg["tidal"]["token"], TOKEN)
        by = {s["name"]: s for s in self.call("/api/settings")[1]["services"]}
        self.assertTrue(by["tidal"]["connected"] and not by["tidal"]["active"])

    def test_qobuz_stays_the_default(self):
        state = server.State(cfg={"user_auth_token": "x"}, save=lambda cfg: None, fetch=self.fake)
        self.assertEqual(state.status()["service"]["name"], "qobuz")


if __name__ == "__main__":
    unittest.main()
