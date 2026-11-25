"""What every service client shares (log of answers, rate limit, strict mode) and the Qobuz
service: same API access as the engine, plus retries, an in-memory album cache and paging
of long track lists. The Tidal service lives in tidal.py and offers the same methods."""
import collections
import threading
import time

from . import core

RETRY_STATUS = (0, 429, 500, 502, 503, 504)
THROTTLED = 429                     # "too many requests": the warning before access is restricted
RATE_HEADERS = ("retry-after", "ratelimit", "rate-limit")
MAX_WAIT = 10.0                     # longest pause before a retry, whatever Qobuz asks for


def endpoint_of(url):
    return url.split("?")[0].split("/0.2/")[-1][-40:]


def rate_headers(headers):
    """The headers of an answer that say something about limits (names in lower case)."""
    if not headers:
        return {}
    try:
        pairs = list(headers.items())
    except AttributeError:
        return {}
    return {str(k).lower(): str(v)[:80] for k, v in pairs if any(w in str(k).lower() for w in RATE_HEADERS)}


class RequestLog:
    """What was sent to Qobuz and how it answered. mark() before a piece of work and since(mark)
    after it give the numbers of that piece: requests, status codes, throttled requests (429)
    per endpoint, retries, waiting time and the highest number of requests within one second."""

    def __init__(self):
        self.lock = threading.Lock()
        self.total = 0
        self.status = collections.Counter()
        self.throttled = collections.Counter()      # per endpoint
        self.failed = collections.Counter()         # per endpoint: no connection or server error
        self.retries = 0
        self.waited = 0.0
        self.times = collections.deque(maxlen=5000)
        self.events = collections.deque(maxlen=200)
        self.headers = {}
        self.last_throttled = 0.0                   # clock time of the last 429, for the status light

    def note(self, endpoint, status, headers=None, sent=None, detail=None):
        """sent: when the request went out - that is what a rate limit on the other side counts.
        detail: the reason the service gave for an error, if it gave one."""
        now = sent or time.monotonic()
        limits = rate_headers(headers)
        with self.lock:
            self.total += 1
            self.status[str(status)] += 1
            self.times.append(now)
            self.headers.update(limits)
            if status == THROTTLED:
                self.throttled[endpoint] += 1
                self.last_throttled = time.time()
            elif status == 0 or status >= 500:
                self.failed[endpoint] += 1
            if status != 200:
                event = {"at": now, "endpoint": endpoint, "status": status,
                         "retry_after": limits.get("retry-after")}
                if detail:
                    event["detail"] = str(detail)[:160]
                self.events.append(event)

    def retry(self, seconds):
        with self.lock:
            self.retries += 1
            self.waited += seconds

    def mark(self):
        with self.lock:
            return {"at": time.monotonic(), "total": self.total, "status": collections.Counter(self.status),
                    "throttled": collections.Counter(self.throttled), "failed": collections.Counter(self.failed),
                    "retries": self.retries, "waited": self.waited}

    def since(self, mark):
        with self.lock:
            times = [t for t in self.times if t >= mark["at"]]
            peak, start = 0, 0
            times.sort()
            for end, t in enumerate(times):
                while t - times[start] >= 1.0:
                    start += 1
                peak = max(peak, end - start + 1)
            throttled = self.throttled - mark["throttled"]
            failed = self.failed - mark["failed"]
            return {
                "requests": self.total - mark["total"],
                "status": dict(self.status - mark["status"]),
                "throttled": sum(throttled.values()),
                "throttled_endpoints": dict(throttled),
                "failed": sum(failed.values()),
                "failed_endpoints": dict(failed),
                "retries": self.retries - mark["retries"],
                "waited_seconds": round(self.waited - mark["waited"], 2),
                "peak_per_second": peak,
                "rate_headers": dict(self.headers),
                "events": [dict(e, at=round(e["at"] - mark["at"], 2)) for e in self.events
                           if e["at"] >= mark["at"]][:40],
            }


class Refused(core.ApiError):
    """A request the service turned down or left unanswered, with a message that is already
    written for the user (English, names the service)."""
    plain = True


class Halted(Refused):
    """Work the app ended by itself: the user stopped it, or it reached its own limit."""


class Traffic:
    """What every service client needs towards its service: the log of answers, an optional
    rate limit and the strict mode that stops everything at the first throttled request."""

    BASE_RATE = None                # requests per second outside library runs (None: no limit)
    LIBRARY = True                  # library checks are available for this service
    LIBRARY_CALLS = None            # loudness requests after which a library run ends by itself
    LAZY = False                    # True: loudness is read in a step of its own (see measure)
    refusal = None                  # what the user is told about the last request the service turned down

    @staticmethod
    def endpoint(url):
        """Name of the endpoint a URL belongs to, for the log (never with the query string)."""
        return endpoint_of(url)

    @staticmethod
    def _detail(error):
        """The reason a service gives in an error answer, for the log."""
        return None

    def blocked(self):
        """Seconds for which the service must be left alone after it turned a request down."""
        return 0

    def diagnostics(self):
        """What the diagnostic report should say about the service besides the request log."""
        return {}

    def measure(self, plan, progress=None, stop=None):
        """Reads the loudness of the tracks the ranking needs: plan is [(album, [track ids])] from
        editions.needed. Only a service that sets LAZY has anything to do here."""

    @staticmethod
    def _quiet(fn, *args, **kw):
        """A failing lookup counts as 'nothing found' - except an expired login."""
        try:
            return fn(*args, **kw)
        except core.ApiError as e:
            if e.status == 401:
                raise
            return None

    def _traffic(self):
        self.log = RequestLog()
        self.strict = False         # library runs: stop at the first throttled request instead of retrying
        self.halted = False         # set by a throttled request in strict mode: nothing is sent any more
        self.rate = None            # library runs: at most this many requests per second
        self._rate_lock = threading.Lock()
        self._next = 0.0

    def _logged(self, fetch):
        def run(url, *args, **kw):
            if self.halted:
                raise core.ApiError(THROTTLED, "stopped after the service throttled a request")
            endpoint = self.endpoint(url)
            sent = self._slot()
            if self.halted:                 # throttled while this request was waiting for its turn
                raise core.ApiError(THROTTLED, "stopped after the service throttled a request")
            core.LAST.headers = None
            core.LAST.sent = sent           # for callers that keep a distance of their own between requests
            try:
                raw = fetch(url, *args, **kw)
            except core.ApiError as e:
                self.log.note(endpoint, e.status, e.headers or getattr(core.LAST, "headers", None), sent,
                              self._detail(e))
                if e.status == THROTTLED and self.strict:
                    self.halted = True
                raise
            self.log.note(endpoint, 200, getattr(core.LAST, "headers", None), sent)
            return raw
        return run

    def _slot(self):
        """With a rate set, requests leave one at a time and at least 1/rate seconds apart: never
        more than `rate` within a second. Returns the moment the request may go out. The wait
        happens inside the lock - measured from when the previous request really left, so a
        request that woke up late cannot push the next ones closer together."""
        rate = self.rate or self.BASE_RATE
        if not rate:
            return time.monotonic()
        with self._rate_lock:
            wait = self._next - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            now = time.monotonic()
            self._next = now + 1.0 / rate
            return now


class Client(Traffic, core.Qobuz):
    """The Qobuz service. Search, comparison and library only use the methods of the section
    "what a service offers" below - a second service implements the same ones and hands out
    albums in the same form (the one Qobuz uses: title, version, artist, tracks with ISRC,
    duration, loudness and peak)."""

    NAME, LABEL = "qobuz", "Qobuz"
    OPEN_PREFIXES = ("https://play.qobuz.com/", "https://open.qobuz.com/", "https://www.qobuz.com/")

    def __init__(self, cfg, fetch=core.http_fetch, save=lambda cfg: None, retries=2, pause=0.6):
        core.Qobuz.__init__(self, cfg, fetch, save)
        self._traffic()
        self.retries, self.pause = retries, pause
        self._id_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._albums = {}
        self.fetch = self._logged(fetch)

    def app_id(self):
        with self._id_lock:
            return super().app_id()

    def call(self, endpoint, post=False, **params):
        for attempt in range(self.retries + 1):
            try:
                return super().call(endpoint, post, **params)
            except core.ApiError as e:
                if attempt == self.retries or e.status not in RETRY_STATUS or self.halted:
                    raise
                wait = self.pause * (attempt + 1)
                asked = rate_headers(e.headers).get("retry-after", "")
                if asked.replace(".", "", 1).isdigit():         # Qobuz says how long to wait
                    wait = max(wait, min(float(asked), MAX_WAIT))
                self.log.retry(wait)
                time.sleep(wait)

    def album(self, album_id):
        """album/get with cache. Qobuz returns long track lists in pages; the rest is fetched
        when the reported total is larger than what came back."""
        key = str(album_id)
        with self._cache_lock:
            hit = self._albums.get(key)
        if hit is not None:
            return hit
        data = super().album(key)
        block = data.get("tracks") or {}
        items = list(block.get("items") or [])
        total = block.get("total")
        while isinstance(total, int) and items and len(items) < total:
            try:
                more = self.call("album/get", album_id=key, offset=len(items), limit=50)
            except core.ApiError:
                break
            got = (more.get("tracks") or {}).get("items") or []
            if not got or got[0].get("id") == items[0].get("id"):
                break                       # server ignores the offset: keep what we have
            items += got
        if block:
            block["items"] = items
        data["url"] = self.album_url(key)
        with self._cache_lock:
            self._albums[key] = data
        return data

    def forget(self):
        with self._cache_lock:
            self._albums.clear()

    # ------------------------------------------------- what a service offers

    def connected(self):
        return bool(self.cfg.get("user_auth_token"))

    def disconnect(self):
        self.cfg.pop("user_auth_token", None)
        self.save(self.cfg)
        self.forget()

    def album_url(self, album_id):
        return f"https://play.qobuz.com/album/{album_id}"

    def find_albums(self, query, max_items=100):
        """Album search, best hits first. A hundred are enough for the result list."""
        return self.paged("catalog/search", "albums", max_items, 50, query=query, type="albums")

    def releases(self, pool, artist, artist_id=None):
        """Everything listed under the artist. artist/get groups editions and does not show each
        one, so the release list is read as well. pool: executor for the parallel pages."""
        ids = [artist_id] if artist_id else (self._quiet(self.search_artist_ids, artist) or [])[:3]
        grouped = [pool.submit(self._quiet, self.paged, "artist/get", "albums", 3000, 500,
                               artist_id=one, extra="albums") for one in ids]
        items = []
        for one in ids:
            items += self._release_pages(pool, one) or self._release_pages(pool, one, release_type="album")
        for f in grouped:
            items += f.result() or []
        return items

    def favourite_albums(self, max_albums=5000, page=500):
        """All albums marked as favourites. Returns (items, shape); shape describes the raw
        answer for the diagnostic report, because the endpoint is not documented."""
        items, seen, shape = [], set(), None
        while len(items) < max_albums:
            data = self.call("favorite/getUserFavorites", type="albums", limit=page, offset=len(items))
            block = data.get("albums") if isinstance(data, dict) else None
            block = block if isinstance(block, dict) else {}
            got = [it for it in (block.get("items") or []) if isinstance(it, dict) and it.get("id")]
            if shape is None:
                shape = {"keys": sorted(map(str, data))[:30] if isinstance(data, dict) else type(data).__name__,
                         "albums_keys": sorted(map(str, block))[:20], "total": block.get("total"),
                         "item_keys": sorted(map(str, got[0]))[:80] if got else []}
            new = [it for it in got if str(it["id"]) not in seen]
            if not new:
                break
            seen.update(str(it["id"]) for it in new)
            items += new
            total = block.get("total")
            if (isinstance(total, int) and len(items) >= total) or len(got) < page:
                break
        return items, shape

    def albums_with_track(self, query, max_items=100):
        """Albums on which a track occurs. The first hundred hits are enough to find editions;
        reading all pages made comparisons slow."""
        albums = []
        for t in self.paged("catalog/search", "tracks", max_items, 50, query=query, type="tracks"):
            alb = dict(t.get("album") or {})
            if alb.get("id"):
                if not alb.get("artist"):
                    alb["artist"] = t.get("performer") or {}
                albums.append(alb)
        return albums

    # ------------------------------------------------------- Qobuz internals

    def _release_pages(self, pool, artist_id, max_items=3000, wave=5, **extra):
        """artist/getReleasesList, pages fetched in parallel waves. The first page shows how many
        entries the server really returns per page (it may cap the requested size)."""
        def fetch(offset):
            return self._quiet(self.call, "artist/getReleasesList", artist_id=artist_id, limit=100,
                               offset=offset, **extra)

        first = fetch(0)
        items = list((first or {}).get("items") or [])
        step = len(items)
        more = bool(first and first.get("has_more") is not False and step)
        offset = step
        while more and offset < max_items:
            offsets = [offset + i * step for i in range(wave)]
            for data in pool.map(fetch, offsets):
                got = (data or {}).get("items") or []
                if not got or got[0].get("id") == items[0].get("id"):
                    more = False                # end reached, or the server ignores the offset
                    break
                items += got
                if data.get("has_more") is False or len(got) < step:
                    more = False
                    break
            offset += wave * step
        return items
