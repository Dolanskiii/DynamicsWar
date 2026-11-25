"""Tests of the engine with simulated Qobuz answers (no network needed).

    python -m unittest discover -s tests -v
"""
import json
import unittest
import urllib.parse

from dynamicswar import core as tp


def track(title, gain, peak=0.98, version=None, duration=240, isrc=None):
    return {"title": title, "version": version, "duration": duration, "isrc": isrc,
            "audio_info": {"replaygain_track_gain": gain, "replaygain_track_peak": peak}}


def album(album_id, title, tracks, bits=16, rate=44.1, version=None, artist="Aerosmith"):
    return {"id": album_id, "title": title, "version": version, "upc": f"88{album_id}",
            "artist": {"name": artist, "id": 7 if artist == "Aerosmith" else 99},
            "maximum_bit_depth": bits, "maximum_sampling_rate": rate,
            "release_date_original": "1973-01-05", "label": {"name": "Columbia", "id": 5},
            "tracks_count": len(tracks), "tracks": {"items": tracks}}


SONGS = ["Make It", "Somebody", "Dream On", "One Way Street"]
ALBUMS = {
    "v1": album("v1", "Aerosmith", [track(s, g) for s, g in zip(SONGS, [-6.0, -6.2, -5.8, -6.1])], 24, 96),
    "v2": album("v2", "Aerosmith", version="Legendary Expanded Edition", bits=24, rate=48, tracks=(
        [track(s, g, version="2024 Remaster") for s, g in zip(SONGS, [-7.0, -7.6, -7.1, -7.2])]
        + [track(s, -12.0, version="2024 Mix") for s in SONGS]
        + [track("Dream On (Live)", -9.0)])),
    "v3": album("v3", "Aerosmith", [track(s, g, isrc=f"USSM1{i}")
                                    for i, (s, g) in enumerate(zip(SONGS, [-7.7, -7.5, -7.2, -7.7]))]),
    "v4": album("v4", "Aerosmith", [track(s, g, isrc=f"USSM1{i}")
                                    for i, (s, g) in enumerate(zip(SONGS, [-6.0, -6.2, -5.8, -6.1]))]),
    "x1": album("x1", "Aerosmith's Greatest Hits", [track("Dream On", -9.9)]),
    "x2": album("x2", "Aerosmith", [track("Something Else", -3.0)], artist="Tribute Band"),
}
EDITIONS = ("v1", "v2", "v3", "v4")         # the releases that are editions of the album
BUNDLE = 'foo;production:{api:{appId:"123456789",appSecret:"' + "a" * 32 + '"}};bar'


class FakeQobuz:
    """Stands in for play.qobuz.com and the API, optionally with a login required."""

    def __init__(self, require_login=False, with_total=True, page_cap=50):
        self.require_login, self.calls = require_login, []
        self.with_total, self.page_cap = with_total, page_cap

    def block(self, items, params):
        off, lim = int(params["offset"]), min(int(params["limit"]), self.page_cap)
        out = {"items": items[off:off + lim]}
        if self.with_total:
            out["total"] = len(items)
        return out

    def __call__(self, url, headers=None, data=None, timeout=30):
        self.calls.append(url)
        headers = headers or {}
        u = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(data.decode() if data else u.query))
        if u.netloc == "play.qobuz.com":
            if u.path == "/login":
                return b'<html><script src="/resources/8.1.0-b019/bundle.js"></script></html>'
            return BUNDLE.encode()
        if headers.get("X-App-Id") != "123456789":
            raise tp.ApiError(400, "Invalid or missing app_id")
        endpoint = u.path.split("/0.2/")[1]
        if self.require_login and headers.get("X-User-Auth-Token") != "TOKEN":
            raise tp.ApiError(401, "User authentication is required")
        if endpoint == "user/get":
            if headers.get("X-User-Auth-Token") != "TOKEN":
                raise tp.ApiError(401, "User authentication is required")
            return b'{"id": 1}'
        if endpoint == "catalog/search":
            items = [{k: v for k, v in a.items() if k != "tracks"} for a in ALBUMS.values()]
            return json.dumps({"albums": self.block(items, params)}).encode()
        if endpoint == "album/get":
            if params["album_id"] not in ALBUMS:
                raise tp.ApiError(404, "No result matching given argument")
            return json.dumps(ALBUMS[params["album_id"]]).encode()
        raise tp.ApiError(404, "unknown endpoint")


def search(q):
    return q.paged("catalog/search", "albums", 1000, 50, query="x", type="albums")


def run(ids=EDITIONS, **kw):
    return tp.analyse([tp.describe(ALBUMS[i]) for i in ids], **kw)


class TitleTests(unittest.TestCase):
    def test_split(self):
        self.assertEqual(tp.split_title("Dream On (2024 Remaster)"), ("dream on", ["2024 remaster"]))
        self.assertEqual(tp.split_title("Brothers In Arms - Remastered 1996")[0], "brothers in arms")
        self.assertEqual(tp.split_title("Water Song - Janie's Got a Gun")[0], "water song janie s got a gun")
        self.assertEqual(tp.split_title("(I Can't Get No) Satisfaction")[0], "satisfaction")
        self.assertEqual(tp.split_title("(Untitled)")[0], "untitled")
        self.assertEqual(tp.split_title("Señorita & Co.")[0], "senorita and co")
        self.assertEqual(tp.split_title("Dream On", "2024 Mix")[1], ["2024 mix"])

    def test_variant(self):
        self.assertTrue(tp.is_variant(["2024 mix"]))
        self.assertTrue(tp.is_variant(["live at the garden"]))
        self.assertFalse(tp.is_variant(["2024 remaster"]))
        self.assertFalse(tp.is_variant(["remastered 1996"]))
        self.assertFalse(tp.is_variant(["album version"]))


class ArtistTests(unittest.TestCase):
    def test_same_artist(self):
        self.assertTrue(tp.same_artist("Black Sabbath", "black sabbath"))
        self.assertTrue(tp.same_artist("The Beatles", "Beatles"))
        self.assertTrue(tp.same_artist("Robert Plant & Alison Krauss", "Robert Plant"))
        self.assertFalse(tp.same_artist("Sabbath", "Black Sabbath"))        # part of the name is not enough
        self.assertFalse(tp.same_artist("Blacksabbathian", "Black Sabbath"))
        self.assertFalse(tp.same_artist("Nolan Allen", "Black Sabbath"))
        self.assertFalse(tp.same_artist("", "Black Sabbath"))

    def test_nested_artist_name(self):
        self.assertEqual(tp.artist_name({"artist": {"name": {"display": "Black Sabbath"}}}), "Black Sabbath")
        self.assertEqual(tp.artist_name({"artist": {"name": "Black Sabbath"}}), "Black Sabbath")
        self.assertIsNone(tp.artist_name({"artist": {}}))


class ApiTests(unittest.TestCase):
    def test_app_id_discovery_and_cache(self):
        fake, saved = FakeQobuz(), []
        q = tp.Qobuz({}, fetch=fake, save=lambda c: saved.append(dict(c)))
        self.assertEqual(q.app_id(), "123456789")
        self.assertEqual(saved[-1]["app_id"], "123456789")
        n = len(fake.calls)
        q.app_id()
        self.assertEqual(len(fake.calls), n)        # from the cache

    def test_stale_app_id_is_refreshed(self):
        q = tp.Qobuz({"app_id": "000000000"}, fetch=FakeQobuz())
        self.assertEqual(len(search(q)), len(ALBUMS))
        self.assertEqual(q.cfg["app_id"], "123456789")

    def test_token(self):
        saved = []
        q = tp.Qobuz({}, fetch=FakeQobuz(require_login=True), save=lambda c: saved.append(dict(c)))
        with self.assertRaises(tp.ApiError) as cm:
            search(q)
        self.assertEqual(cm.exception.status, 401)
        with self.assertRaises(tp.ApiError) as cm:
            q.set_token("WRONG")
        self.assertIn("rejected the token", cm.exception.message)
        self.assertNotIn("user_auth_token", q.cfg)
        self.assertFalse(any("user_auth_token" in c for c in saved))            # nothing wrong was stored
        with self.assertRaises(tp.ApiError) as cm:
            q.set_token("  ")
        self.assertIn("No token", cm.exception.message)
        q.set_token('  X-User-Auth-Token: TOKEN \n')                             # header line copied along
        self.assertEqual(saved[-1], {"app_id": "123456789", "user_auth_token": "TOKEN"})
        self.assertEqual(len(search(q)), len(ALBUMS))

    def test_paging_without_total_and_small_pages(self):
        for with_total in (True, False):
            q = tp.Qobuz({}, fetch=FakeQobuz(with_total=with_total, page_cap=4))
            self.assertEqual(len(search(q)), len(ALBUMS))

    def test_album(self):
        q = tp.Qobuz({}, fetch=FakeQobuz())
        self.assertEqual(q.album("v1")["title"], "Aerosmith")
        with self.assertRaises(tp.ApiError):
            q.album("nope")


class AnalyseTests(unittest.TestCase):
    def test_ranking(self):
        ranked, rest, shared = run()
        self.assertEqual([v["id"] for v in ranked], ["v1", "v4", "v2", "v3"])
        self.assertEqual(rest, [])
        self.assertEqual(len(shared), 4)
        self.assertAlmostEqual(ranked[2]["delta"], -1.2, places=6)              # the remaster, not the mix
        self.assertAlmostEqual(ranked[3]["delta"], -1.5, places=6)
        self.assertEqual(ranked[2]["variants"], 0)
        self.assertEqual(ranked[1]["same_as"], 1)                               # v4 = v1 in 16/44.1
        self.assertIsNone(ranked[2]["same_as"])
        self.assertEqual(ranked[2]["n"], 4)

    def test_single_track(self):
        ranked, _, shared = run(only_track="dream on")
        self.assertEqual(shared, ["dream on"])
        self.assertEqual(ranked[0]["id"], "v1")
        self.assertAlmostEqual(ranked[-1]["delta"], -1.4, places=6)

    def test_keep_all_rates_other_releases_too(self):
        ranked, _, _ = run(EDITIONS + ("x1",), keep_all=True)
        self.assertIn("x1", [v["id"] for v in ranked])
        ranked, rest, _ = run(EDITIONS + ("x1",))
        self.assertNotIn("x1", [v["id"] for v in ranked])                       # one track only: left out
        self.assertEqual([v["id"] for v in rest], ["x1"])

    def test_different_tracklists_stay_comparable(self):
        a = tp.describe(album("a", "X", [track("One", -5.0), track("Two", -6.0)]))
        b = tp.describe(album("b", "X", [track("One", -8.0), track("Three", -2.0)]))
        c = tp.describe(album("c", "X", [track("Two", -9.0), track("Three", -5.0)]))
        ranked, rest, _ = tp.analyse([a, b, c])
        self.assertEqual([v["id"] for v in ranked], ["a", "b", "c"])

    def test_missing_gain_and_variant_only(self):
        a = tp.describe(album("a", "X", [track("One", -5.0), track("Two", -6.0)]))
        b = tp.describe(album("b", "X", [track("One", None, None), track("Two", 0, 0)]))
        c = tp.describe(album("c", "X", [track("One (Live)", -7.0, duration=400), track("Two", -7.0)]))
        ranked, rest, _ = tp.analyse([a, b, c])
        self.assertEqual([v["id"] for v in ranked], ["a", "c"])
        self.assertEqual([v["id"] for v in rest], ["b"])
        self.assertEqual((ranked[1]["variants"], ranked[1]["odd_length"]), (1, 1))

    def test_loudest_track_of_the_whole_edition(self):
        # Roon in album mode = loudest track of the whole edition + 4 dB; for v2 the mix counts too
        ranked, _, _ = run()
        self.assertEqual([v["album_gain"] for v in ranked], [-6.2, -6.2, -12.0, -7.7])
        self.assertIsNone(tp.loudest_gain(album("z", "Z", [track("One", None, None), track("Two", 0, 0)])))
        self.assertEqual(tp.loudest_gain({"loudest_gain": -9.5, "tracks": {"items": []}}), -9.5)

    def test_core_tracks_only(self):
        # bonus material that only some of the versions have does not count
        base = [("One", -5.0), ("Two", -6.0), ("Three", -7.0)]
        a = tp.describe(album("a", "X", [track(s, g) for s, g in base]))
        b = tp.describe(album("b", "X", [track(s, g - 1) for s, g in base] + [track("Bonus (Live)", -1.0)]))
        c = tp.describe(album("c", "X", [track(s, g - 1.2) for s, g in base] + [track("Bonus (Live)", -1.5)]))
        ranked, _, shared = tp.analyse([c, b, a])
        self.assertEqual(shared, ["one", "three", "two"])
        self.assertEqual([round(v["delta"], 2) for v in ranked], [0, -1.0, -1.2])
        self.assertEqual([round(v["gain_mean"], 2) for v in ranked], [-6.0, -7.0, -7.2])
        self.assertEqual([v["variants"] for v in ranked], [0, 0, 0])
        # 1 dB louder at the same peak levels is no longer a shared master;
        # b and c are only 0.2 dB apart and still count as the same one
        self.assertEqual([v["same_as"] for v in ranked], [None, None, 2])

    def test_shifted_same_master(self):
        # real case "Get Your Wings": the CD edition is 0.20 dB louder on every track
        hi = [-7.04, -8.30, -7.94, -7.85, -5.97, -7.49, -7.00, -7.60]
        cd = [-7.24, -8.50, -8.14, -8.05, -6.17, -7.69, -7.20, -7.79]
        names = [f"Song {i}" for i in range(8)]
        a = tp.describe(album("a", "X", [track(n, g) for n, g in zip(names, hi)], 24, 96))
        b = tp.describe(album("b", "X", [track(n, g) for n, g in zip(names, cd)]))
        c = tp.describe(album("c", "X", [track(n, g - d) for n, g, d in
                                         zip(names, hi, [1.2, 1.5, 1.4, 0.7, 1.3, 0.9, 2.4, 1.7])]))
        ranked, rest, shared = tp.analyse([b, a])
        self.assertEqual((ranked[1]["id"], ranked[1]["same_as"]), ("b", 1))
        self.assertAlmostEqual(ranked[1]["offset"], -0.2, places=2)
        ranked, rest, shared = tp.analyse([c, b, a])                   # a real remaster stays on its own
        self.assertEqual([(v["id"], v["same_as"]) for v in ranked], [("a", None), ("b", 1), ("c", None)])

    def test_single_version_gets_values(self):
        # real values: Aerosmith 24/96
        gains = [-7.03, -8.53, -7.88, -8.10, -7.74, -9.02, -7.26, -8.29]
        names = ["Dream On", "Make It", "Mama Kin", "Movin' Out", "One Way Street", "Somebody",
                 "Walkin' The Dog", "Write Me a Letter"]
        a = tp.describe(album("a", "Aerosmith", [track(n, g) for n, g in zip(names, gains)]
                              + [track("Dream On (Live)", -12.0)], 24, 96))
        ranked, rest, shared = tp.analyse([a])
        self.assertEqual((ranked, len(rest), rest[0]["n"]), ([], 1, 8))        # the live take does not count
        self.assertAlmostEqual(rest[0]["gain_mean"], -7.98, places=2)
        self.assertEqual(rest[0]["album_gain"], -12.0)                         # album value incl. the live track

    def test_no_gain_data_at_all(self):
        b = tp.describe(album("b", "X", [track("One", None, None)]))
        ranked, rest, shared = tp.analyse([b])
        self.assertEqual((ranked, rest[0]["n"], rest[0]["gain_mean"]), ([], 0, None))

    def paranoid(self):
        # real case: the album plus wrongly assigned one-track covers of any loudness
        names = ["War Pigs", "Paranoid", "Planet Caravan", "Iron Man", "Electric Funeral", "Hand of Doom"]
        r2012 = [-2.5, -5.0, -1.0, -3.0, -2.6, -2.6]
        r2009 = [g - 1.0 - 0.1 * i for i, g in enumerate(r2012)]
        a = tp.describe(album("a", "Paranoid", [track(n, g) for n, g in zip(names, r2012)],
                              version="Remastered 2012", artist="Black Sabbath"))
        b = tp.describe(album("b", "Paranoid", [track(n, g) for n, g in zip(names, r2009)],
                              version="Remastered 2009", artist="Black Sabbath"))
        covers = [tp.describe(album(f"c{i}", "Paranoid", [track("Paranoid", g)], 24, 48, artist="Black Sabbath"))
                  for i, g in enumerate([-2.99, -4.05, -6.33, -6.71, -6.81])]
        return a, b, covers

    def test_singles_and_covers_do_not_distort(self):
        a, b, covers = self.paranoid()
        ranked, rest, shared = tp.analyse(covers + [b, a])
        self.assertEqual([v["id"] for v in ranked], ["a", "b"])
        self.assertEqual(len(shared), 6)
        self.assertEqual(sorted(v["id"] for v in rest), ["c0", "c1", "c2", "c3", "c4"])
        self.assertTrue(all(v["skipped"] for v in rest))
        ranked, rest, shared = tp.analyse(covers + [b, a], only_track="Paranoid")
        self.assertEqual([v["id"] for v in ranked], ["a", "b"])                 # in single-track mode too
        ranked, _, _ = tp.analyse(covers + [b, a], keep_all=True)               # rate everything
        self.assertEqual(len(ranked), 7)

    def test_one_album_plus_covers_is_single_version(self):
        a, _, covers = self.paranoid()
        ranked, rest, shared = tp.analyse(covers + [a])
        self.assertEqual(ranked, [])
        self.assertEqual(sum(1 for v in rest if v["skipped"]), 5)
        self.assertAlmostEqual(next(v for v in rest if v["id"] == "a")["plr_mean"], 15.0, places=1)

    def test_single_release_query_keeps_singles(self):
        _, _, covers = self.paranoid()                                          # one-track releases only: hide nothing
        ranked, rest, _ = tp.analyse(covers)
        self.assertEqual((len(ranked), rest), (5, []))

    def test_dynamics_metric(self):
        # dynamics = peak level (dBFS) - loudness (LUFS); the Qobuz gain refers to -18 LUFS
        tr = tp.pick_tracks(album("a", "X", [track("One", -8.0, 1.0), track("Two", -8.0, 0.5),
                                             track("Three", -8.0, None), track("Four", None, 0.9)]))
        self.assertAlmostEqual(tr["one"]["plr"], 10.0)
        self.assertAlmostEqual(tr["two"]["plr"], 10.0 - 6.0206, places=3)
        self.assertIsNone(tr["three"]["plr"])
        self.assertIsNone(tr["four"]["plr"])

    def test_ranking_follows_dynamics_not_loudness(self):
        # a is quieter (higher gain) but only turned down: its peaks are at -6 dBFS
        names = ["One", "Two", "Three"]
        a = tp.describe(album("a", "X", [track(n, -6.0, 0.5) for n in names]))
        b = tp.describe(album("b", "X", [track(n, g, 1.0) for n, g in zip(names, [-8.0, -8.5, -7.6])]))
        ranked, rest, shared = tp.analyse([a, b])
        self.assertEqual([(v["id"], v["metric"]) for v in ranked], [("b", "plr"), ("a", "plr")])
        self.assertAlmostEqual(ranked[0]["plr_mean"], 18 - 8.0333, places=3)
        self.assertAlmostEqual(ranked[1]["plr_mean"], 12 - 6.0206, places=3)
        self.assertAlmostEqual(ranked[1]["delta"], -3.99, places=2)

    def test_falls_back_to_loudness_without_peaks(self):
        names = ["One", "Two", "Three"]
        a = tp.describe(album("a", "X", [track(n, -6.0, None) for n in names]))
        b = tp.describe(album("b", "X", [track(n, g, 1.0) for n, g in zip(names, [-8.0, -8.6, -7.5])]))
        ranked, rest, shared = tp.analyse([a, b])
        self.assertEqual([(v["id"], v["metric"]) for v in ranked], [("a", "gain"), ("b", "gain")])

    def test_same_master_prefers_resolution(self):
        # real case Get Your Wings: the CD twin is 0.20 dB louder with slightly higher peaks,
        # so its dynamics come out a touch larger
        names = [f"Song {i}" for i in range(8)]
        hi = [-7.04, -8.30, -7.94, -7.85, -5.97, -7.49, -7.00, -7.60]
        a = tp.describe(album("hi", "X", [track(n, g, 0.96) for n, g in zip(names, hi)], 24, 96))
        b = tp.describe(album("cd", "X", [track(n, g - 0.2, 1.0) for n, g in zip(names, hi)]))
        ranked, rest, shared = tp.analyse([b, a])
        self.assertGreater(b["plr_mean"], a["plr_mean"])
        self.assertEqual([(v["id"], v["rank"], v["same_as"]) for v in ranked], [("hi", 1, None), ("cd", 2, 1)])
        self.assertAlmostEqual(ranked[1]["offset"], -0.2, places=2)

    def test_all_same_master(self):
        a = tp.describe(album("a", "X", [track("One", -5.0), track("Two", -6.0)], 24, 192))
        b = tp.describe(album("b", "X", [track("One", -5.0), track("Two", -6.0)]))
        ranked, rest, shared = tp.analyse([b, a])
        self.assertEqual(ranked[0]["id"], "a")                                  # higher resolution first
        self.assertEqual(ranked[1]["same_as"], 1)

    def test_master_years(self):
        a = tp.describe(album("a", "Paranoid", [track("One", -5.0)], version="Remastered 2012"))
        self.assertEqual(a["master_years"], [2012])


if __name__ == "__main__":
    unittest.main()
