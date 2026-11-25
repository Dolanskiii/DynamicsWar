#!/usr/bin/env python3
"""Engine of DynamicsWar: access to Qobuz, title matching and the dynamics of each edition.

The services store loudness (ReplayGain) and peak level per track. From these the engine works
out the dynamics of a track (the distance between peak level and loudness): higher = more
dynamic. Where peak levels are missing, loudness is used instead.

Only metadata is read, nothing is downloaded. Standard library only. The interfaces are the ones
of the web players - they are not official APIs and can change.
"""
import json
import math
import re
import statistics
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

API = "https://www.qobuz.com/api.json/0.2/"
PLAY = "https://play.qobuz.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
MAX_VERSIONS = 30
# Qobuz refers ReplayGain to -18 LUFS, Roon levels to -14 LUFS: in the signal path (volume
# leveling, mode "Track") Roon shows the Qobuz value + 4 dB. Confirmed by three measurements.
ROON_OFFSET = 4.0
SAME_MASTER_PLR = 0.6   # dB: how far the dynamics of a track may differ within "the same master"

BUNDLE_RE = re.compile(r'<script[^>]+src="(/resources/[^"]+/bundle\.js)"')
APP_ID_RES = (
    re.compile(r'production:\{api:\{appId:"(\d{9})"'),
    re.compile(r'appId:"(\d{9})"'),
)


class ApiError(Exception):
    def __init__(self, status, message, headers=None):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message or ""
        self.headers = headers      # response headers, if any (Retry-After on 429)
        self.body = ""              # start of the error response (where a service names the reason)


# ---------------------------------------------------------------- HTTP / API

LAST = threading.local()     # headers of this thread's last response (for the request log of the app)


def http_fetch(url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    LAST.headers = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            LAST.headers = resp.headers
            return resp.read()
    except urllib.error.HTTPError as e:
        LAST.headers = e.headers
        body = e.read()
        try:
            msg = json.loads(body).get("message", "")
        except Exception:
            msg = body[:200].decode("utf-8", "replace")
        err = ApiError(e.code, msg or str(e.reason), e.headers)
        err.body = body[:400].decode("utf-8", "replace")
        raise err
    except urllib.error.URLError as e:
        raise ApiError(0, f"No connection: {e.reason}")


class Qobuz:
    def __init__(self, cfg, fetch=http_fetch, save=lambda cfg: None):
        self.cfg, self.fetch, self.save = cfg, fetch, save

    def app_id(self):
        if self.cfg.get("app_id"):
            return self.cfg["app_id"]
        html = self.fetch(PLAY + "/login").decode("utf-8", "replace")
        m = BUNDLE_RE.search(html)
        if not m:
            raise ApiError(0, "The Qobuz web player has changed (bundle.js not found). "
                              'Enter an app_id by hand in config.json: {"app_id": "..."}')
        bundle = self.fetch(PLAY + m.group(1), timeout=90).decode("utf-8", "replace")
        for rx in APP_ID_RES:
            m = rx.search(bundle)
            if m:
                self.cfg["app_id"] = m.group(1)
                self.save(self.cfg)
                return m.group(1)
        raise ApiError(0, "No app_id found in the Qobuz web player. "
                          'Enter it by hand in config.json: {"app_id": "..."}')

    def call(self, endpoint, post=False, **params):
        for attempt in (1, 2):
            app_id = self.app_id()
            headers = {"X-App-Id": app_id}
            if self.cfg.get("user_auth_token"):
                headers["X-User-Auth-Token"] = self.cfg["user_auth_token"]
            qs = urllib.parse.urlencode({"app_id": app_id, **params})
            try:
                if post:
                    raw = self.fetch(API + endpoint, headers, qs.encode())
                else:
                    raw = self.fetch(f"{API}{endpoint}?{qs}", headers)
                return json.loads(raw)
            except ApiError as e:
                # the cached app_id is no longer valid -> look it up once more
                if attempt == 1 and e.status == 400 and "app" in e.message.lower():
                    self.cfg.pop("app_id", None)
                    continue
                raise

    def set_token(self, token):
        """Stores the token from the web player and checks it against user/get."""
        token = token.strip().strip('"\'')
        if token.lower().startswith("x-user-auth-token"):
            token = token.split(":", 1)[-1].strip()
        if not token:
            raise ApiError(401, "No token entered.")
        self.cfg["user_auth_token"] = token
        try:
            self.call("user/get")
        except ApiError as e:
            self.cfg.pop("user_auth_token", None)
            if e.status == 401:
                raise ApiError(401, "Qobuz rejected the token (mistyped or expired).")
            raise
        self.save(self.cfg)

    def paged(self, endpoint, key, max_items, limit, **params):
        """Pages until a page comes back empty. Relies neither on 'total' nor on the server
        returning the page size that was asked for."""
        items, total, first = [], None, object()
        while len(items) < max_items:
            data = self.call(endpoint, limit=limit, offset=len(items), **params)
            block = (data.get(key) or {}) if key else data
            got = block.get("items") or []
            if block.get("total") is not None:
                total = block["total"]
            if not got or got[0].get("id") == first:
                break
            first = got[0].get("id")
            items += got
            if (total is not None and len(items) >= total) or block.get("has_more") is False:
                break
        return items

    def search_artist_ids(self, name):
        data = self.call("catalog/search", query=name, type="artists", limit=10)
        want = norm(name)
        return [a["id"] for a in ((data.get("artists") or {}).get("items") or [])
                if norm(a.get("name")) == want and a.get("id")]

    def album(self, album_id):
        return self.call("album/get", album_id=album_id)


# ---------------------------------------------------------- title matching

PAREN = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
DASH_SUFFIX = re.compile(r"\s[-–—]\s+([^-–—]+)$")
TAGWORD = re.compile(r"remaster|version|edit|mix|live|mono|stereo|demo|deluxe|edition|"
                     r"anniversary|bonus|acoustic|instrumental", re.I)
# takes that are not another mastering of the same recording
VARIANT = re.compile(r"\b(live|demo|mix|remix|remixed|acoustic|instrumental|edit|session|sessions|"
                     r"rehearsal|take|alternate|alternative|outtake|radio|single|mono|karaoke|"
                     r"unplugged|rough|early|bbc|a cappella|acapella)\b")


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold().replace("&", " and ")
    return re.sub(r"[\W_]+", " ", s).strip()


def split_title(title, version=None):
    """'Dream On (2024 Remaster)' -> ('dream on', ['2024 remaster'])"""
    title = title or ""
    tags = PAREN.findall(title)
    base = PAREN.sub(" ", title)
    m = DASH_SUFFIX.search(base)
    if m and TAGWORD.search(m.group(1)):
        tags.append(m.group(1))
        base = base[:m.start()]
    if version:
        tags.append(version)
    return norm(base) or norm(title), [t for t in map(norm, tags) if t]


def is_variant(tags):
    return any(VARIANT.search(t) for t in tags)


def artist_name(item):
    name = (item.get("artist") or {}).get("name")
    return name.get("display") if isinstance(name, dict) else name


def same_artist(name, want):
    """Same name, or the wanted name as a whole run of words in it ('X & Y' fits 'X').
    Parts of words do not count: 'Sabbath' is not 'Black Sabbath'."""
    a, w = norm(name), norm(want)
    a, w = (x[4:] if x.startswith("the ") else x for x in (a, w))
    return bool(a and w) and (a == w or f" {w} " in f" {a} ")


def pick_tracks(album_json):
    """For each base title the first regular take (otherwise the first variant)."""
    out = {}
    for t in ((album_json.get("tracks") or {}).get("items") or []):
        base, tags = split_title(t.get("title"), t.get("version"))
        ai = t.get("audio_info") or {}
        gain, peak = ai.get("replaygain_track_gain"), ai.get("replaygain_track_peak")
        if gain == 0 and not peak:          # 0/0 = no data stored
            gain = None
        # dynamics: peak level in dBFS minus loudness in LUFS (the Qobuz gain refers to -18 LUFS)
        plr = 20 * math.log10(peak) + 18 + gain if gain is not None and peak and peak > 0 else None
        entry = {"title": t.get("title") or "", "gain": gain, "peak": peak, "plr": plr,
                 "variant": is_variant(tags), "duration": t.get("duration"), "isrc": t.get("isrc"),
                 "id": t.get("id"),
                 # what the service says per track about the release the audio comes from
                 # (Tidal: album gain of the source); older reports only have the Tidal field
                 "source": t["source"] if "source" in t else (t.get("tidal") or {}).get("album_gain")}
        cur = out.get(base)
        if cur is None or (cur["variant"] and not entry["variant"]):
            out[base] = entry
    return out


def loudest_gain(album_json):
    """Gain of the loudest track over ALL tracks of the edition (bonus material included).
    When the service names this value itself (Tidal), that one counts - then not every track
    has to be read."""
    if album_json.get("loudest_gain") is not None:
        return album_json["loudest_gain"]
    gains = []
    for t in ((album_json.get("tracks") or {}).get("items") or []):
        ai = t.get("audio_info") or {}
        gain, peak = ai.get("replaygain_track_gain"), ai.get("replaygain_track_peak")
        if gain is not None and not (gain == 0 and not peak):
            gains.append(gain)
    return min(gains) if gains else None


def describe(album_json):
    label = album_json.get("title") or "?"
    ver = album_json.get("version")
    if ver and norm(ver) not in norm(label):
        label += f" ({ver})"
    bits, rate = album_json.get("maximum_bit_depth"), album_json.get("maximum_sampling_rate")
    return {
        "id": str(album_json.get("id", "")),
        "artist": artist_name(album_json) or "?",
        "title": album_json.get("title") or "",
        # years that point to the mastering: from the version suffix and the (P) line
        "master_years": sorted({int(y) for y in re.findall(
            r"\b(?:19|20)\d{2}\b", " ".join(PAREN.findall(album_json.get("title") or "")
                                             + [album_json.get("version") or ""]))}
            | {int(y) for y in re.findall(r"\u2117\s*((?:19|20)\d{2})", album_json.get("copyright") or "")}),
        "label": label,
        "bits": bits or 0,
        "rate": rate or 0,
        "format": f"{bits}/{rate:g}" if bits and rate else "?",
        "year": str(album_json.get("release_date_original") or "")[:4] or "?",
        "stream": str(album_json.get("release_date_stream") or "") or "?",
        "company": (album_json.get("label") or {}).get("name") or "?",
        "copyright": album_json.get("copyright") or "",
        "album_gain": loudest_gain(album_json),
        "ident": {k: album_json.get(k) for k in ("upc", "qobuz_id", "release_type", "displayable",
                                                 "streamable", "purchasable", "release_date_stream")},
        "total": album_json.get("tracks_count") or len(pick_tracks(album_json)),
        "tracks": pick_tracks(album_json),
    }


# ------------------------------------------------------------- evaluation

def analyse(versions, only_track=None, keep_all=False):
    """Rates each version relative to the median of the others - per track, then averaged.
    That keeps versions with different track lists (deluxe, expanded) comparable.

    When it is about an album (at least 4 tracks), releases with only 1-2 tracks or hardly any
    shared tracks are not rated: those are singles or other recordings (covers, wrongly assigned
    uploads) whose loudness would distort the comparison. keep_all switches that off."""
    want = split_title(only_track)[0] if only_track else None
    full = [{k: t for k, t in v["tracks"].items() if t["gain"] is not None} for v in versions]
    count_full = Counter(k for tr in full for k in tr)
    overlap = [sum(1 for k in tr if count_full[k] >= 2) for tr in full]
    biggest, most = max(map(len, full), default=0), max(overlap, default=0)
    per, compared = [], []
    for v, tr, shared_n in zip(versions, full, overlap):
        v["skipped"] = not keep_all and ((biggest >= 4 and len(tr) <= 2) or
                                         (most >= 4 and shared_n * 2 < most))
        if want:
            tr = {k: t for k, t in tr.items() if k == want} or {k: t for k, t in tr.items() if want in k}
        per.append(tr)
        compared.append({} if v["skipped"] else tr)
    count = Counter(k for tr in compared for k in tr)
    shared = {k for k, c in count.items() if c >= 2}
    # If possible rate only tracks that occur in ALL versions (no bonus material).
    linked = [set(tr) & shared for tr in compared if set(tr) & shared]
    core = set.intersection(*linked) if linked else set()
    if core and (len(core) >= 3 or len(core) == len(shared) or want):
        shared = core
    # Dynamics are rated; only when a peak level is missing somewhere, loudness instead.
    metric = "plr" if shared and all(tr[k]["plr"] is not None
                                     for tr in compared for k in shared & tr.keys()) else "gain"
    med = {k: statistics.median(tr[k][metric] for tr in compared if k in tr) for k in shared}
    med_dur = {}
    for k in shared:
        durs = [tr[k]["duration"] for tr in compared if k in tr and tr[k]["duration"]]
        if durs:
            med_dur[k] = statistics.median(durs)

    for v, tr in zip(versions, per):
        keys = [] if v["skipped"] else sorted(shared & tr.keys())
        comparable = bool(keys)
        if not comparable:      # nothing to compare with: rate its own tracks (regular takes first)
            keys = sorted(k for k, t in tr.items() if not t["variant"]) or sorted(tr)
        v["used"] = {k: tr[k] for k in keys}
        v["n"] = len(keys)
        v["metric"] = metric
        v["score"] = statistics.fmean(tr[k][metric] - med[k] for k in keys) if comparable else None
        v["gain_mean"] = statistics.fmean(tr[k]["gain"] for k in keys) if keys else None
        plrs = [tr[k]["plr"] for k in keys if tr[k]["plr"] is not None]
        v["plr_mean"] = statistics.fmean(plrs) if plrs else None
        peaks = [tr[k]["peak"] for k in keys if tr[k]["peak"] is not None]
        v["peak_max"] = max(peaks) if peaks else None
        # report only when other versions have the regular take in this place
        # ... and not when it is the same recording (same ISRC) under another name here
        # ("Beat It (Single Version)" on the original album)
        v["variants"] = sum(1 for k in keys if tr[k]["variant"]
                            and any(k in o and not o[k]["variant"] for o in compared)
                            and not any(k in o and not o[k]["variant"] and tr[k].get("isrc")
                                        and o[k].get("isrc") == tr[k]["isrc"] for o in compared))
        v["odd_length"] = sum(1 for k in keys if k in med_dur and tr[k]["duration"]
                              and abs(tr[k]["duration"] - med_dur[k]) > 15)
        v["same_as"], v["offset"] = None, 0.0

    ranked = sorted((v for v in versions if v["score"] is not None),
                    key=lambda v: (-round(v["score"], 1), -v["bits"], -v["rate"],
                                   v.get("foreign", False), -v.get("total", 0)))
    rest = [v for v in versions if v["score"] is None]
    for i, v in enumerate(ranked, 1):
        v["rank"] = i
        v["delta"] = v["score"] - ranked[0]["score"]
    # Same master: identical values, or shifted evenly over all tracks
    # (e.g. the CD edition of the same master at a slightly different level).
    for i, a in enumerate(ranked):
        for b in ranked[i + 1:]:
            keys = a["used"].keys() & b["used"].keys()
            if b["same_as"] is not None or len(keys) < 2:
                continue
            diffs = [b["used"][k]["gain"] - a["used"][k]["gain"] for k in keys]
            identical = max(abs(d) for d in diffs) <= 0.05
            shifted = len(keys) >= 3 and max(diffs) - min(diffs) <= 0.06
            # Same loudness but clearly different peak levels: limited differently, so not the same
            # master (real case "Street Songs": deluxe and expanded equally loud, dynamics up to
            # 1.7 dB apart). True pairs of the same master were at most 0.4 dB apart in 48 cases.
            pairs = [(a["used"][k]["plr"], b["used"][k]["plr"]) for k in keys]
            same_peaks = all(x is None or y is None or abs(x - y) <= SAME_MASTER_PLR for x, y in pairs)
            if (identical or shifted) and same_peaks:
                b["same_as"] = a["same_as"] or a["rank"]
                b["offset"] = a["offset"] + (0.0 if identical else statistics.fmean(diffs))
    # Within the same master the resolution decides, not tenths of a dB.
    # At the same resolution the edition with more tracks wins (more content); editions under
    # another name (box sets) stand behind those of the album itself.
    groups = {}
    for v in ranked:
        groups.setdefault(v["same_as"] or v["rank"], []).append(v)
    for members in groups.values():
        if len(members) > 1:
            slots = sorted(m["rank"] for m in members)
            order = sorted(members, key=lambda m: (-m["bits"], -m["rate"], m.get("foreign", False),
                                                   -m.get("total", 0), m["rank"]))
            base = order[0]["offset"]
            for slot, m in zip(slots, order):
                m["rank"], m["offset"] = slot, m["offset"] - base
            for m in order:
                m["same_as"] = None if m is order[0] else order[0]["rank"]
    ranked.sort(key=lambda v: v["rank"])
    for v in ranked:
        v["delta"] = v["score"] - ranked[0]["score"]
    return ranked, rest, sorted(shared)
