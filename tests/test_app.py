"""Tests for search, edition logic, client and local server (no network needed).

    python -m unittest discover -s tests -v
"""
import json
import time
import unittest
import urllib.error
import urllib.request

from dynamicswar import core, demo, discover, editions, server
from dynamicswar.client import Client


def track(title, gain, peak=0.95, version=None, duration=240, isrc=None):
    return {"title": title, "version": version, "duration": duration, "isrc": isrc,
            "audio_info": {"replaygain_track_gain": gain, "replaygain_track_peak": peak}}


def album(album_id, title, tracks, bits=16, rate=44.1, version=None, release_type="album"):
    return {"id": album_id, "title": title, "version": version, "artist": {"name": "Band", "id": 1},
            "maximum_bit_depth": bits, "maximum_sampling_rate": rate, "release_type": release_type,
            "release_date_original": "1980-01-01", "label": {"name": "L"},
            "tracks_count": len(tracks), "tracks": {"items": tracks}}


SONGS = [f"Song {c}" for c in "ABCDEFGH"]
BONUS = [f"Bonus {c}" for c in "KLMNOP"]


def songs(gain, wobble=0.0, version=None, names=SONGS):
    return [track(s, gain + wobble * (i % 3), version=version) for i, s in enumerate(names)]


class TitleTests(unittest.TestCase):
    def test_album_key_strips_edition_words(self):
        key = discover.album_key
        self.assertEqual(key("Rumours (Deluxe Edition)"), "rumours")
        self.assertEqual(key("Rumours 2004 Remaster"), "rumours")
        self.assertEqual(key("Rumours Super Deluxe Edition"), "rumours")
        self.assertEqual(key("Thriller 40th Anniversary"), "thriller")
        self.assertEqual(key("Chicago 17"), "chicago 17")       # a bare number belongs to the name
        self.assertEqual(key("1984"), "1984")
        self.assertEqual(key("Deluxe"), "deluxe")               # never strip the whole title

    def test_title_matches(self):
        m = discover.title_matches
        self.assertTrue(m("Thriller", None, "thriller"))
        self.assertTrue(m("Thriller 25", None, "thriller"))
        self.assertTrue(m("Thriller", "25th Anniversary Edition", "thriller"))
        self.assertFalse(m("Thriller Live", None, "thriller"))
        self.assertFalse(m("Bad", None, "thriller"))

    def test_display_title_keeps_real_brackets(self):
        self.assertEqual(discover.display_title("Rumours (Deluxe Edition)"), "Rumours")
        self.assertEqual(discover.display_title("(What's the Story) Morning Glory?"),
                         "(What's the Story) Morning Glory?")
        self.assertEqual(discover.display_title("Brothers In Arms - Remastered 1996"), "Brothers In Arms")

    def test_edition_text_and_kind(self):
        self.assertEqual(editions.text_kind(discover.edition_text("X (Live at Leeds)")), "live")
        self.assertEqual(editions.text_kind(discover.edition_text("X", "2019 Stereo Mix")), "mix")
        self.assertEqual(editions.text_kind(discover.edition_text("X (Mono)")), "mono")
        self.assertIsNone(editions.text_kind(discover.edition_text("X (2011 Stereo Remaster)")))
        self.assertIsNone(editions.text_kind(discover.edition_text("X Super Deluxe Edition")))


class GroupingTests(unittest.TestCase):
    def test_editions_of_one_album_become_one_entry(self):
        items = [dict(album("1", "Rumours", []), release_date_original="1977-02-04"),
                 dict(album("2", "Rumours (Deluxe Edition)", [], 24, 96), release_date_original="2013-01-29"),
                 album("3", "Rumours", [], version="2004 Remaster"),
                 album("4", "Tusk", [])]
        groups = discover.group_albums(items)
        self.assertEqual([(g["title"], g["count"]) for g in groups], [("Rumours", 3), ("Tusk", 1)])
        self.assertEqual(groups[0]["ids"], ["1", "2", "3"])
        self.assertEqual((groups[0]["year"], groups[0]["format"]), (1977, "24/96"))


class NumberedEditionTests(unittest.TestCase):
    def item(self, album_id, title, year, artist="Band"):
        return dict(album(album_id, title, []), release_date_original=f"{year}-01-01",
                    artist={"name": artist, "id": 1})

    def test_anniversary_editions_join_the_album(self):
        # titles and years as Qobuz lists them for Thriller
        groups = discover.group_albums([
            self.item("1", "Thriller", 1982), self.item("2", "Thriller 25 (Super Deluxe Edition)", 2008),
            self.item("3", "Thriller 25 Super Deluxe Edition", 2008), self.item("4", "Thriller 40", 2022),
            self.item("5", "Thriller", 2020, artist="Cover Band")])
        self.assertEqual([(g["title"], g["artist"], g["ids"]) for g in groups],
                         [("Thriller", "Band", ["1", "3", "2", "4"]), ("Thriller", "Cover Band", ["5"])])

    def test_numbered_albums_stay_separate(self):
        groups = discover.group_albums([self.item("1", "Chicago", 1970), self.item("2", "Chicago 17", 1984),
                                        self.item("3", "Chicago 16", 1982)])
        self.assertEqual([g["title"] for g in groups], ["Chicago", "Chicago 17", "Chicago 16"])

    def test_same_year_counts_as_the_same_album(self):
        groups = discover.group_albums([self.item("1", "Album", 1990), self.item("2", "Album 30", 1990)])
        self.assertEqual([g["count"] for g in groups], [2])


class EditionTests(unittest.TestCase):
    def setUp(self):
        self.original = album("o", "Album", songs(-4.0, 0.3))
        self.remaster = album("r", "Album", songs(-8.0, 0.5, "2010 Remaster"), 24, 96, "2010 Remaster")
        self.deluxes = [album(f"d{i}", "Album", songs(-8.0 - i, 0.2 * i, "Remaster")
                              + songs(-7.0, 0.0, None, BONUS), 24, 96, "Deluxe Edition")
                        for i in range(1, 5)]

    def result(self, *albums):
        return editions.compare(list(albums))

    def test_original_stays_when_deluxe_editions_dominate(self):
        r = self.result(self.original, *self.deluxes)
        self.assertEqual(r["album"]["tracks"], 8)
        ranked = r["groups"][0]["ranked"]
        self.assertEqual(len(ranked), 5)
        self.assertEqual(ranked[0]["id"], "o")
        self.assertEqual(r["excluded"], [])

    def test_one_ranking_on_album_tracks_with_reasons(self):
        r = self.result(self.original, self.remaster, *self.deluxes[:2])
        self.assertEqual([g["key"] for g in r["groups"]], ["editions"])
        g = r["groups"][0]
        self.assertEqual([v["id"] for v in g["ranked"]], ["o", "r", "d1", "d2"])
        self.assertEqual(g["best"], "o")
        self.assertIn("widest dynamics", g["verdict"])
        self.assertIn("Most dynamic master", g["ranked"][0]["reasons"][0])
        self.assertIn("less dynamic than #1", g["ranked"][1]["reasons"][0])
        self.assertIn("louder", g["ranked"][1]["reasons"][0])
        self.assertEqual([(v["group"], v["extra"]) for v in g["ranked"]],
                         [("standard", 0), ("standard", 0), ("expanded", 6), ("expanded", 6)])
        self.assertEqual([v["compared"] for v in g["ranked"]], [8, 8, 8, 8])    # bonus tracks ignored
        self.assertEqual([t["title"] for t in g["tracks"]], SONGS)              # album order

    def test_album_without_loudness_data_like_thriller(self):
        # live finding: the plain album has no ReplayGain on Qobuz, the anniversary editions do
        plain = album("p", "Album", [track(s, 0, 0) for s in SONGS], 24, 176.4)
        a25 = album("a", "Album 25 (Super Deluxe Edition)", songs(-8.0, 0.3) + songs(-7.0, names=BONUS), 24, 44.1)
        b25 = album("b", "Album 25 Super Deluxe Edition", songs(-8.2, 0.5) + songs(-7.0, names=BONUS[:5]))
        a40 = album("c", "Album 40", songs(-8.6, 0.1) + songs(-7.0, names=BONUS), 24, 44.1)
        r = self.result(plain, a25, b25, a40)
        self.assertEqual(r["album"]["tracks"], 8)                               # the plain album defines it
        self.assertEqual(sorted(v["id"] for v in r["groups"][0]["ranked"]), ["a", "b", "c"])
        self.assertEqual([v["compared"] for v in r["groups"][0]["ranked"]], [8, 8, 8])
        self.assertEqual([v["id"] for v in r["excluded"]], ["p"])
        self.assertIn("no loudness data", r["excluded"][0]["excluded_reason"])
        self.assertIn("1 edition(s) of the album cannot be ranked", r["notes"][0])

    def test_same_master_prefers_resolution_and_says_so(self):
        cd = album("cd", "Album", songs(-8.0, 0.5, "2010 Remaster"), version="2010 Remaster")
        r = self.result(self.original, self.remaster, cd)
        ranked = r["groups"][0]["ranked"]
        self.assertEqual([v["id"] for v in ranked], ["o", "r", "cd"])
        self.assertEqual(ranked[2]["same_as"], 2)
        self.assertIn("Same master as #2", ranked[2]["reasons"][0])
        self.assertIn("lower resolution", ranked[2]["reasons"][0])

    def test_other_versions_are_shown_but_not_ranked(self):
        live = album("l", "Album (Live in Paris)", songs(-6.0))
        mono = album("m", "Album", songs(-5.0), version="Mono")
        mix = album("x", "Album", songs(-7.0), version="2019 Mix")
        live_takes = album("t", "Album", songs(-6.0, version="Live"))        # only the tracks say so
        single = album("s", "Album", songs(-9.0)[:2], release_type="single")
        other = album("z", "Album 2", songs(-9.0, names=[f"Other {i}" for i in range(8)]))
        r = self.result(self.original, self.remaster, live, mono, mix, live_takes, single, other)
        reasons = {v["id"]: v["excluded_reason"] for v in r["excluded"]}
        self.assertEqual(reasons["l"], "Live recording")
        self.assertEqual(reasons["t"], "Live recording")
        self.assertEqual(reasons["m"], "Mono version")
        self.assertTrue(reasons["x"].startswith("Different mix"))
        self.assertTrue(reasons["s"].startswith("Single or EP"))
        self.assertTrue(reasons["z"].startswith("Not this album (0 of its 8 tracks"))
        self.assertEqual([v["id"] for v in r["groups"][0]["ranked"]], ["o", "r"])
        self.assertTrue(all(v["dynamics"] is not None for v in r["excluded"]))

    # ---- cases found in live Qobuz data (25.11.2026)

    def test_more_tracks_win_when_master_and_resolution_are_equal(self):
        # Linkin Park "From Zero": standard and deluxe are identical on the album tracks
        std = album("s", "Album", songs(-9.0, 0.3), 24, 48)
        dlx = album("d", "Album (Deluxe Edition)", songs(-9.0, 0.3) + songs(-8.0, names=BONUS[:3]), 24, 48)
        cd = album("c", "Album (Deluxe Edition)", songs(-9.0, 0.3) + songs(-8.0, names=BONUS[:3]))
        g = self.result(std, dlx, cd)["groups"][0]
        self.assertEqual([v["id"] for v in g["ranked"]], ["d", "s", "c"])       # resolution still comes first
        self.assertIn("In the same resolution, Album (Deluxe Edition) [24/48] has the most tracks", g["verdict"])
        self.assertIn("but 3 more track(s)", " ".join(g["ranked"][0]["reasons"]))
        self.assertIn("with 3 fewer track(s)", g["ranked"][1]["reasons"][0])

    def test_live_bootleg_without_live_in_its_name_is_not_an_edition(self):
        # AC/DC "Greatest Hits Broadcast Collection": same song titles, other recordings
        lengths = [200, 185, 320, 260, 410, 150, 290, 500]
        boot = album("b", "Greatest Hits Broadcast Collection",
                     [track(s, -3.0, duration=240 + d) for s, d in zip(SONGS, lengths)] + songs(-3.0, names=BONUS))
        r = editions.compare([self.original, self.remaster, boot], "Album")
        self.assertEqual([v["id"] for v in r["groups"][0]["ranked"]], ["o", "r"])
        self.assertIn("Different recordings", r["excluded"][0]["excluded_reason"])

    def test_remaster_with_other_lengths_is_not_admitted_by_its_name(self):
        # "Aerosmith (2024 Remastered)" on Tidal: three of eight tracks 4-6 s longer. A wider
        # tolerance for releases that call themselves a remaster was tried and taken out: this
        # release turned out to be put together from other releases.
        longer = [0, 0, 0, 4, 6, -1, 0, 5]
        new = album("n", "Album (2024 Remastered)",
                    [track(s, -6.0, version="2024 Remaster", duration=240 + d) for s, d in zip(SONGS, longer)], 24, 96)
        r = editions.compare([self.original, self.remaster, new], "Album")
        self.assertEqual([v["id"] for v in r["groups"][0]["ranked"]], ["o", "r"])
        self.assertIn("Different recordings", r["excluded"][0]["excluded_reason"])

    def test_release_put_together_from_several_releases_is_shown_but_not_ranked(self):
        # Tidal plays some catalogue entries track by track from other releases; the service
        # marks every track with the release its audio comes from
        def marked(a, sources):
            for t, source in zip(a["tracks"]["items"], sources):
                t["source"] = source
            return a
        own = marked(album("o", "Album", songs(-8.0, 0.3)), [-9.0] * 8)
        mix = marked(album("m", "Album", [track(s, -3.0 - (i % 2) * 6) for i, s in enumerate(SONGS)], 24, 96),
                     [-5.8, -9.0, -5.8, -9.0, -12.9, -9.0, -5.8, -9.0])
        r = editions.compare([own, mix], "Album", "Tidal")
        group = r["groups"][0]
        self.assertEqual([v["id"] for v in group["ranked"] + group["unranked"]], ["o"])
        out = r["excluded"][0]
        self.assertEqual(out["id"], "m")
        self.assertIn("Put together by Tidal from tracks of 3 different releases", out["excluded_reason"])
        self.assertIsNotNone(out["dynamics"])               # its values are still shown
        self.assertIn("not ranked", r["notes"][0])
        # one source for all tracks: an ordinary release
        self.assertEqual(len(editions.compare([own, marked(album("p", "Album", songs(-5.0, 0.3), 24, 96), [-6.0] * 8)],
                                              "Album")["groups"][0]["ranked"]), 2)

    def test_box_set_counts_but_stands_behind_the_album(self):
        # "Bonfire" contains Back In Black, "Studio Collection" contains Meteora
        box = album("x", "Studio Collection", songs(-8.0, 0.5, "2010 Remaster")
                    + songs(-7.0, names=[f"Other {i}" for i in range(30)]), 24, 96)
        best_of = album("h", "Greatest Hits", songs(-8.0, 0.5)[:4] + songs(-7.0, names=BONUS))
        r = editions.compare([self.original, self.remaster, box, best_of], "Album")
        ranked = r["groups"][0]["ranked"]
        self.assertEqual([v["id"] for v in ranked], ["o", "r", "x"])
        self.assertTrue(ranked[2]["foreign"])
        self.assertIn("as part of another release", ranked[2]["reasons"][0])
        self.assertIn("Not this album (4 of its 8 tracks", r["excluded"][0]["excluded_reason"])

    def test_album_name_decides_the_track_list_not_a_smaller_live_album(self):
        # Dire Straits: "Live At The BBC" has fewer tracks than the album and no brackets in its title
        live = album("l", "Live At The BBC", [track(s, -6.0, duration=300) for s in SONGS[:6]])
        r = editions.compare([self.original, live], "Album")
        self.assertEqual(r["album"]["tracks"], 8)
        self.assertEqual(r["excluded"][0]["excluded_reason"], "Live recording")

    def test_track_names_that_differ_between_remasters(self):
        # Black Sabbath "Paranoid": 'War Pigs' (2009) is 'War Pigs / Luke's Wall' (2012)
        names = ["War Pigs / Luke's Wall", "Jack the Stripper / Fairies Wear Boots"] + SONGS[:6]
        other = ["War Pigs", "Fairies Wear Boots"] + SONGS[:6]
        a = album("a", "Album", [track(s, -4.0 - 0.1 * i, duration=200 + 30 * i) for i, s in enumerate(names)])
        b = album("b", "Album", [track(s, -6.0 - 0.3 * i, duration=201 + 30 * i) for i, s in enumerate(other)])
        g = editions.compare([a, b], "Album")["groups"][0]
        self.assertEqual([v["compared"] for v in g["ranked"]], [8, 8])

    def test_format_in_the_title_is_not_part_of_the_name(self):
        # Guns N' Roses: "Appetite For Destruction (Super Deluxe) - 192 kHz"
        self.assertEqual(discover.album_key("Appetite For Destruction - 192 kHz"), "appetite for destruction")
        self.assertEqual(discover.display_title("Appetite For Destruction - 192 kHz"), "Appetite For Destruction")
        self.assertEqual(discover.album_key("A Little Bit"), "a little bit")

    def test_old_box_set_with_better_masters_of_some_tracks(self):
        # Aerosmith "Pandora's Box": 4 of 8 album tracks as the same recording (same ISRC) in a far more
        # dynamic master, the rest as alternate takes - no edition of the album, but worth a hint
        current = album("a", "Album", [track(s, -8.5, isrc=f"US{i}") for i, s in enumerate(SONGS)], 24, 96)
        cd = album("c", "Album", [track(s, -8.7, isrc=f"US{i}") for i, s in enumerate(SONGS)])
        box = album("p", "Pandora's Box",
                    [track(s, -4.5, isrc=f"US{i}") for i, s in enumerate(SONGS[:4])]
                    + [track(s, -3.0, version="Alternate Version", duration=240) for s in SONGS[4:7]]
                    + songs(-4.0, names=BONUS))
        same_level = album("g", "Greatest Hits", [track(s, -8.5, isrc=f"US{i}") for i, s in enumerate(SONGS[:3])]
                           + songs(-8.0, names=BONUS))
        r = editions.compare([current, cd, box, same_level], "Album")
        self.assertEqual([v["id"] for v in r["groups"][0]["ranked"]], ["a", "c"])
        self.assertEqual([x["id"] for x in r["partials"]], ["p"])              # equally loud best-of: no hint
        hint = r["partials"][0]
        self.assertEqual((hint["count"], hint["album_size"], hint["gain"]), (4, 8, 4.0))
        self.assertEqual([t["title"] for t in hint["tracks"]], SONGS[:4])
        self.assertIn("4 of the album's 8 tracks as the same recording, on average 4.0 dB more dynamic", hint["summary"])
        self.assertEqual([v["id"] for v in r["excluded"]], ["g"])              # the box is not listed twice

    def test_take_marked_as_alternate_is_another_recording_even_at_equal_length(self):
        a = {"isrc": None, "variant": False, "duration": 193}
        self.assertFalse(editions.same_recording(a, {"isrc": None, "variant": True, "duration": 193}))
        self.assertTrue(editions.same_recording(a, {"isrc": None, "variant": False, "duration": 195}))
        self.assertTrue(editions.same_recording(dict(a, isrc="X1"), {"isrc": "X1", "variant": True, "duration": 400}))

    def test_six_step_meter(self):
        level = lambda plr: editions.assess({"plr_mean": plr, "gain_mean": -8.0})
        self.assertEqual([level(x)[1] for x in (5.0, 6.0, 6.9, 7.0, 8.4, 8.5, 9.9, 10.0, 12.9, 13.0, 16.0)],
                         [1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6])
        self.assertEqual([level(x)[0] for x in (5.0, 7.0, 10.0, 13.0)],
                         ["Heavily compressed", "Moderately compressed", "Dynamic", "Very dynamic"])
        by_loudness = lambda gain: editions.assess({"plr_mean": None, "gain_mean": gain})
        self.assertEqual([by_loudness(g)[1] for g in (-12.0, -10.0, -8.5, -7.0, -5.5, -4.0)], [1, 2, 3, 4, 5, 6])
        self.assertIsNone(editions.assess({"plr_mean": None, "gain_mean": None}))

    def test_alternate_version_with_identical_lengths_is_not_an_edition(self):
        # Daft Punk "Random Access Memories (Drumless Edition)": same lengths, other ISRCs, more dynamic
        # because the drums are missing - it was ranked first
        plain = album("a", "Album", [track(s, -6.5, peak=1.0, isrc=f"US{i}") for i, s in enumerate(SONGS)], 24, 88.2)
        anniv = album("b", "Album", [track(s, -6.5, peak=1.0, isrc=f"US{i}") for i, s in enumerate(SONGS)]
                      + songs(-3.0, names=BONUS), 24, 44.1, version="10th Anniversary Edition")
        drumless = album("d", "Album", [track(f"{s} (Drumless Edition)", -5.3, peak=0.99, isrc=f"GB{i}")
                                        for i, s in enumerate(SONGS)], 24, 88.2, version="Drumless Edition")
        r = editions.compare([plain, anniv, drumless], "Album")
        self.assertEqual([v["id"] for v in r["groups"][0]["ranked"]], ["a", "b"])
        self.assertEqual([(v["id"], v["excluded_reason"]) for v in r["excluded"]],
                         [("d", "Different mix (remix, new stereo or surround mix)")])
        # the same without a known keyword: the mark on every track gives it away
        odd = album("e", "Album", [track(f"{s} (Kazoo Rendition)", -5.3) for s in SONGS], version="Kazoo Rendition")
        r = editions.compare([plain, anniv, odd], "Album")
        self.assertEqual(r["excluded"][0]["excluded_reason"],
                         "Different version of the album (every track is marked \u201cKazoo Rendition\u201d)")

    def test_marks_that_every_edition_shares_do_not_exclude(self):
        # remaster wording is neutral, and a mark the album itself carries (soundtrack) is no difference
        a = album("a", "Album", [track(f"{s} (From the Motion Picture)", -6.0) for s in SONGS])
        b = album("b", "Album", [track(f"{s} (From the Motion Picture)", -8.0 - 0.2 * i, version="2015 Remaster")
                                 for i, s in enumerate(SONGS)], 24, 96)
        r = editions.compare([a, b], "Album")
        self.assertEqual(len(r["groups"][0]["ranked"]), 2)
        self.assertEqual(r["excluded"], [])
        self.assertEqual(editions.special_words(["2012 remaster", "drumless edition", "feat someone"]),
                         ["drumless", "feat someone"])

    def test_tracks_that_repeat_the_album_title_or_share_its_isrcs(self):
        # Bad Omens "CONCRETE JUNGLE (Original Soundtrack)": the 16-bit edition appends
        # "(Original Soundtrack)" to every track, the 24-bit edition does not - same ISRCs
        hi = album("h", "Jungle (Original Soundtrack)", [track(s, -9.0, isrc=f"US{i}") for i, s in enumerate(SONGS)], 24, 48)
        cd = album("c", "Jungle (Original Soundtrack)",
                   [track(f"{s} (Original Soundtrack)", -9.2, isrc=f"US{i}") for i, s in enumerate(SONGS)])
        r = editions.compare([hi, cd], "Jungle")
        self.assertEqual([v["id"] for v in r["groups"][0]["ranked"]], ["h", "c"])
        self.assertEqual(r["excluded"], [])
        # same ISRCs alone are enough, whatever the tracks are called
        odd = album("o", "Jungle (Original Soundtrack)",
                    [track(f"{s} (Kazoo Rendition)", -9.2, isrc=f"US{i}") for i, s in enumerate(SONGS)])
        self.assertEqual(editions.compare([hi, odd], "Jungle")["excluded"], [])

    def test_same_loudness_but_other_peaks_is_not_the_same_master(self):
        # Rick James "Street Songs": Deluxe and Expanded Edition are equally loud on every track, but the
        # Expanded Edition's peaks are cut lower - it was called "same master" and ranked by track count
        deluxe = album("d", "Album (Deluxe Edition)",
                       [track(s, -4.5, peak=p) for s, p in zip(SONGS, [.97, .88, .86, .66, .88, .97, .88, .90])]
                       + songs(-6.0, names=BONUS))
        expanded = album("e", "Album (Expanded Edition)", [track(s, -4.5, peak=0.82) for s in SONGS])
        box = album("x", "The Complete Albums", [track(s, -4.5, peak=0.82) for s in SONGS]
                    + songs(-6.0, names=[f"Other {i}" for i in range(20)]))
        ranked = editions.compare([deluxe, expanded, box], "Album")["groups"][0]["ranked"]
        self.assertEqual([(v["id"], v["same_as"]) for v in ranked], [("d", None), ("e", None), ("x", 2)])
        self.assertIn("less dynamic than #1", ranked[1]["reasons"][0])
        self.assertIn("Same master as #2", ranked[2]["reasons"][0])

    def test_roon_value_stops_at_the_peak_for_quiet_masters(self):
        # Rockwell "Somebody's Watching Me": Qobuz gain -0.9 dB would be +3.1 dB in Roon, but the peak is at
        # -0.5 dBFS and Roon does not amplify into clipping - it showed +0.0 dB
        quiet = album("q", "Album", [track(s, -0.9, peak=0.94455) for s in SONGS])
        loud = album("l", "Album", [track(s, -6.9 - 0.2 * i, peak=0.988556) for i, s in enumerate(SONGS)],
                     version="Deluxe Edition")
        ranked = {v["id"]: v for v in editions.compare([quiet, loud], "Album")["groups"][0]["ranked"]}
        self.assertEqual(ranked["q"]["roon_track"], 0.5)        # not +3.1
        self.assertEqual(ranked["l"]["roon_track"], -3.6)       # negative values are unchanged: gain + 4
        # album mode (the album plays through): the loudest track sets the value - Roon showed -4.0 dB for
        # the Deluxe Edition and +0.0 dB for the original
        self.assertEqual(ranked["l"]["roon_album"], -4.3)       # loudest track: gain -8.3
        self.assertEqual(ranked["q"]["roon_album"], 0.5)        # +3.1 wanted, the peak allows +0.5

    def test_rerecording_is_ranked_and_flagged(self):
        again = album("n", "Album (Band's Version)", songs(-9.0, 0.4), version="Re-Recorded")
        r = self.result(self.original, again)
        ranked = r["groups"][0]["ranked"]
        self.assertEqual([v["id"] for v in ranked], ["o", "n"])
        self.assertTrue(ranked[1]["rerecorded"])
        self.assertIn("Re-recording", ranked[1]["reasons"][-1])

    def test_no_loudness_data(self):
        silent = album("q", "Album", [track(s, 0, 0) for s in SONGS])
        r = self.result(self.original, self.remaster, silent)
        self.assertEqual([(v["id"], v["excluded_reason"]) for v in r["excluded"]],
                         [("q", "Qobuz provides no loudness data for this edition")])


class DiscoverTests(unittest.TestCase):
    def find(self, fetch):
        client = Client({}, fetch=fetch, pause=0)
        group = [g for g in discover.search(client, "northern lights")
                 if g["artist"] == "The Paper Lanterns"][0]
        seen = []
        albums, sources, truncated = discover.discover(
            client, group["artist"], group["title"], group["artist_id"], group["ids"],
            progress=lambda *a: seen.append(a))
        return sorted(str(a["id"]) for a in albums), sources, seen

    def test_finds_box_set_under_another_name(self):
        ids, sources, seen = self.find(demo.DemoQobuz(delay=0))
        self.assertEqual(ids, [str(i) for i in range(1001, 1014)])
        self.assertEqual(sources["tracks"], 2)                  # 1012 and 1013: found through their songs
        self.assertNotIn("3001", ids)                           # tribute band stays out
        self.assertEqual({s[0] for s in seen}, {"search", "load", "tracks"})

    def test_discography_fills_gaps_of_the_search(self):
        fake = demo.DemoQobuz(delay=0)
        inner = fake.__call__

        def fetch(url, headers=None, data=None, timeout=30):
            raw = inner(url, headers, data, timeout)
            if "catalog/search" in url and "type=albums" in url:
                data_ = json.loads(raw)
                data_["albums"]["items"] = [a for a in data_["albums"]["items"] if a["id"] != "1004"]
                return json.dumps(data_).encode()
            return raw

        ids, sources, _ = self.find(fetch)
        self.assertIn("1004", ids)
        self.assertEqual(sources["discography"], 1)


class ClientTests(unittest.TestCase):
    def test_retries_and_caches(self):
        fake, calls = demo.DemoQobuz(delay=0), []

        def fetch(url, headers=None, data=None, timeout=30):
            calls.append(url)
            if "album/get" in url and sum("album/get" in c for c in calls) == 1:
                raise core.ApiError(503, "busy")
            return fake(url, headers, data, timeout)

        client = Client({}, fetch=fetch, pause=0)
        self.assertEqual(client.album("1001")["id"], "1001")
        n = len(calls)
        client.album("1001")
        self.assertEqual(len(calls), n)

    def test_long_track_lists_are_paged(self):
        items = [dict(track(f"T{i}", -5.0), id=i) for i in range(70)]
        fake = demo.DemoQobuz(delay=0)

        def fetch(url, headers=None, data=None, timeout=30):
            if "album/get" in url:
                off = int(dict(p.split("=") for p in url.split("?")[1].split("&")).get("offset", 0))
                return json.dumps({"id": "9", "title": "Long", "tracks_count": 70,
                                   "tracks": {"total": 70, "items": items[off:off + 50]}}).encode()
            return fake(url, headers, data, timeout)

        client = Client({}, fetch=fetch, pause=0)
        self.assertEqual(len(client.album("9")["tracks"]["items"]), 70)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.state = server.State(cfg={}, save=lambda cfg: None, fetch=demo.DemoQobuz(delay=0), demo=True)
        self.httpd, self.base = server.serve(self.state)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, path, body=None, session=True, host=None):
        headers = {"X-Session": self.state.session} if session else {}
        if host:
            headers["Host"] = host
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base.rstrip("/") + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if path.startswith("/api/") else raw.decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_page_carries_session_and_api_needs_it(self):
        status, page = self.call("/", session=False)
        self.assertEqual(status, 200)
        self.assertIn(self.state.session, page)
        self.assertEqual(self.call("/api/status", session=False)[0], 403)
        self.assertEqual(self.call("/api/status", host="evil.example:80")[0], 403)
        self.assertEqual(self.call("/api/status")[1]["demo"], True)
        status, icon = self.call("/favicon.svg", session=False)
        self.assertEqual(status, 200)
        self.assertIn("<svg", icon)

    def test_search_and_compare(self):
        groups = self.call("/api/search?q=northern%20lights")[1]["groups"]
        group = [g for g in groups if g["artist"] == "The Paper Lanterns"][0]
        self.assertEqual(group["count"], 11)
        status, started = self.call("/api/compare", {"artist": group["artist"], "title": group["title"],
                                                     "artist_id": group["artist_id"], "ids": group["ids"]})
        self.assertEqual(status, 200)
        for _ in range(200):
            job = self.call("/api/job/" + started["job"])[1]
            if job["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], "done", job.get("error"))
        result = job["result"]
        self.assertEqual([g["key"] for g in result["groups"]], ["editions"])
        self.assertEqual(len(result["groups"][0]["ranked"]), 8)
        self.assertEqual(result["groups"][0]["best"], "1001")
        self.assertEqual(len(result["excluded"]), 4)
        self.assertEqual([x["id"] for x in result["partials"]], ["1013"])

    def test_report_file_lists_what_was_offered(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            self.state.report_dir = tmp
            started = self.call("/api/compare", {"artist": "The Paper Lanterns", "title": "Northern Lights",
                                                 "artist_id": 1, "ids": ["1001"]})[1]
            for _ in range(200):
                job = self.call("/api/job/" + started["job"])[1]
                if job["state"] != "running":
                    break
                time.sleep(0.02)
            self.assertEqual(job["state"], "done", job.get("error"))
            self.assertGreater(job["result"]["requests"], 10)
            self.assertIn("load", job["result"]["timing"])
            report = json.loads((Path(tmp) / "The Paper Lanterns - Northern Lights.json").read_text("utf-8"))
            replay = editions.compare(report["albums"])                 # a report can be replayed
            self.assertEqual([v["id"] for v in replay["groups"][0]["ranked"]],
                             [v["id"] for v in job["result"]["groups"][0]["ranked"]])
            self.assertIn("album/get", report["requests"])
            offered = {o["id"]: o for o in report["offered"]}
            self.assertTrue(offered["1004"]["taken"])
            self.assertEqual(offered["1012"]["source"], "tracks")       # box set, found through its songs
            self.assertTrue(offered["1012"]["taken"])
            self.assertFalse(offered["1020"]["taken"])                  # another album of the artist
            self.assertNotIn("3001", {o["id"] for o in report["offered"] if o["taken"]})

    def test_expired_token_is_reported_as_login_needed(self):
        def fetch(url, headers=None, data=None, timeout=30):
            if "play.qobuz.com" in url:
                return demo.DemoQobuz(delay=0)(url, headers, data, timeout)
            raise core.ApiError(401, "User authentication is required")

        self.state.client.fetch = fetch
        status, body = self.call("/api/search?q=anything")
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"])

    def test_status_says_when_a_restart_is_needed(self):
        self.assertFalse(self.call("/api/status")[1]["stale"])
        self.state.started = 0                                  # as if the files were replaced after the start
        self.assertTrue(self.call("/api/status")[1]["stale"])

    def test_open_only_accepts_qobuz_links(self):
        self.assertEqual(self.call("/api/open", {"url": "https://example.com/"})[0], 400)

    def test_library_over_http(self):
        self.state.library_pause, self.state.library_rate = 0, 0
        status, lib = self.call("/api/library")
        self.assertEqual(status, 200)
        self.assertEqual(len(lib["albums"]), 6)
        self.assertEqual(self.call("/api/library/check", {"ids": [str(n) for n in range(30)]})[0], 400)
        status, started = self.call("/api/library/check", {"ids": ["1002", "2002"]})
        self.assertEqual(status, 200)
        for _ in range(400):
            job = self.call("/api/job/" + started["job"])[1]
            if job["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual((job["state"], job["kind"], job["done"]), ("done", "library", 2))
        self.assertEqual([r["check"]["kind"] for r in job["results"]], ["master", "resolution"])
        checked = {a["id"]: a["check"] for a in self.call("/api/library")[1]["albums"]}
        self.assertEqual(checked["1002"]["best"]["id"], "1001")
        self.assertIsNone(checked["1030"])


def throttling(after, endpoint="catalog/search", headers=None):
    """Demo catalogue that answers 429 once `after` requests to the endpoint went through."""
    fake, seen = demo.DemoQobuz(delay=0), []

    def fetch(url, headers_=None, data=None, timeout=30):
        if endpoint in url:
            seen.append(url)
            if len(seen) > after:
                raise core.ApiError(429, "Too Many Requests", headers)
        return fake(url, headers_, data, timeout)

    fetch.seen = seen
    return fetch


class RequestLogTests(unittest.TestCase):
    def test_throttled_requests_are_counted_and_retried(self):
        fake, state = demo.DemoQobuz(delay=0), {"n": 0}

        def fetch(url, headers=None, data=None, timeout=30):
            if "catalog/search" in url:
                state["n"] += 1
                if state["n"] == 1:
                    raise core.ApiError(429, "Too Many Requests", {"Retry-After": "0", "X-RateLimit-Limit": "60"})
            return fake(url, headers, data, timeout)

        client = Client({"app_id": "1"}, fetch=fetch, pause=0)
        mark = client.log.mark()
        self.assertTrue(client.find_albums("northern lights"))
        traffic = client.log.since(mark)
        self.assertEqual(traffic["throttled"], 1)
        self.assertEqual(traffic["throttled_endpoints"], {"catalog/search": 1})
        self.assertEqual(traffic["retries"], 1)
        self.assertEqual(traffic["status"]["429"], 1)
        self.assertGreaterEqual(traffic["status"]["200"], 1)
        self.assertEqual(traffic["events"][0]["endpoint"], "catalog/search")
        self.assertEqual(traffic["events"][0]["retry_after"], "0")
        self.assertEqual(traffic["rate_headers"]["x-ratelimit-limit"], "60")
        self.assertGreaterEqual(traffic["peak_per_second"], 2)
        self.assertEqual(client.log.since(client.log.mark())["requests"], 0)      # a mark starts from zero

    def test_strict_mode_sends_nothing_after_the_first_throttled_request(self):
        fetch = throttling(after=0)
        client = Client({"app_id": "1"}, fetch=fetch, pause=0)
        client.strict = True
        with self.assertRaises(core.ApiError):
            client.find_albums("northern lights")
        self.assertTrue(client.halted)
        self.assertEqual(len(fetch.seen), 1)                                       # no retry
        with self.assertRaises(core.ApiError):
            client.album("1001")                                                   # not even other endpoints
        self.assertEqual(client.log.since({**client.log.mark(), "total": 0, "at": 0})["requests"], 1)


    def test_rate_limit_spaces_the_requests(self):
        client = Client({"app_id": "1"}, fetch=demo.DemoQobuz(delay=0), pause=0)
        client.rate = 20
        mark, started = client.log.mark(), time.monotonic()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda i: client.call("album/get", album_id=i), ["1001", "1002", "1003", "1004"] * 5))
        self.assertGreaterEqual(time.monotonic() - started, 19 / 20 - 0.05)     # 20 requests, 20 per second
        traffic = client.log.since(mark)
        self.assertEqual(traffic["requests"], 20)
        self.assertLessEqual(traffic["peak_per_second"], 20)
        client.rate, mark = 4, client.log.mark()
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda i: client.call("album/get", album_id=i), ["1001", "1002", "1003", "1004"] * 2 + ["1001"]))
        self.assertEqual(client.log.since(mark)["peak_per_second"], 4)          # 9 requests, never 5 in a second
        client.rate = None
        started = time.monotonic()
        client.call("album/get", album_id="1001")
        self.assertLess(time.monotonic() - started, 0.5)


class TimelineTests(unittest.TestCase):
    QUERY = {"artist": "The Paper Lanterns", "title": "Northern Lights", "artist_id": 1, "ids": ["1001"]}

    def make(self, **kw):
        return server.State(cfg={}, save=lambda cfg: None, fetch=demo.DemoQobuz(delay=0), demo=kw.pop("demo", True), **kw)

    def test_every_comparison_lands_in_the_timeline(self):
        state = self.make()
        self.assertEqual(state.timeline()["albums"], [])
        state._compare(dict(self.QUERY), lambda *a: None, time.time())
        (entry,) = state.timeline()["albums"]
        self.assertEqual((entry["artist"], entry["album"], entry["year"]), ("The Paper Lanterns", "Northern Lights", 1978))
        self.assertEqual((entry["best"]["id"], entry["best"]["dynamics"], entry["editions"]), ("1001", 13.41, 8))
        self.assertLess(entry["lowest"], 8)
        self.assertEqual(entry["query"]["ids"], ["1001"])               # a click on the point reopens the comparison
        state._compare(dict(self.QUERY), lambda *a: None, time.time())
        self.assertEqual(len(state.timeline()["albums"]), 1)            # compared twice, shown once

    def test_same_best_edition_under_two_names_is_one_album(self):
        from dynamicswar import history
        store = history.Store()
        base = {"artist": "A", "album": "Thriller", "year": 1982, "cover": "", "lowest": 9.0, "editions": 3,
                "best": {"id": "7", "label": "Thriller 25", "format": "24/44.1", "bits": 24, "dynamics": 9.6}}
        store.add(dict(base, key="a|thriller 25", when="2026-11-24 10:00:00", album="Thriller 25"))
        store.add(dict(base, key="a|thriller", when="2026-11-25 10:00:00"))
        store.add(dict(base, key="a|thriller", when="2026-11-23 10:00:00", year=1900))     # older: not taken
        self.assertEqual([(e["album"], e["year"]) for e in store.albums()], [("Thriller", 1982)])

    def test_box_set_compared_with_itself_is_left_out(self):
        from dynamicswar import history
        ed = {"id": "1", "label": "The Ten Year War", "format": "24/96", "bits": 24, "year": "2017", "dynamics": 13.3,
              "total": 66, "assessment": {"label": "Very dynamic", "level": 6}}
        result = {"album": {"title": "The Ten Year War", "artist": "Black Sabbath"},
                  "groups": [{"best": "1", "ranked": [], "unranked": [ed]}]}
        self.assertIsNone(history.record(result))
        self.assertEqual(history.record(dict(result, groups=[{"best": "1", "ranked": [], "unranked": [dict(ed, total=9)]}]))["year"], 2017)
        self.assertIsNone(history.record({"groups": []}))

    def test_earlier_reports_are_read_in_and_kept(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            first = self.make(demo=False, report_dir=tmp, history_path=Path(tmp) / "h1.json", library_path=Path(tmp) / "l.json")
            first._compare(dict(self.QUERY), lambda *a: None, time.time())
            (Path(tmp) / "_library run x.json").write_text("{}", encoding="utf-8")         # not a comparison
            fresh = self.make(demo=False, report_dir=tmp, history_path=Path(tmp) / "h2.json", library_path=Path(tmp) / "l.json")
            self.assertEqual([e["best"]["id"] for e in fresh.timeline()["albums"]], ["1001"])   # from the report
            again = self.make(demo=False, history_path=Path(tmp) / "h2.json", library_path=Path(tmp) / "l.json")
            self.assertEqual(len(again.timeline()["albums"]), 1)                                # from its own file


class LibraryTests(unittest.TestCase):
    def make(self, fetch=None, **kw):
        state = server.State(cfg={}, save=lambda cfg: None, fetch=fetch or demo.DemoQobuz(delay=0),
                             demo=kw.pop("demo", True), **kw)
        state.library_pause, state.library_rate = 0, 0
        return state

    def run_check(self, state, ids):
        job = state.library_check({"ids": ids})["job"]
        for _ in range(600):
            if state.job(job)["state"] != "running":
                break
            time.sleep(0.02)
        return state.job(job)

    def test_favourites_are_listed_unchecked(self):
        lib = self.make().library()
        self.assertEqual([a["id"] for a in lib["albums"]], demo.FAVOURITES)
        self.assertEqual(lib["albums"][0]["label"], "Northern Lights (Remastered 2009)")
        self.assertEqual(lib["albums"][0]["title"], "Northern Lights")
        self.assertTrue(all(a["check"] is None for a in lib["albums"]))
        self.assertEqual((lib["limit"], lib["paused"], lib["run"]), (25, 0, None))
        state = self.make()
        self.assertEqual((state.status()["connected"], state.status()["throttled"]), (True, 0))

    def test_every_kind_of_result(self):
        state = self.make()
        job = self.run_check(state, demo.FAVOURITES)
        self.assertEqual(job["state"], "done", job.get("error"))
        got = {r["id"]: r["check"] for r in job["results"]}
        self.assertEqual(got["1002"]["kind"], "master")                 # the original is 4 dB more dynamic
        self.assertEqual(got["1002"]["best"]["id"], "1001")
        self.assertEqual(got["1002"]["gain"], 4.0)
        self.assertFalse(got["1002"]["same_master"])
        self.assertEqual(got["1007"]["kind"], "master")
        self.assertEqual(got["2002"]["kind"], "resolution")             # same master in 24 bit
        self.assertTrue(got["2002"]["same_master"])
        self.assertEqual(got["2002"]["best"]["format"], "24/44.1")
        self.assertEqual(got["2002"]["best"]["id"], "2001")              # the same edition in 24 bit ...
        self.assertEqual((got["2002"]["also"]["id"], got["2002"]["also"]["more_tracks"]), ("2003", 3))   # ... then more
        self.assertEqual(got["1020"]["kind"], "best")
        self.assertEqual(got["1020"]["editions"], 2)
        self.assertTrue(got["1030"]["only"])
        self.assertEqual((got["1008"]["kind"], got["1008"]["reason"]), ("other", "Live recording"))
        self.assertEqual(got["1002"]["query"]["title"], "Northern Lights")   # what Compare reopens
        checked = {a["id"]: a["check"] for a in state.library()["albums"]}
        self.assertEqual(checked["1002"]["kind"], "master")
        state.library_forget({"ids": ["1002"]})
        self.assertIsNone({a["id"]: a["check"] for a in state.library()["albums"]}["1002"])

    def test_upgrade_of_the_same_edition_comes_before_more_content(self):
        from dynamicswar import library

        def ed(rank, label, bits, rate, total, same_as=None, dyn=9.0):
            return {"id": str(rank), "label": label, "format": f"{bits}/{rate:g}", "bits": bits, "rate": rate,
                    "year": "1987", "dynamics": dyn, "rank": rank, "same_as": same_as, "total": total,
                    "assessment": {"label": "Moderately compressed", "level": 4}, "url": f"u{rank}"}

        ranked = [ed(1, "Appetite (Super Deluxe)", 24, 192, 73), ed(2, "Appetite (Deluxe Edition)", 24, 192, 30, 1),
                  ed(3, "Appetite", 24, 192, 12, 1), ed(4, "Appetite (Super Deluxe)", 24, 96, 73, 1),
                  ed(5, "Appetite (Deluxe Edition)", 24, 96, 30, 1), ed(6, "Appetite", 16, 44.1, 12, 1)]
        result = {"groups": [{"ranked": ranked, "unranked": []}], "excluded": [], "partials": []}
        got = library.outcome(result, "5")                      # the Deluxe Edition in 24/96 is in the library
        self.assertEqual(got["kind"], "resolution")
        self.assertEqual((got["best"]["label"], got["best"]["format"]), ("Appetite (Deluxe Edition)", "24/192"))
        self.assertEqual((got["also"]["label"], got["also"]["more_tracks"]), ("Appetite (Super Deluxe)", 43))
        self.assertEqual((got["also"]["same_master"], got["also"]["gain"]), (True, 0.0))    # said in the second line too
        self.assertTrue(got["same_master"])
        got = library.outcome(result, "4")                      # the Super Deluxe itself: its upgrade is #1
        self.assertEqual(got["best"]["id"], "1")
        self.assertNotIn("also", got)
        ranked[1]["label"] = "Appetite (Deluxe)"                # no edition of the same name: first of the ranking
        got = library.outcome(result, "5")
        self.assertEqual(got["best"]["id"], "1")
        self.assertNotIn("also", got)

    def test_box_set_is_not_called_the_best_edition(self):
        from dynamicswar import library
        only = {"kind": "best", "only": True, "editions": 1, "mine": {"label": "The Ten Year War", "tracks": 66}}
        got = library.adjust({"tracks": 66, "media": 1}, only)
        self.assertEqual((got["kind"], got["box"]), ("other", True))
        self.assertIn("66 tracks", got["reason"])
        typed = dict(only, mine={"label": "Pandora's Box", "tracks": 12, "release_type": "compilation"})
        self.assertIn("Box set or compilation", library.adjust({"tracks": 12, "media": 1}, typed)["reason"])
        self.assertEqual(library.adjust({"tracks": 9, "media": 1}, dict(only, mine={"tracks": 9}))["kind"], "best")
        ranked = {"kind": "best", "only": False, "editions": 3, "mine": {"tracks": 70}}      # a real ranking stays
        self.assertEqual(library.adjust({"tracks": 70, "media": 4}, ranked)["kind"], "best")
        stored = {"kind": "best", "only": True, "mine": {"label": "Old result"}}             # stored before 0.2.1
        self.assertEqual(library.adjust({"tracks": 53, "media": 3}, stored)["kind"], "other")
        self.assertIsNone(library.adjust({"tracks": 53}, None))

    def test_run_sets_and_clears_the_rate_limit(self):
        state = self.make()
        state.library_rate = 500
        state.library()                                         # reading the favourites is not part of a run
        seen = []
        inner = state.client.fetch
        state.client.fetch = lambda url, *a, **kw: (seen.append(state.client.rate), inner(url, *a, **kw))[1]
        self.assertEqual(self.run_check(state, ["1030"])["state"], "done")
        self.assertEqual(set(seen), {500})
        self.assertIsNone(state.client.rate)

    def test_limit_and_unknown_albums(self):
        state = self.make()
        with self.assertRaises(ValueError):
            state.library_check({"ids": []})
        with self.assertRaises(ValueError):
            state.library_check({"ids": [str(n) for n in range(26)]})
        with self.assertRaises(ValueError):
            state.library_check({"ids": ["4711"]})                      # not a favourite

    def test_results_survive_a_restart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "library.json"
            state = self.make(demo=False, library_path=path)
            self.assertEqual(self.run_check(state, ["2002"])["state"], "done")
            again = self.make(demo=False, library_path=path)
            self.assertEqual({a["id"]: a["check"] for a in again.library()["albums"]}["2002"]["kind"], "resolution")

    def test_run_stops_at_the_first_throttled_request_and_pauses(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            fetch = throttling(after=12, headers={"Retry-After": "30"})
            state = self.make(fetch=fetch, report_dir=tmp)
            job = self.run_check(state, demo.FAVOURITES)
            self.assertEqual(job["state"], "stopped")
            self.assertIn("slowed", job["message"])
            sent = len(fetch.seen)
            self.assertEqual(sent, 13)                                  # nothing after the throttled request
            self.assertLess(len(job["results"]), len(demo.FAVOURITES))
            self.assertTrue(all(r["check"]["kind"] != "other" or not r["check"].get("failed") for r in job["results"]))
            self.assertGreater(state.library()["paused"], 800)
            self.assertGreater(state.status()["throttled"], 800)       # status light: amber
            with self.assertRaises(ValueError):
                state.library_check({"ids": ["1030"]})                  # paused
            self.assertFalse(state.client.strict or state.client.halted)    # single comparisons work again
            report = json.loads(next(Path(tmp).glob("_library run *.json")).read_text("utf-8"))
            self.assertEqual(report["state"], "stopped")
            self.assertEqual(report["traffic"]["throttled"], 1)
            self.assertEqual(report["traffic"]["events"][0]["retry_after"], "30")
            self.assertEqual(report["checked"], len(job["results"]))
            self.assertIn("item_keys", report["favourites"]["shape"])

    def test_checking_again_keeps_the_result_until_the_new_one_is_in(self):
        fetch = throttling(after=10 ** 6)
        state = self.make(fetch=fetch)
        self.assertEqual(self.run_check(state, ["2002"])["state"], "done")
        first = {a["id"]: a["check"] for a in state.library()["albums"]}["2002"]
        self.assertEqual(first["kind"], "resolution")
        state.client.fetch = state.client._logged(throttling(after=0))      # from now on Qobuz throttles at once
        again = self.run_check(state, ["2002"])
        self.assertEqual(again["state"], "stopped")
        kept = {a["id"]: a["check"] for a in state.library()["albums"]}["2002"]
        self.assertEqual((kept["kind"], kept["when"]), ("resolution", first["when"]))
        state.store.pause(0)
        state.client.fetch = state.client._logged(demo.DemoQobuz(delay=0))
        state.client.forget()
        self.assertEqual(self.run_check(state, ["2002"])["state"], "done")     # a clean run replaces it
        self.assertEqual({a["id"]: a["check"] for a in state.library()["albums"]}["2002"]["kind"], "resolution")

    def test_stop_button_ends_the_run_after_the_current_album(self):
        state = self.make(fetch=demo.DemoQobuz(delay=0.01))
        job = state.library_check({"ids": demo.FAVOURITES})["job"]
        state.library_stop({})
        for _ in range(600):
            if state.job(job)["state"] != "running":
                break
            time.sleep(0.02)
        done = state.job(job)
        self.assertEqual(done["state"], "cancelled")
        self.assertLess(len(done["results"]), len(demo.FAVOURITES))
        self.assertEqual(state.library()["paused"], 0)

    def test_single_comparison_reports_throttling(self):
        import tempfile
        from pathlib import Path
        fake, n = demo.DemoQobuz(delay=0), {"n": 0}

        def fetch(url, headers=None, data=None, timeout=30):
            if "album/get" in url:
                n["n"] += 1
                if n["n"] == 2:
                    raise core.ApiError(429, "Too Many Requests")
            return fake(url, headers, data, timeout)

        with tempfile.TemporaryDirectory() as tmp:
            state = self.make(fetch=fetch, report_dir=tmp)
            state.client.pause = 0
            result = state._compare({"artist": "The Paper Lanterns", "title": "Northern Lights", "artist_id": 1,
                                     "ids": ["1001"]}, lambda *a: None, time.time())
            self.assertEqual(result["traffic"]["throttled"], 1)
            self.assertEqual(result["traffic"]["retries"], 1)
            self.assertGreater(state.status()["throttled"], 800)       # also after a single comparison
            self.assertTrue(any("slowed down 1 request" in note for note in result["notes"]))
            self.assertEqual(result["groups"][0]["best"], "1001")       # the retry got the edition after all
            report = json.loads((Path(tmp) / "The Paper Lanterns - Northern Lights.json").read_text("utf-8"))
            self.assertEqual(report["traffic"]["throttled_endpoints"], {"album/get": 1})


if __name__ == "__main__":
    unittest.main()
