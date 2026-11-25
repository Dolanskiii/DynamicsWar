"""Built-in demo catalogue (python -m dynamicswar --demo).

A stand-in for the Qobuz API with invented artists, albums and values, so the interface
can be tried and tested without an account. Nothing here describes a real release.
"""
import json
import time
import urllib.parse

from . import core

APP_ID = "123456789"
ARTISTS = {1: "The Paper Lanterns", 2: "Mara Voss", 3: "Paper Lantern Tribute Band"}

SONGS = ["Harbour Lights", "Slow Train North", "Glass Morning", "Kites", "The Long Field",
         "Northern Lights", "Ash and Ember", "Copper Wire", "Last Ferry Home"]
# Per-track deviation of each (invented) mastering: different masters differ from track to
# track, the same master in another resolution does not.
WOBBLE = {
    "1978": [0.0, -0.4, 0.3, -0.2, 0.5, -0.6, 0.2, -0.1, 0.4],
    "2009": [0.3, 0.1, -0.5, 0.4, -0.2, 0.2, -0.6, 0.5, -0.1],
    "2012": [-0.2, 0.6, 0.1, -0.5, 0.3, 0.4, -0.3, -0.4, 0.2],
    "2016": [0.5, -0.1, -0.3, 0.2, -0.6, 0.1, 0.4, -0.2, -0.4],
    "2021": [-0.4, 0.2, 0.5, -0.1, 0.1, -0.3, -0.5, 0.3, 0.6],
}
# The demo account's favourite albums: a remaster with a better original, an expanded edition,
# a 16-bit edition of a hi-res master, an album that is already the best, the only edition
# of an album and a live album.
FAVOURITES = ["1002", "1007", "2002", "1020", "1030", "1008"]
MARA = ["Paper Boats", "Salt", "Low Winter Sun", "Telegraph", "Anchors", "Blue Hour", "Tide Tables"]


def _cover(text, hue):
    initials = "".join(w[0] for w in text.split()[:2]).upper()
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">'
           f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           f'<stop offset="0" stop-color="hsl({hue},45%,38%)"/>'
           f'<stop offset="1" stop-color="hsl({(hue + 50) % 360},50%,16%)"/></linearGradient></defs>'
           f'<rect width="300" height="300" fill="url(#g)"/>'
           f'<circle cx="150" cy="150" r="92" fill="none" stroke="rgba(255,255,255,.22)" stroke-width="2"/>'
           f'<text x="150" y="172" font-family="Segoe UI,Arial,sans-serif" font-size="64" font-weight="600" '
           f'fill="rgba(255,255,255,.85)" text-anchor="middle">{initials}</text></svg>')
    return "data:image/svg+xml," + urllib.parse.quote(svg)


def _track(n, title, gain, peak, version=None, duration=None):
    return {"id": 0, "track_number": n, "title": title, "version": version,
            "duration": duration or 200 + 17 * (len(title) % 9), "isrc": None,
            "audio_info": {"replaygain_track_gain": round(gain, 2), "replaygain_track_peak": peak}}


def _album(album_id, title, artist_id, tracks, bits=16, rate=44.1, version=None, year="1978-03-10",
           stream="2015-06-01", label="Lowlight Records", copyright_="1978 Lowlight Records",
           release_type="album", hue=210):
    for i, t in enumerate(tracks, 1):
        t["id"] = int(album_id) * 100 + i
        t["track_number"] = i
        t["isrc"] = t["isrc"] or f"XX{album_id}{i:02d}"
    image = _cover(title, hue)
    return {"id": album_id, "title": title, "version": version, "upc": f"0880{album_id}",
            "artist": {"id": artist_id, "name": ARTISTS[artist_id]},
            "image": {"small": image, "thumbnail": image, "large": image},
            "maximum_bit_depth": bits, "maximum_sampling_rate": rate,
            "release_date_original": year, "release_date_stream": stream,
            "label": {"name": label}, "copyright": "℗ " + copyright_, "release_type": release_type,
            "tracks_count": len(tracks), "tracks": {"total": len(tracks), "items": tracks}}


def _songs(gain, peak, version=None, master="1978"):
    return [_track(i, s, gain + w, peak, version) for i, (s, w) in enumerate(zip(SONGS, WOBBLE[master]), 1)]


def _bonus(gain, peak, n):
    extra = [("Harbour Lights", "Demo"), ("Kites", "Demo"), ("Copper Wire", "Early Take"),
             ("Night Mail", None), ("Paper Moon", None), ("Glass Morning", "Live at the Orpheum"),
             ("Kites", "Live at the Orpheum"), ("Last Ferry Home", "Live at the Orpheum"),
             ("Signal Fires", None), ("The Long Field", "Rehearsal"), ("Ash and Ember", "Single Edit"),
             ("Northern Lights", "Radio Session"), ("Lanterns", None), ("Slow Train North", "Demo")]
    return [_track(0, t, gain + 1.5, peak, v) for t, v in extra[:n]]


def _catalogue():
    nl, hue = "Northern Lights", 205
    albums = [
        _album("1001", nl, 1, _songs(-3.6, 0.891), hue=hue),
        _album("1002", nl, 1, _songs(-8.5, 0.989, "2009 Remaster", "2009"), version="Remastered 2009",
               stream="2009-10-02", copyright_="2009 Lowlight Records", hue=hue),
        _album("1003", nl, 1, _songs(-8.5, 0.989, "2009 Remaster", "2009"), 24, 96, version="2009 Remaster",
               stream="2014-02-21", copyright_="2009 Lowlight Records", hue=hue),
        _album("1004", nl, 1, _songs(-6.0, 0.966, "2021 Remaster", "2021"), 24, 192, version="2021 Remaster",
               stream="2021-09-17", copyright_="2021 Lowlight Records", hue=hue),
        _album("1005", nl, 1, _songs(-8.5, 0.989, "2009 Remaster", "2009") + _bonus(-8.5, 0.989, 8), 24, 96,
               version="Deluxe Edition", stream="2014-02-21", copyright_="2009 Lowlight Records", hue=hue),
        _album("1006", nl, 1, _songs(-6.0, 0.966, "2021 Remaster", "2021") + _bonus(-6.0, 0.966, 14), 24, 192,
               version="40th Anniversary Super Deluxe", stream="2021-09-17",
               copyright_="2021 Lowlight Records", hue=hue),
        _album("1007", nl, 1, _songs(-10.6, 1.0, "Remastered", "2012") + _bonus(-10.6, 1.0, 5),
               version="Expanded Edition", stream="2012-05-04", copyright_="2012 Lowlight Records", hue=hue),
        _album("1008", nl, 1, _songs(-7.2, 0.95, "Live at the Orpheum", "2012"), version="Live at the Orpheum",
               year="1980-11-01", copyright_="1980 Lowlight Records", hue=hue + 120),
        _album("1009", nl, 1, _songs(-4.4, 0.90, "Mono"), version="Mono", stream="2018-04-13", hue=hue),
        _album("1010", nl, 1, _songs(-7.9, 0.98, "2019 Stereo Mix", "2016"), 24, 96, version="2019 Stereo Mix",
               stream="2019-03-08", copyright_="2019 Lowlight Records", hue=hue),
        _album("1011", nl, 1, [_track(1, "Northern Lights", -7.0, 0.98, "Single Version"),
                               _track(2, "Night Mail", -6.5, 0.97)],
               release_type="single", year="1978-01-20", hue=hue + 30),
        _album("1012", "Lanterns: The Studio Albums", 1,
               _songs(-5.2, 0.94, "2016 Remaster", "2016") + _bonus(-5.2, 0.94, 12), 24, 48,
               year="2016-11-04", stream="2016-11-04", copyright_="2016 Lowlight Records", hue=30),
        _album("1013", "Tin Box: Rarities 1976-1982", 1,
               [dict(_track(i, t, -1.6 + w, 0.89), isrc=f"XX1001{i:02d}")
                for i, (t, w) in enumerate(zip(SONGS[:4], WOBBLE["1978"]), 1)]
               + [_track(0, t, -2.5, 0.9, "Alternate Take", duration=150) for t in SONGS[4:7]]
               + _bonus(-2.0, 0.9, 9), year="1992-10-05", stream="2016-03-11",
               copyright_="1992 Lowlight Records", hue=280),
        _album("1020", "Harbour Songs", 1, [_track(i, t, -5.5, 0.93) for i, t in enumerate(
            ["Quayside", "Nets", "Gull", "Breakwater", "Low Tide", "Pilot"], 1)],
               year="1976-05-14", copyright_="1976 Lowlight Records", hue=160),
        _album("1021", "Harbour Songs", 1, [_track(i, t, -9.2, 0.99, "2011 Remaster") for i, t in enumerate(
            ["Quayside", "Nets", "Gull", "Breakwater", "Low Tide", "Pilot"], 1)], 24, 96,
               version="2011 Remaster", year="1976-05-14", copyright_="2011 Lowlight Records", hue=160),
        _album("1030", "Glasshouse", 1, [_track(i, t, -7.4, 0.97) for i, t in enumerate(
            ["Glasshouse", "Fern", "Hothouse Days", "Condensation", "Seedlings"], 1)],
               year="1981-02-27", copyright_="1981 Lowlight Records", hue=95),
        _album("2001", "Tide Tables", 2, [_track(i, t, -9.8, 0.995) for i, t in enumerate(MARA, 1)],
               24, 44.1, year="2019-09-13", stream="2019-09-13", label="Saltwater",
               copyright_="2019 Saltwater", hue=330),
        _album("2002", "Tide Tables", 2, [_track(i, t, -9.8, 0.995) for i, t in enumerate(MARA, 1)],
               year="2019-09-13", stream="2019-09-13", label="Saltwater", copyright_="2019 Saltwater", hue=330),
        _album("2003", "Tide Tables", 2, [_track(i, t, -9.8, 0.995) for i, t in enumerate(MARA, 1)]
               + [_track(0, t, -9.5, 0.99) for t in ("Driftwood", "Sandbar", "Low Water")],
               24, 44.1, version="Deluxe", year="2019-09-13", stream="2020-03-06", label="Saltwater",
               copyright_="2019 Saltwater", hue=330),
        _album("3001", nl, 3, [_track(i, s, -11.0, 1.0) for i, s in enumerate(SONGS[:5], 1)],
               year="2005-01-01", label="Budget Covers", copyright_="2005 Budget Covers", hue=0),
    ]
    return {a["id"]: a for a in albums}


class DemoQobuz:
    """fetch() replacement answering like the Qobuz API, from the catalogue above."""

    def __init__(self, delay=0.12):
        self.albums, self.delay = _catalogue(), delay

    def _light(self, a):
        return {k: v for k, v in a.items() if k != "tracks"}

    def _block(self, items, params):
        off, lim = int(params.get("offset", 0)), int(params.get("limit", 50))
        return {"total": len(items), "items": items[off:off + lim]}

    def __call__(self, url, headers=None, data=None, timeout=30):
        time.sleep(self.delay)
        u = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(data.decode() if data else u.query))
        if u.netloc == "play.qobuz.com":
            if u.path == "/login":
                return b'<script src="/resources/demo/bundle.js"></script>'
            return f'production:{{api:{{appId:"{APP_ID}"'.encode()
        endpoint = u.path.split("/0.2/")[1]
        if endpoint == "user/get":
            return b'{"id": 1, "display_name": "Demo"}'
        if endpoint == "catalog/search":
            words = core.norm(params.get("query", "")).split()
            kind = params.get("type")
            if kind == "artists":
                items = [{"id": i, "name": n} for i, n in ARTISTS.items()
                         if all(w in core.norm(n) for w in words)]
                return json.dumps({"artists": self._block(items, params)}).encode()
            if kind == "tracks":
                isrc = params.get("query", "")
                items = [{"id": t["id"], "title": t["title"], "version": t["version"],
                          "performer": a["artist"], "album": self._light(a)}
                         for a in self.albums.values() for t in a["tracks"]["items"]
                         if t["isrc"] == isrc or all(
                             w in core.norm(f"{a['artist']['name']} {t['title']}") for w in words)]
                return json.dumps({"tracks": self._block(items, params)}).encode()
            items = [self._light(a) for a in self.albums.values()
                     if words and all(w in core.norm(f"{a['artist']['name']} {a['title']} {a['version'] or ''}")
                                      for w in words)]
            return json.dumps({"albums": self._block(items, params)}).encode()
        if endpoint in ("artist/get", "artist/getReleasesList"):
            mine = [self._light(a) for a in self.albums.values()
                    if str(a["artist"]["id"]) == str(params.get("artist_id"))]
            block = self._block(mine, params)
            if endpoint == "artist/get":
                return json.dumps({"albums": block}).encode()
            return json.dumps({"has_more": int(params.get("offset", 0)) + len(block["items"]) < len(mine),
                               "items": block["items"]}).encode()
        if endpoint == "favorite/getUserFavorites":
            items = [self._light(self.albums[i]) for i in FAVOURITES] if params.get("type") == "albums" else []
            return json.dumps({"albums": self._block(items, params)}).encode()
        if endpoint == "album/get":
            album = self.albums.get(str(params.get("album_id")))
            if not album:
                raise core.ApiError(404, "No result matching given argument")
            return json.dumps(album).encode()
        raise core.ApiError(404, "unknown endpoint")

