"""The user's library on the service in use: which of the favourite albums exist in a better
master.

Reading the favourites is cheap (one request per 100 to 500 albums). Checking an album is a full
comparison (around 25 requests on Qobuz; on Tidal the loudness of every album track on every
edition is read as well), so checks run in small batches, one album after the other, and stop by
themselves as soon as the service throttles a request or turns one down."""
import json
import threading
import time
from pathlib import Path

from . import core, discover

LIMIT = 25              # albums per run
MIN_GAIN = 1.0          # dB: below this two masters count as practically the same
COOLDOWN = 15 * 60      # seconds without library runs after Qobuz throttled a request
PAUSE = 1.0             # seconds between two albums of a run
WORKERS = 2             # parallel requests within one album of a run (a single comparison uses more)
RATE = 4.0              # requests per second in a run; parallel requests alone do not limit the rate,
                        # Qobuz answers within 100 ms (measured: up to 11 requests in one second)
BOX_TRACKS = 30         # an only edition with this many tracks is taken for a box set or compilation
BOX_MEDIA = 3
BOX_TYPES = ("boxset", "compilation", "bestof")
MAX_ALBUMS = 5000
PAGE = 500
# Tidal's loudness values for 192 kHz releases came out about 1 dB louder than for the same master
# in other resolutions (measured on two albums, 25 tracks, against Qobuz). A "better master" that
# rests on such a difference is marked as uncertain instead of being stated as a fact.
UNSURE_RATE = 176.4
UNSURE_GAIN = 2.0


def favourites(client):
    """All albums marked as favourites on the service. Returns (items, shape)."""
    return client.favourite_albums(MAX_ALBUMS, PAGE)


def entry(item, album_url=None):
    """One favourite album as the interface shows it. album_url: the service's link to an album."""
    d = core.describe(item)
    # a list that only says "hi-res" (Tidal) gets no invented sampling rate
    shown = d["format"] if d["format"] != "?" else ("24-bit" if d["bits"] > 16 else "")
    return {"id": d["id"], "label": d["label"], "title": discover.display_title(item.get("title")) or d["label"],
            "artist": d["artist"], "artist_id": (item.get("artist") or {}).get("id"),
            "format": shown, "bits": d["bits"], "rate": d["rate"], "year": d["year"],
            "tracks": item.get("tracks_count") or 0, "media": item.get("media_count") or 0,
            "cover": discover.cover(item, "small"),
            "url": album_url(d["id"]) if album_url else f"https://play.qobuz.com/album/{d['id']}"}


def brief(v):
    a = v.get("assessment") or {}
    return {"id": str(v["id"]), "label": v["label"], "format": v["format"], "bits": v["bits"], "rate": v["rate"],
            "year": v["year"], "dynamics": v.get("dynamics"), "level": a.get("level"), "verdict": a.get("label"),
            "url": v.get("url"), "tracks": v.get("total"), "release_type": v.get("release_type") or ""}


def adjust(entry, check):
    """A box set or compilation that is the only release of its name is 'the only edition' by
    definition - that says nothing about the albums on it. Such a result is shown as not
    comparable instead. Applied when a result is stored and when stored results are read."""
    if not check or check.get("kind") != "best" or not check.get("only"):
        return check
    mine = check.get("mine") or {}
    kind = str(mine.get("release_type") or "").lower()
    tracks = mine.get("tracks") or entry.get("tracks") or 0
    if kind in BOX_TYPES or (entry.get("media") or 0) >= BOX_MEDIA:
        reason = "Box set or compilation: it is compared as a whole. The albums on it are not checked one by one."
    elif tracks >= BOX_TRACKS:
        reason = (f"{tracks} tracks and no other edition: probably a box set or compilation. It is compared "
                  "as a whole; the albums on it are not checked one by one.")
    else:
        return check
    return dict(check, kind="other", box=True, reason=reason)


def outcome(result, album_id, service="Qobuz"):
    """What a comparison means for the edition in the library:
    master      another edition is at least MIN_GAIN dB more dynamic
    resolution  the same (or practically the same) master exists in a higher resolution
    best        nothing better on the service
    other       the edition cannot be compared (live, mono, no loudness data ...), with the reason"""
    album_id = str(album_id)
    group = result["groups"][0] if result.get("groups") else None
    ranked = group["ranked"] if group else []
    unranked = group["unranked"] if group else []
    mine = next((v for v in ranked + unranked if str(v["id"]) == album_id), None)
    if mine is None:
        ex = next((v for v in result.get("excluded", []) + result.get("partials", [])
                   if str(v["id"]) == album_id), None)
        if ex is None:
            return {"kind": "other", "reason": f"{service} did not return this release - it may have been withdrawn."}
        reason = ex.get("excluded_reason") or "Not an edition of the album: it holds only some of its tracks."
        return {"kind": "other", "mine": brief(ex), "reason": reason}
    out = {"mine": brief(mine), "editions": len(ranked) + len(unranked)}
    if not ranked or mine not in ranked:
        if len(ranked) + len(unranked) == 1:
            return dict(out, kind="best", only=True)
        return dict(out, kind="other", reason="Shares no track with loudness data with the other editions.")
    best = ranked[0]
    if best is mine:
        return dict(out, kind="best", only=len(ranked) == 1)
    master = lambda v: v.get("same_as") or v["rank"]
    same = master(mine) == master(best)
    higher = (best["bits"], best["rate"]) > (mine["bits"], mine["rate"])
    gain = None
    if mine.get("dynamics") is not None and best.get("dynamics") is not None:
        gain = round(best["dynamics"] - mine["dynamics"], 1)
    out.update(best=brief(best), gain=gain, same_master=same)
    if not same and gain is None:
        return dict(out, kind="other", reason=f"{service} has no peak levels for these editions, so the gain "
                                              "cannot be stated. Open the comparison for the details.")
    if not same and gain >= MIN_GAIN:
        if (result.get("service") == "tidal" and gain < UNSURE_GAIN
                and (mine["rate"] >= UNSURE_RATE) != (best["rate"] >= UNSURE_RATE)):
            out["unsure"] = ("One of the two editions is a 176.4 or 192 kHz release. Tidal's loudness values for "
                             "these came out about 1 dB too loud in the two albums that could be checked against "
                             "Qobuz - a gain below 2 dB may be no real difference.")
        return dict(out, kind="master")
    if higher:
        # First the upgrade of the very edition in the library; the edition with the most content
        # (first in the ranking) follows as a second suggestion.
        twins = [v for v in ranked if v is not mine and core.norm(v["label"]) == core.norm(mine["label"])
                 and master(v) in (master(mine), master(best))
                 and (v["bits"], v["rate"]) > (mine["bits"], mine["rate"])]
        if twins:
            twin = max(twins, key=lambda v: (v["bits"], v["rate"], -v["rank"]))
            if twin is not best:
                more = max(0, (best.get("total") or 0) - (twin.get("total") or 0))
                also = dict(brief(best), more_tracks=more, same_master=same, gain=gain)   # vs. the library's edition
                out.update(best=brief(twin), also=also, same_master=master(twin) == master(mine))
                if mine.get("dynamics") is not None and twin.get("dynamics") is not None:
                    out["gain"] = round(twin["dynamics"] - mine["dynamics"], 1)
        return dict(out, kind="resolution")
    return dict(out, kind="best", equal=True)


class Store:
    """Results of the library checks, kept between sessions (path=None: only in memory)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.data = {"checked": {}, "paused_until": 0}
        if self.path:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("checked"), dict):
                    self.data.update(checked=data["checked"], paused_until=data.get("paused_until") or 0)
            except (OSError, ValueError):
                pass

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def checked(self):
        with self.lock:
            return dict(self.data["checked"])

    def put(self, album_id, record):
        with self.lock:
            self.data["checked"][str(album_id)] = record
            self._save()

    def forget(self, album_ids):
        with self.lock:
            for one in album_ids:
                self.data["checked"].pop(str(one), None)
            self._save()

    def pause(self, until):
        with self.lock:
            self.data["paused_until"] = until
            self._save()

    def paused(self):
        """Seconds until library runs are allowed again (0 = now)."""
        with self.lock:
            return max(0, int(self.data.get("paused_until", 0) - time.time()))
