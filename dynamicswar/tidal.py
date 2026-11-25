"""The Tidal service: the same methods as the Qobuz client, albums handed out in the same form.

Tidal's catalogue calls give the peak of every track, but the loudness of a track and the exact
format (bit depth, sampling rate) only come with the call the player makes before it plays a
track ("playbackinfo"). That call is the delicate one, so it is treated with care:

  - only for the tracks a ranking needs: the album's own tracks on the releases that turned
    out to be editions - not bonus material, not compilations that merely were candidates
  - one call per track, once: the values are kept in a file and never asked for again
  - at most two such calls per second, counted from when each one leaves
  - the first call Tidal turns down stops them for a quarter of an hour
  - the answer also carries the access to the stream ("manifest"); it is dropped on the spot,
    never stored and never used - no audio is loaded

Unofficial interface, same footing as the Qobuz one: the token comes from the user's own web
player. Calls that the feasibility test did not cover (artist releases, track search, lookup by
ISRC, favourites) are tried once per session and left alone when Tidal does not know them.
"""
import base64
import json
import re
import threading
import time
import urllib.parse
from pathlib import Path

from . import core
from .client import MAX_WAIT, RETRY_STATUS, THROTTLED, Halted, Refused, Traffic, rate_headers

API = "https://api.tidal.com/v1/"
IMAGES = "https://resources.tidal.com/images/"
QUALITY = "HI_RES_LOSSLESS"         # best stereo quality; Tidal answers with what the track really has
LOSSLESS = ("LOSSLESS", "HI_RES_LOSSLESS", "HI_RES")
PLAYBACK_RATE = 2.0                 # loudness calls per second. Was 1.0: more than 400 calls at that rate
                                    # passed without a reaction from Tidal, and a first comparison of an album
                                    # with ten editions took two minutes, which was too slow
COOLDOWN = 15 * 60                  # seconds without loudness calls after Tidal turned one down
MAX_CALLS = 200                     # loudness calls per comparison, as a safety net
PLAYBACK_KEEP = ("audioQuality", "audioMode", "bitDepth", "sampleRate", "trackReplayGain",
                 "trackPeakAmplitude", "albumReplayGain", "albumPeakAmplitude")     # nothing of the stream
TOKEN_PROBLEMS = (6001, 6004, 11001, 11002, 11003)      # subStatus of a 401 that means: sign in again
UNKNOWN_CALL = (400, 404, 405)      # answers that mean "Tidal does not offer this call (any more)"
ISRC = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
PAGE = 50


def endpoint_of(url):
    """'albums/{id}/tracks' for .../v1/albums/1781800/tracks?countryCode=DE"""
    path = url.split("?")[0].split("/v1/")[-1]
    return re.sub(r"(?<=/)\d+(?=/|$)", "{id}", path)[-40:]


def problem(error):
    """(subStatus, message) of a Tidal error answer, as far as it says so."""
    try:
        data = json.loads(getattr(error, "body", "") or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data.get("subStatus"), str(data.get("userMessage") or data.get("description") or "")[:120]


def claims(token):
    """What the token says about itself (user, country, expiry), read locally: Tidal's tokens are
    signed JSON. Returns {} for a token in another form."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def covers(cover):
    """Tidal names a cover by an id; the pictures sit under fixed addresses."""
    if not cover:
        return {}
    path = IMAGES + str(cover).replace("-", "/")
    return {"large": f"{path}/640x640.jpg", "small": f"{path}/320x320.jpg", "thumbnail": f"{path}/160x160.jpg"}


class TrackStore:
    """Loudness and format of every track read so far, kept between sessions, plus the time
    until which Tidal is left alone after it turned a loudness call down (path=None: memory)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.tracks, self.block = {}, {}
        if self.path:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.tracks = data.get("tracks") if isinstance(data.get("tracks"), dict) else {}
                    self.block = data.get("block") if isinstance(data.get("block"), dict) else {}
            except (OSError, ValueError):
                pass

    def get(self, track_id):
        with self.lock:
            return self.tracks.get(str(track_id))

    def put(self, track_id, values):
        with self.lock:
            self.tracks[str(track_id)] = values

    def set_block(self, block):
        with self.lock:
            self.block = block or {}
            self._save()

    def save(self):
        with self.lock:
            self._save()

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"tracks": self.tracks, "block": self.block}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass


class TidalClient(Traffic):
    NAME, LABEL = "tidal", "Tidal"
    OPEN_PREFIXES = ("https://tidal.com/", "https://listen.tidal.com/")
    BASE_RATE = 4.0                 # all calls: never more than four per second
    LIBRARY_CALLS = 300             # loudness requests per library run: a run ends by itself once that
                                    # many are sent (measured so far without reaction: 419 in one go)
    LAZY = True                     # loudness is read after the editions are known: see measure()

    def __init__(self, cfg, fetch=core.http_fetch, save=lambda cfg: None, retries=2, pause=0.6,
                 store=None, playback_rate=PLAYBACK_RATE, cooldown=COOLDOWN, max_calls=MAX_CALLS):
        self.cfg, self.save = cfg, save
        self._traffic()
        self.fetch = self._logged(fetch)
        self.retries, self.pause = retries, pause
        self.store = store if store is not None else TrackStore()
        self.playback_rate, self.cooldown, self.max_calls = playback_rate, cooldown, max_calls
        self.measured = 0           # loudness calls answered in this session
        self.reused = 0             # tracks whose values came from the file
        self.unknown = {}           # calls Tidal did not know: {name: status}
        self._listed = {}           # albums as the search or an artist's list described them
        self._play_lock = threading.Lock()
        self._play_next = 0.0
        self._cache_lock = threading.Lock()
        self._albums = {}

    endpoint = staticmethod(endpoint_of)

    @staticmethod
    def _detail(error):
        sub, text = problem(error)
        return " ".join(str(x) for x in (sub, text) if x) or None

    # --------------------------------------------------------------- account

    def _account(self):
        account = self.cfg.get("tidal")
        return account if isinstance(account, dict) else {}

    def connected(self):
        account = self._account()
        expires = account.get("expires")
        return bool(account.get("token")) and not (expires and expires < time.time())

    def expires(self):
        """When the token runs out (seconds since 1970), as the token itself says - or None."""
        return self._account().get("expires") if self._account().get("token") else None

    def country(self):
        return self._account().get("country") or "DE"

    def set_token(self, token):
        """Stores the token from the web player and checks it with one search."""
        token = (token or "").strip().strip("\"'")
        if token.lower().startswith("authorization"):
            token = token.split(":", 1)[-1].strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            raise core.ApiError(401, "No token entered.")
        said = claims(token)
        expires = said.get("exp") if isinstance(said.get("exp"), (int, float)) else None
        if expires and expires < time.time():
            raise core.ApiError(401, "This token has already expired. Copy a fresh one from the web player.")
        before = self.cfg.get("tidal")
        country = said.get("cc") if isinstance(said.get("cc"), str) and len(said["cc"]) == 2 else None
        self.cfg["tidal"] = {"token": token, "country": (country or self.country()).upper(),
                             "user_id": said.get("uid"), "expires": expires}
        try:
            self.call("search", query="test", types="ALBUMS", limit=1, offset=0)
        except core.ApiError:
            if before is None:
                self.cfg.pop("tidal", None)
            else:
                self.cfg["tidal"] = before
            raise
        self.save(self.cfg)

    def disconnect(self):
        self.cfg.pop("tidal", None)
        self.save(self.cfg)
        self.forget()

    def forget(self):
        with self._cache_lock:
            self._albums.clear()
            self._listed.clear()

    # ------------------------------------------------------------------ calls

    def call(self, path, **params):
        return self._get(path, params, self.retries)

    def _get(self, path, params, retries):
        token = self._account().get("token")
        if not token or not self.connected():
            raise core.ApiError(401, "not connected to Tidal")
        query = urllib.parse.urlencode({"countryCode": self.country(), **params})
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        for attempt in range(retries + 1):
            try:
                raw = self.fetch(f"{API}{path}?{query}", headers)
                try:
                    return json.loads(raw or b"null")
                except ValueError:
                    raise core.ApiError(502, "Tidal sent an answer that cannot be read")
            except core.ApiError as e:
                if attempt == retries or e.status not in RETRY_STATUS or self.halted:
                    raise
                wait = self.pause * (attempt + 1)
                asked = rate_headers(e.headers).get("retry-after", "")
                if asked.replace(".", "", 1).isdigit():
                    wait = max(wait, min(float(asked), MAX_WAIT))
                self.log.retry(wait)
                time.sleep(wait)

    def _paged(self, path, max_items, key=None, limit=PAGE, **params):
        """Reads a list page by page. Tidal's lists come as {items, totalNumberOfItems}, the
        search puts one such list under each type."""
        items = []
        while len(items) < max_items:
            data = self.call(path, limit=min(limit, max_items - len(items)), offset=len(items), **params)
            block = (data.get(key) if key else data) if isinstance(data, dict) else None
            block = block if isinstance(block, dict) else {}
            got = [it for it in (block.get("items") or []) if isinstance(it, dict)]
            items += got
            total = block.get("totalNumberOfItems")
            if not got or len(got) < limit or (isinstance(total, int) and len(items) >= total):
                break
        return items

    def _optional(self, name, fn, *args, **kw):
        """A call the feasibility test did not cover. When Tidal does not know it, that is noted
        for the report and the call is not tried again in this session."""
        if name in self.unknown:
            return None
        try:
            return fn(*args, **kw)
        except core.ApiError as e:
            if e.status == 401:
                raise
            if e.status in UNKNOWN_CALL:
                self.unknown[name] = e.status
            return None

    # ----------------------------------------------- loudness and format

    def blocked(self):
        until = (self.store.block or {}).get("until") or 0
        return max(0, int(until - time.time()))

    def _refuse(self, status, message):
        self.refusal = message
        return Refused(status, message)

    def _blocked_message(self):
        minutes = max(1, round(self.blocked() / 60))
        block = self.store.block or {}
        why = f"HTTP {block.get('status')}" + (f", {block['detail']}" if block.get("detail") else "")
        return (f"Tidal turned down a loudness request ({why}). DynamicsWar sends no further ones for "
                f"another {minutes} minute(s). The values read so far are kept.")

    def _playback(self, track_id):
        """Loudness and format of one track. Raises Refused when Tidal does not answer or turns
        the call down - the latter also stops these calls for a while."""
        with self._play_lock:
            if self.blocked():
                raise self._refuse(THROTTLED, self._blocked_message())
            if self.playback_rate:
                wait = self._play_next - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            try:
                try:
                    data = self._get(f"tracks/{track_id}/playbackinfo",
                                     {"audioquality": QUALITY, "playbackmode": "STREAM",
                                      "assetpresentation": "FULL"}, retries=0)
                finally:
                    # counted from the moment the call really left (it may have waited for its
                    # turn among all calls): two of them are never closer than the set distance
                    left = getattr(core.LAST, "sent", None) or time.monotonic()
                    self._play_next = left + (1.0 / self.playback_rate if self.playback_rate else 0)
            except core.ApiError as e:
                sub, text = problem(e)
                if e.status == 401 and (sub in TOKEN_PROBLEMS or "token" in text.lower()
                                        or (sub is None and not text)):
                    raise                           # the login ran out: ask for a new token
                if e.status == 0 or e.status >= 500:
                    raise self._refuse(e.status, "Tidal did not answer a loudness request "
                                       f"(HTTP {e.status}). Try again in a moment - the values read "
                                       "so far are kept.")
                self.store.set_block({"until": time.time() + self.cooldown, "status": e.status,
                                      "detail": self._detail(e), "track": str(track_id),
                                      "when": time.strftime("%Y-%m-%d %H:%M:%S")})
                raise self._refuse(e.status, self._blocked_message())
            data = data if isinstance(data, dict) else {}
            kept = {k: data.get(k) for k in PLAYBACK_KEEP}      # the stream access ends here
            del data
            self.measured += 1
            return {"gain": kept["trackReplayGain"], "peak": kept["trackPeakAmplitude"],
                    "album_gain": kept["albumReplayGain"], "album_peak": kept["albumPeakAmplitude"],
                    "bits": kept["bitDepth"], "rate": kept["sampleRate"], "quality": kept["audioQuality"],
                    "mode": kept["audioMode"], "when": time.strftime("%Y-%m-%d")}

    def measure(self, plan, progress=None, stop=None):
        """Reads loudness and format for the tracks the ranking needs: plan is
        [(album, [track ids])] from editions.needed - the album's own tracks on the releases that
        count as editions. Tracks known from the file cost nothing. progress(stage, done, total)
        and stop() (True: end now) come from the comparison that is running."""
        todo, seen = [], set()
        for album, ids in plan:
            want = {str(i) for i in ids}
            for t in (album.get("tracks") or {}).get("items") or []:
                one = str(t.get("id"))
                if one in want and one not in seen and (t.get("tidal") or {}).get("playable", True):
                    seen.add(one)
                    if self.store.get(one):
                        self.reused += 1
                    else:
                        todo.append(one)
        total, fresh = len(todo), 0
        try:
            for n, one in enumerate(todo[:self.max_calls]):
                if stop and stop():
                    raise Halted(0, "Comparison stopped. The values read so far are kept.")
                if progress:
                    progress("measure", n, total)
                self.store.put(one, self._playback(one))
                fresh += 1
        finally:
            if fresh:
                self.store.save()
        if total > self.max_calls:
            raise Halted(0, f"This album needs {total} loudness requests on Tidal. DynamicsWar reads at most "
                            f"{self.max_calls} per comparison. Start the comparison again to read the "
                            "rest - what was read is kept.")
        for album, ids in plan:
            self._fill(album, ids)

    def _fill(self, album, ids=None):
        """Puts what the file knows about an album's tracks into the album: loudness and peak
        per track and the gain of the loudest track as Tidal states it. With ids (the album's own
        tracks, from the plan of a comparison) also the exact format - that of those tracks, not
        of bonus material. Tidal can serve the tracks of one release in different formats
        (measured: a "Thriller" with seven tracks in 16/44.1 and two in 24 bit); then the lowest
        one stands for the release and format_mixed lists them all."""
        got, own = [], []
        want = None if ids is None else {str(i) for i in ids}
        for t in (album.get("tracks") or {}).get("items") or []:
            v = self.store.get(t.get("id"))
            if not v:
                continue
            got.append(v)
            if want is not None and str(t.get("id")) in want:
                own.append(v)
            t["audio_info"] = {"replaygain_track_gain": v.get("gain"),
                               "replaygain_track_peak": v.get("peak") if v.get("peak") is not None
                               else (t.get("tidal") or {}).get("listed_peak")}
            t.setdefault("tidal", {}).update(quality=v.get("quality"), bits=v.get("bits"), rate=v.get("rate"),
                                             album_gain=v.get("album_gain"), album_peak=v.get("album_peak"))
            # Every track of a release carries the same album value - unless Tidal plays the
            # track from another release. That makes the value the mark of where the audio is from.
            t["source"] = v.get("album_gain")
        formats = sorted({(v["bits"], v["rate"]) for v in own
                          if v.get("quality") in LOSSLESS and v.get("bits") and v.get("rate")})
        if formats:
            album["maximum_bit_depth"], album["maximum_sampling_rate"] = formats[0][0], formats[0][1] / 1000
            album["format_mixed"] = [f"{bits}/{rate / 1000:g}" for bits, rate in formats] if len(formats) > 1 else None
        # Tidal's album value is the gain of the release's loudest track. When the tracks carry
        # different ones, they are played from other releases and the values belong to those:
        # then the loudest track is worked out from this release's own tracks instead.
        gains = {v["album_gain"] for v in got if v.get("album_gain") is not None}
        album["loudest_gain"] = gains.pop() if len(gains) == 1 else None
        return album

    # ------------------------------------------------- what a service offers

    def album_url(self, album_id):
        return f"https://tidal.com/album/{album_id}"

    def _entry(self, a, artist=None):
        """An album of a list (search, releases) in the form the search and the comparison read.
        The exact format is unknown here; the tags only say whether a hi-res version exists."""
        who = a.get("artist") or next(iter(a.get("artists") or []), None) or artist or {}
        who = who if isinstance(who, dict) else {"name": who}
        tags = (a.get("mediaMetadata") or {}).get("tags") or []
        return {"id": str(a.get("id")), "title": a.get("title") or "", "version": a.get("version"),
                "artist": {"id": who.get("id"), "name": who.get("name")},
                "image": covers(a.get("cover")),
                "maximum_bit_depth": 24 if "HIRES_LOSSLESS" in tags else None, "maximum_sampling_rate": None,
                "release_date_original": a.get("releaseDate"),
                "release_date_stream": str(a.get("streamStartDate") or "")[:10] or None,
                "label": None, "copyright": a.get("copyright"),
                "release_type": str(a.get("type") or "").lower() or None,
                "tracks_count": a.get("numberOfTracks"), "media_count": a.get("numberOfVolumes"),
                "upc": a.get("upc"), "url": self.album_url(a.get("id")), "service": self.NAME}

    def _remember(self, albums):
        """Search and artist lists describe an album as completely as the album call itself."""
        with self._cache_lock:
            for a in albums:
                if a.get("id") is not None and "numberOfTracks" in a and (a.get("artist") or a.get("artists")):
                    self._listed[str(a["id"])] = a
        return albums

    @staticmethod
    def _wanted(a):
        """Singles are no editions of an album, and each one would cost loudness calls."""
        return a.get("id") is not None and str(a.get("type") or "").upper() != "SINGLE"

    def find_albums(self, query, max_items=100):
        """Album search, best hits first."""
        items = self._paged("search", max_items, key="albums", query=query, types="ALBUMS")
        return [self._entry(a) for a in self._remember(items) if self._wanted(a)]

    def album(self, album_id):
        """An album with its tracks: titles, lengths, ISRCs and the peak of every track - what
        Tidal's catalogue gives without the delicate call. Loudness and exact format are added
        from the file where they are known; the rest is read by measure(), and only for the
        tracks a ranking needs."""
        key = str(album_id)
        with self._cache_lock:
            hit = self._albums.get(key)
            meta = self._listed.get(key)
        if hit is not None:
            return self._fill(hit)
        if meta is None:                            # not seen in a list with all its details
            meta = self.call(f"albums/{key}")
        rows = self._paged(f"albums/{key}/tracks", 1000, limit=100)
        data = self._entry(meta if isinstance(meta, dict) else {"id": key})
        data["maximum_bit_depth"] = None            # exact values only: set by _fill
        items = []
        for t in rows:
            items.append({
                "id": t.get("id"), "title": t.get("title") or "", "version": t.get("version"),
                "duration": t.get("duration"), "isrc": t.get("isrc"),
                "media_number": t.get("volumeNumber"), "track_number": t.get("trackNumber"),
                "audio_info": {"replaygain_track_gain": None, "replaygain_track_peak": t.get("peak")},
                # kept for the reports: what Tidal says beyond the two values the comparison reads
                "tidal": {"listed_peak": t.get("peak"), "listed_gain": t.get("replayGain"),
                          "playable": t.get("streamReady") is not False and t.get("allowStreaming") is not False}})
        data["tracks"] = {"total": len(items), "items": items}
        data["tracks_count"] = data.get("tracks_count") or len(items)
        with self._cache_lock:
            self._albums[key] = data
        return self._fill(data)


    def _artist_ids(self, name):
        want = core.norm(name)
        found = self._paged("search", 10, key="artists", limit=10, query=name, types="ARTISTS")
        return [a["id"] for a in found if core.norm(a.get("name")) == want and a.get("id")]

    def releases(self, pool, artist, artist_id=None):
        """The albums listed under the artist. pool is not needed: one page is usually all."""
        ids = [artist_id] if artist_id else (self._optional("artist search", self._artist_ids, artist) or [])[:3]
        items = []
        for one in ids:
            who = {"id": one, "name": artist}
            got = self._optional("artist albums", self._paged, f"artists/{one}/albums", 300, limit=100) or []
            items += [self._entry(a, who) for a in self._remember(got) if self._wanted(a)]
        return items

    def albums_with_track(self, query, max_items=100):
        """Albums on which a track occurs, by title or by ISRC."""
        if ISRC.match(query.strip().upper()):
            tracks = self._optional("isrc lookup", self._paged, "tracks", PAGE, isrc=query.strip().upper()) or []
        else:
            # Tidal's search is generous: asked for one song it also lists the artist's other
            # songs (measured: every Michael Jackson album came back for the songs of "Thriller").
            # Only tracks whose title is part of what was asked for count, and the first page
            # holds them.
            asked = f" {core.norm(query)} "
            tracks = self._optional("track search", self._paged, "search", min(max_items, PAGE), key="tracks",
                                    query=query, types="TRACKS") or []
            tracks = [t for t in tracks
                      if f" {core.split_title(t.get('title'), t.get('version'))[0]} ".strip(" ") and
                      f" {core.split_title(t.get('title'), t.get('version'))[0]} " in asked]
        albums = []
        for t in tracks:
            alb = t.get("album") or {}
            if alb.get("id"):
                albums.append(self._entry(alb, t.get("artist") or next(iter(t.get("artists") or []), None)))
        return albums

    def favourite_albums(self, max_albums=5000, page=100):
        """All albums marked as favourites. Returns (items, shape of the raw answer)."""
        user = self._account().get("user_id")
        if not user:
            session = self.call("sessions")
            user = session.get("userId") if isinstance(session, dict) else None
        if not user:
            raise core.ApiError(0, "Tidal did not say which account the token belongs to")
        rows = self._paged(f"users/{user}/favorites/albums", max_albums, limit=min(page, 100),
                           order="DATE", orderDirection="DESC")
        albums = [r.get("item") if isinstance(r.get("item"), dict) else r for r in rows]
        shape = {"row_keys": sorted(map(str, rows[0]))[:30] if rows else [],
                 "item_keys": sorted(map(str, albums[0]))[:80] if albums else [], "total": len(rows)}
        return [self._entry(a) for a in albums if a.get("id") is not None], shape

    def diagnostics(self):
        return {"loudness_calls": self.measured, "tracks_from_file": self.reused,
                "unknown_calls": dict(self.unknown), "blocked_seconds": self.blocked(),
                "block": dict(self.store.block or {}) or None}

