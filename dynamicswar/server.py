"""Local HTTP server behind the interface. Listens on 127.0.0.1 only; every API call
needs the session key that is handed to the page when it loads."""
import collections
import json
import mimetypes
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, config, core, discover, editions, history, library
from .client import Client, Halted, Refused
from .tidal import TidalClient, TrackStore

WEB = Path(__file__).with_name("web")
STATIC = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg"}
MAX_JOBS = 20


def friendly(e, service="Qobuz"):
    """What the user is told about an error from a service."""
    if getattr(e, "plain", False):          # already written for the user
        return e.message
    if e.status == 0:
        if "app_id" in e.message or "bundle" in e.message:
            return "Could not read the app ID from the Qobuz web player - it may have changed."
        return f"Cannot reach {service}. Check your internet connection."
    if e.status == 401:
        if "already expired" in e.message or "No token" in e.message:
            return e.message
        return f"{service} rejected the token - it was mistyped or has expired."
    if e.status == 429:
        return f"{service} is slowing requests down at the moment. Wait a few minutes and try again."
    return f"{service} answered with an error ({e.status}): {e.message}"


class State:
    def __init__(self, cfg=None, save=None, fetch=core.http_fetch, demo=False, report_dir=None,
                 library_path=None, history_path=None, service=None, tidal_path=None):
        self.cfg = config.load() if cfg is None else cfg
        self.save = save or config.save
        self.requests = 0
        self.endpoints = collections.Counter()
        self.report_dir = report_dir      # where diagnostic reports of each comparison go (or None)
        self.demo = demo

        def counted(name_of):
            def run(url, *args, **kw):
                self.requests += 1
                self.endpoints[name_of(url)] += 1
                return fetch(url, *args, **kw)
            return run

        # the services; search, comparison, library and timeline follow the chosen one
        tracks = TrackStore(None if demo else (tidal_path or config.config_dir() / "tidal.json"))
        self.clients = {"qobuz": Client(self.cfg, fetch=counted(Client.endpoint),
                                        save=self._saving(save, ("tidal",))),
                        "tidal": TidalClient(self.cfg, fetch=counted(TidalClient.endpoint), store=tracks,
                                             save=self._saving(save, ("user_auth_token", "app_id")))}
        chosen = "qobuz" if demo else (service or self.cfg.get("service"))
        self.service = chosen if chosen in self.clients else "qobuz"
        # results of the library checks, one file per service; the demo keeps them in memory only
        lib = Path(library_path or config.config_dir() / "library.json")
        self.stores = {"qobuz": library.Store(None if demo else lib),
                       "tidal": library.Store(None if demo else lib.with_name(f"{lib.stem}-tidal{lib.suffix}"))}
        self.favourites = None            # (time read, entries, shape of the raw answer)
        # what has been compared so far, for the timeline
        self.history = history.Store(None if demo else (history_path or config.config_dir() / "history.json"))
        self.history_read = False         # the reports of earlier comparisons are read in on first use
        self.library_job = None
        self.library_pause = library.PAUSE
        self.library_rate = library.RATE
        self.closing = False              # the app is shutting down: running work ends at its next step
        self.quit = False                 # a newer start asked this one to end (see app.replace_instance)
        self.session = secrets.token_urlsafe(18)
        self.jobs, self.lock = {}, threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.started = time.time()
        self.last_seen = time.monotonic()
        self.bye_at = None

    @staticmethod
    def _saving(save, foreign):
        """How one service stores the settings. Two windows (one per service) may be open: what
        belongs to the other service is taken from the file as it is now, not from this
        window's copy - otherwise saving here would undo a sign-in made over there."""
        if save is not None:
            return save

        def run(cfg):
            fresh = config.load()
            for key in foreign:
                if key in fresh:
                    cfg[key] = fresh[key]
                else:
                    cfg.pop(key, None)
            config.save(cfg)
        return run

    @property
    def client(self):
        return self.clients[self.service]

    @property
    def store(self):
        return self.stores[self.service]

    def friendly(self, e):
        return friendly(e, self.client.LABEL)

    # ---------------------------------------------------------------- API

    def stale(self):
        """True when program files changed after this process started: the window may already show
        the new interface while the analysis still runs the old code - a restart is needed."""
        try:
            return any(f.stat().st_mtime > self.started for f in Path(__file__).parent.glob("*.py"))
        except OSError:
            return False

    def throttled(self):
        """Seconds for which the service counts as throttling: while library checks are paused,
        for the same length of time after any request was answered with 429, or while the service
        is left alone because it turned a request down."""
        recent = library.COOLDOWN - (time.time() - self.client.log.last_throttled)
        paused = self.store.paused() if self.client.LIBRARY else 0
        return max(0, paused, int(recent), self.client.blocked())

    def status(self):
        client = self.client
        connected = self.demo or client.connected()
        return {"version": __version__, "demo": self.demo, "connected": connected,
                "service": {"name": client.NAME, "label": client.LABEL, "library": client.LIBRARY,
                            "loudness_rate": getattr(client, "playback_rate", None) if client.LAZY else None},
                "throttled": self.throttled() if connected else 0,
                "stale": self.stale()}

    def _named(self, body):
        """The service a request names, else the one in use."""
        name = str(body.get("service") or self.service)
        if name not in self.clients:
            raise ValueError("unknown service")
        return self.clients[name]

    def login(self, body):
        client = self._named(body)
        token = body.get("token") or ""
        try:
            client.set_token(token)
        except core.ApiError as e:              # the message must name the service that was asked
            raise Refused(e.status, friendly(e, client.LABEL))
        client.forget()
        return self.status()

    def logout(self, body):
        self._named(body).disconnect()
        return self.status()

    # ----------------------------------------------------------- settings

    def _busy(self):
        return any(job["state"] == "running" for job in self.jobs.values())

    def settings(self):
        """What the Settings tab shows: the services, which one is in use, what is stored."""
        services = []
        for name, client in self.clients.items():
            store = getattr(client, "store", None)
            services.append({
                "name": name, "label": client.LABEL, "active": name == self.service,
                "connected": (self.demo and name == "qobuz") or client.connected(),
                "expires": getattr(client, "expires", lambda: None)(),
                "library": client.LIBRARY, "lazy": client.LAZY,
                "loudness_rate": getattr(client, "playback_rate", None) if client.LAZY else None,
                "blocked": client.blocked(),
                "stored_tracks": len(store.tracks) if store is not None else None,
                "compared": len(self.history.albums(name)),
            })
        return {"active": self.service, "busy": self._busy(), "demo": self.demo, "services": services,
                "folder": str(config.config_dir())}

    def use_service(self, body):
        """Makes a service the one search, comparison, library and timeline work with."""
        name = str(body.get("service") or "")
        if name not in self.clients:
            raise ValueError("unknown service")
        if self.demo and name != "qobuz":
            raise ValueError("The demo only has its own invented catalogue.")
        if self._busy():
            raise ValueError("A comparison or library check is running. Switch when it has finished.")
        if name != self.service:
            self.service = name
            self.favourites = None              # they belong to the other service
            self.cfg["service"] = name
            self.clients[name].save(self.cfg)
        return self.status()

    def search(self, query):
        query = " ".join((query or "").split())
        if len(query) < 2:
            return {"query": query, "groups": []}
        return {"query": query, "groups": discover.search(self.client, query)}

    def compare(self, body):
        job_id = secrets.token_urlsafe(8)
        job = {"id": job_id, "state": "running", "stage": "search", "done": 0, "total": 0,
               "result": None, "error": None, "stop": False, "started": time.time()}
        with self.lock:
            for old in sorted(self.jobs.values(), key=lambda j: j["started"])[:-MAX_JOBS + 1]:
                self.jobs.pop(old["id"], None)
            self.jobs[job_id] = job
        self.pool.submit(self._run, job, dict(body))
        return {"job": job_id}

    def _run(self, job, body):
        try:
            result = self._compare(body, lambda stage, done, total: job.update(stage=stage, done=done, total=total),
                                   job["started"], stop=lambda: job["stop"] or self.closing)
            job.update(result=result, state="done")
        except Halted as e:
            job.update(state="error", error={"status": 0, "message": e.message, "halted": True})
        except core.ApiError as e:
            job.update(state="error", error={"status": e.status, "message": self.friendly(e)})
        except Exception as e:                # show, do not hide: the window has no console
            job.update(state="error", error={"status": 500, "message": f"Unexpected error: {e!r}"})

    def compare_stop(self, body):
        """Ends a running comparison at its next step (it matters where a step is a request
        per track and second)."""
        job = self.jobs.get(str(body.get("job") or ""))
        if job and job["state"] == "running" and "stop" in job:
            job["stop"] = True
        return {"ok": True}

    def _compare(self, body, progress, started, workers=discover.WORKERS, stop=None):
        """One comparison: find the editions, rank them, write the report. Used by the single
        comparison and by library runs."""
        stages, seen, requests, endpoints = {}, [], self.requests, collections.Counter(self.endpoints)
        client = self.client
        mark, before = client.log.mark(), client.diagnostics()

        def say(stage, done, total):
            stages.setdefault(stage, time.time())
            progress(stage, done, total)

        albums, sources, truncated = discover.discover(
            client, body.get("artist") or "", body.get("title") or "",
            body.get("artist_id"), body.get("ids") or (), say, workers=workers, debug=seen)
        if client.LAZY:
            # Loudness costs one request per track on this service: first decide what is an
            # edition (titles, lengths and ISRCs are enough for that), then read only the
            # album's own tracks on those editions.
            try:
                client.measure(editions.needed(albums, body.get("title")), say, stop)
            except Halted:
                raise
            except Refused:
                # a ranking without the edition that could not be read would mislead
                self._refused_report(body, client, mark)
                raise
        progress("analyse", 0, 0)
        stages["analyse"] = time.time()
        result = editions.compare(albums, body.get("title"), client.LABEL)
        end = time.time()
        marks = sorted(stages.items(), key=lambda kv: kv[1]) + [("end", end)]
        traffic = client.log.since(mark)
        result.update(service=client.NAME, sources=sources, truncated=truncated, limit=discover.MAX_VERSIONS,
                      seconds=round(end - started, 1),
                      timing={name: round(marks[i + 1][1] - at, 2) for i, (name, at) in enumerate(marks[:-1])},
                      requests=self.requests - requests,
                      traffic={k: traffic[k] for k in ("throttled", "failed", "retries", "waited_seconds",
                                                       "peak_per_second")})
        put_together = {str(v["id"]) for v in result.get("excluded") or [] if "Put together" in str(v.get("excluded_reason"))}
        for a in albums:                        # a release whose tracks come in different formats
            if a.get("format_mixed") and str(a.get("id")) not in put_together:
                result["notes"].append(
                    f"{client.LABEL} serves the tracks of \u201c{core.describe(a)['label']}\u201d in different "
                    f"formats ({', '.join(a['format_mixed'])}). The lowest one is shown.")
        if traffic["throttled"]:
            result["notes"].append(
                f"{client.LABEL} slowed down {traffic['throttled']} request(s) during this comparison (HTTP 429). "
                f"They were repeated after a short wait"
                + (" - some searches may still be missing, so the list of editions can be incomplete."
                   if traffic["throttled"] > traffic["retries"] else "."))
        # what the service counts over the whole session, turned into the numbers of this comparison
        notes = client.diagnostics()
        for key, value in list(notes.items()):
            if isinstance(value, int) and not isinstance(value, bool) and isinstance(before.get(key), int) \
                    and key != "blocked_seconds":
                notes["session_" + key], notes[key] = value, value - before[key]
        self._report(body, result, seen, albums, dict(self.endpoints - endpoints), traffic, notes)
        self.history.add(history.record(result, body, service=client.NAME))
        return result

    def _refused_report(self, body, client, mark):
        """A comparison that ended because the service turned a request down: what was sent and
        how it was answered still goes to a file - that is exactly the case worth looking at."""
        if not self.report_dir:
            return
        try:
            path = Path(self.report_dir)
            path.mkdir(parents=True, exist_ok=True)
            data = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "version": __version__, "service": client.NAME,
                    "asked": {k: body.get(k) for k in ("artist", "artist_id", "title", "ids")},
                    "refused": client.refusal, "traffic": client.log.since(mark),
                    "service_notes": client.diagnostics()}
            name = time.strftime(f"_{client.NAME} refused %Y-%m-%d %H%M%S.json")
            (path / name).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _report(self, body, result, seen, albums, endpoints, traffic=None, notes=None):
        """Writes what was asked, offered and decided to a file - the basis for tuning the search.
        The loaded editions are included (reduced to what the analysis reads), so a comparison
        can be replayed from the file."""
        if not self.report_dir:
            return
        keep = ("id", "title", "version", "artist", "image", "maximum_bit_depth", "maximum_sampling_rate",
                "release_date_original", "release_date_stream", "label", "copyright", "release_type",
                "tracks_count", "upc", "media_count")
        tkeep = ("title", "version", "duration", "isrc", "audio_info", "media_number", "track_number")
        extra = ("url", "service", "loudest_gain", "format_mixed")     # only where a service sets them
        textra = ("id", "tidal", "source")
        slim = [dict({k: a.get(k) for k in keep}, **{k: a[k] for k in extra if a.get(k) and a.get("service")},
                     tracks={"total": (a.get("tracks") or {}).get("total"),
                             "items": [dict({k: t.get(k) for k in tkeep},
                                            **{k: t[k] for k in textra if t.get("tidal") and k in t})
                                       for t in ((a.get("tracks") or {}).get("items") or [])]})
                for a in albums]
        client = self.client
        try:
            name = re.sub(r"[^\w .-]+", "_", f"{body.get('artist')} - {body.get('title')}").strip()[:90]
            if client.NAME != "qobuz":          # the Qobuz report of the same album stays next to it
                name += f" [{client.NAME}]"
            path = Path(self.report_dir)
            path.mkdir(parents=True, exist_ok=True)
            data = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "version": __version__, "service": client.NAME,
                    "query": {k: body.get(k) for k in ("artist", "artist_id", "title", "ids")},
                    "requests": endpoints, "traffic": traffic,
                    "service_notes": client.diagnostics() if notes is None else notes,
                    "result": result, "offered": seen, "albums": slim}
            (path / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def job(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return None
        keys = ("id", "state", "stage", "done", "total", "result", "error", "kind", "current", "results", "message")
        return {k: job[k] for k in keys if k in job}

    # ----------------------------------------------------------- timeline

    def timeline(self):
        """Every album compared so far with its year and the dynamics of its best edition."""
        if not self.history_read:
            self.history_read = True
            if self.report_dir and not self.demo:
                history.import_reports(self.history, self.report_dir)
        return {"albums": self.history.albums(self.service)}

    # ------------------------------------------------------------ library

    def _favourites(self, refresh=False):
        if not self.client.LIBRARY:
            raise ValueError(f"Library checks are not available for {self.client.LABEL}.")
        if refresh or not self.favourites or time.time() - self.favourites[0] > 600:
            items, shape = library.favourites(self.client)
            self.favourites = (time.time(), [library.entry(it, self.client.album_url) for it in items], shape)
        return self.favourites[1]

    def library(self, refresh=False):
        """The favourite albums with the stored result of their check, if there is one."""
        entries = self._favourites(refresh)
        checked = self.store.checked()
        job = self.library_job
        client = self.client
        return {"albums": [dict(e, check=library.adjust(e, checked.get(e["id"]))) for e in entries],
                "limit": library.LIMIT, "min_gain": library.MIN_GAIN,
                "paused": max(self.store.paused(), client.blocked()),
                "rate": self.library_rate,
                # a service that reads loudness track by track: how fast, and how much per run
                "lazy": client.LAZY, "budget": client.LIBRARY_CALLS,
                "loudness_rate": getattr(client, "playback_rate", None) if client.LAZY else None,
                "cooldown_minutes": library.COOLDOWN // 60,
                "run": job["id"] if job and job["state"] == "running" else None}

    def library_check(self, body):
        ids = list(collections.OrderedDict.fromkeys(str(i) for i in (body.get("ids") or [])))
        if not ids:
            raise ValueError("Select at least one album.")
        if len(ids) > library.LIMIT:
            raise ValueError(f"At most {library.LIMIT} albums per run.")
        wait = max(self.store.paused(), self.client.blocked())
        if wait:
            raise ValueError(f"{self.client.LABEL} slowed requests down or turned one down. Library checks are "
                             f"paused for another {max(1, round(wait / 60))} minute(s).")
        known = {e["id"]: e for e in self._favourites()}
        entries = [known[i] for i in ids if i in known]
        if len(entries) != len(ids):
            raise ValueError("Some of the selected albums are no longer in your library. Refresh the list.")
        with self.lock:
            if self.library_job and self.library_job["state"] == "running":
                raise ValueError("A library check is already running.")
            job_id = secrets.token_urlsafe(8)
            job = {"id": job_id, "kind": "library", "state": "running", "stage": "search", "done": 0,
                   "total": len(entries), "current": entries[0]["label"], "results": [], "result": None,
                   "error": None, "message": None, "stop": False, "started": time.time()}
            self.jobs[job_id] = self.library_job = job
        self.pool.submit(self._run_library, job, entries)
        return {"job": job_id}

    def library_stop(self, body):
        job = self.library_job
        if job and job["state"] == "running":
            job["stop"] = True
        return {"ok": True}

    def library_forget(self, body):
        """Drops stored results, so the albums can be checked again."""
        self.store.forget(str(i) for i in (body.get("ids") or []))
        return {"ok": True}

    def _library_query(self, e):
        """The comparison the search would start for this album: same title and editions as
        when it is picked from the result list (anniversary editions join their album)."""
        for g in discover.search(self.client, f"{e['artist']} {e['title']}"):
            if e["id"] in g["ids"]:
                return {"artist": g["artist"], "artist_id": g["artist_id"], "title": g["title"], "ids": g["ids"]}
        return {"artist": e["artist"], "artist_id": e["artist_id"], "title": e["title"], "ids": [e["id"]]}

    def _run_library(self, job, entries):
        client, store, label = self.client, self.store, self.client.LABEL
        run_mark, rows = client.log.mark(), []
        client.strict, client.halted, client.rate = True, False, self.library_rate or None
        budget, read_before = client.LIBRARY_CALLS, getattr(client, "measured", 0)
        used = lambda: getattr(client, "measured", 0) - read_before
        stop = lambda: job["stop"] or self.closing
        state, message = "done", None
        try:
            for n, e in enumerate(entries):
                if job["stop"]:
                    state, message = "cancelled", f"Stopped after {n} of {len(entries)} albums."
                    break
                if budget and used() >= budget:
                    # the delicate requests are counted, not the albums: a run ends once enough are sent
                    message = (f"Checked {n} of {len(entries)} albums: the run ends after {budget} loudness requests "
                               f"({used()} were sent). Start another run for the others.")
                    break
                job.update(current=e["label"], done=n, stage="search")
                started, mark, read = time.time(), client.log.mark(), used()
                record = None
                try:
                    query = self._library_query(e)
                    result = self._compare(query, lambda stage, done, total: job.update(stage=stage), started,
                                           workers=library.WORKERS, stop=stop)
                    if not client.halted:
                        record = library.adjust(e, library.outcome(result, e["id"], label))
                        record.update(query=query, seconds=result["seconds"], requests=result["requests"])
                except Halted as err:           # stopped by the user, or the album alone needs too many requests
                    if stop():
                        state, message = "cancelled", f"Stopped after {n} of {len(entries)} albums."
                        break
                    record = {"kind": "other", "failed": True, "reason": err.message}
                except core.ApiError as err:
                    if err.status == 401:
                        job.update(state="error", error={"status": 401, "message": self.friendly(err)})
                        return
                    if client.blocked():        # a loudness request was turned down: the run ends here
                        state = "stopped"
                        message = (f"{label} turned a request down after {n} of {len(entries)} albums. The run was "
                                   f"stopped and library checks are paused for {max(1, round(client.blocked() / 60))} "
                                   "minutes.")
                        break
                    if not client.halted:
                        record = {"kind": "other", "failed": True, "reason": self.friendly(err)}
                if client.halted:               # throttled: this album is incomplete, the run ends here
                    state = "stopped"
                    message = (f"{label} slowed requests down after {n} of {len(entries)} albums. The run was "
                               f"stopped and library checks are paused for {library.COOLDOWN // 60} minutes.")
                    break
                record["when"] = time.strftime("%Y-%m-%d %H:%M")
                if not record.get("failed"):
                    store.put(e["id"], record)
                job["results"].append({"id": e["id"], "check": record})
                traffic = client.log.since(mark)
                rows.append({"id": e["id"], "label": e["label"], "artist": e["artist"], "kind": record["kind"],
                             "gain": record.get("gain"), "seconds": round(time.time() - started, 1),
                             "requests": traffic["requests"], "peak_per_second": traffic["peak_per_second"],
                             "loudness_calls": used() - read if client.LAZY else None})
                job["done"] = n + 1
                wait_until = time.time() + (self.library_pause if n + 1 < len(entries) else 0)
                while time.time() < wait_until and not job["stop"]:
                    time.sleep(0.05)
        except Exception as e:                  # show, do not hide: the window has no console
            job.update(state="error", error={"status": 500, "message": f"Unexpected error: {e!r}"})
            return
        finally:
            halted, client.strict, client.halted, client.rate = client.halted, False, False, None
            if halted:
                store.pause(time.time() + library.COOLDOWN)
            self._library_report(job, state, rows, client.log.since(run_mark), client, used())
        job.update(state=state, message=message)

    def _library_report(self, job, state, rows, traffic, client=None, loudness_calls=None):
        """One file per run: what was checked, how long it took and how the service answered."""
        client = client or self.client
        if not self.report_dir:
            return
        try:
            path = Path(self.report_dir)
            path.mkdir(parents=True, exist_ok=True)
            data = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "version": __version__, "state": state,
                    "service": client.NAME, "loudness_calls": loudness_calls if client.LAZY else None,
                    "service_notes": client.diagnostics() or None,
                    "selected": job["total"], "checked": len(rows),
                    "seconds": round(time.time() - job["started"], 1), "traffic": traffic, "albums": rows,
                    "favourites": {"count": len(self.favourites[1]), "shape": self.favourites[2]}
                    if self.favourites else None}
            mark = "" if client.NAME == "qobuz" else f" [{client.NAME}]"
            name = time.strftime(f"_library run %Y-%m-%d %H%M%S{mark}.json", time.localtime(job["started"]))
            (path / name).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def open(self, body):
        url = str(body.get("url") or "")
        if not url.startswith(tuple(self.client.OPEN_PREFIXES)):
            raise ValueError(f"not a {self.client.LABEL} link")
        webbrowser.open(url)
        return {"ok": True}


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "DynamicsWar"

        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' https: data:; "
                             "style-src 'self'; script-src 'self'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _local(self):
            """Only requests addressed to this machine (stops DNS-rebinding tricks)."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def _authorised(self, query):
            key = self.headers.get("X-Session") or (query.get("s") or [""])[0]
            return secrets.compare_digest(key, state.session)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            data = json.loads(self.rfile.read(min(length, 1 << 20)) or b"{}")
            return data if isinstance(data, dict) else {}

        def _api(self, fn, *args):
            try:
                self._send(200, fn(*args))
            except core.ApiError as e:
                self._send(401 if e.status == 401 else 502, {"error": state.friendly(e), "status": e.status})
            except (ValueError, KeyError) as e:
                self._send(400, {"error": str(e)})

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if not self._local():
                return self._send(403, {"error": "forbidden"})
            if url.path in STATIC:
                data = (WEB / STATIC[url.path]).read_bytes()
                if url.path == "/":
                    data = data.replace(b"{{SESSION}}", state.session.encode())
                ctype = mimetypes.guess_type(STATIC[url.path])[0] or "application/octet-stream"
                return self._send(200, data, f"{ctype}; charset=utf-8")
            if not url.path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not self._authorised(query):
                return self._send(403, {"error": "forbidden"})
            state.last_seen, state.bye_at = time.monotonic(), None
            if url.path == "/api/status":
                return self._api(state.status)
            if url.path == "/api/ping":
                return self._send(200, {"ok": True})
            if url.path == "/api/search":
                return self._api(state.search, (query.get("q") or [""])[0])
            if url.path == "/api/settings":
                return self._api(state.settings)
            if url.path == "/api/timeline":
                return self._api(state.timeline)
            if url.path == "/api/library":
                return self._api(state.library, (query.get("refresh") or [""])[0] == "1")
            if url.path.startswith("/api/job/"):
                job = state.job(url.path[len("/api/job/"):])
                return self._send(200, job) if job else self._send(404, {"error": "unknown job"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if not self._local() or not self._authorised(query):
                return self._send(403, {"error": "forbidden"})
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            if url.path == "/api/bye":          # window is closing (or reloading)
                state.bye_at = time.monotonic()
                return self._send(200, {"ok": True})
            if url.path == "/api/quit":         # a start of another version takes over
                state.quit = True
                return self._send(200, {"ok": True})
            state.last_seen, state.bye_at = time.monotonic(), None
            routes = {"/api/login": state.login, "/api/logout": state.logout,
                      "/api/compare": state.compare, "/api/compare/stop": state.compare_stop,
                      "/api/open": state.open, "/api/service": state.use_service,
                      "/api/library/check": state.library_check, "/api/library/stop": state.library_stop,
                      "/api/library/forget": state.library_forget}
            if url.path in routes:
                return self._api(routes[url.path], body)
            return self._send(404, {"error": "not found"})

    return Handler


class _Server(ThreadingHTTPServer):
    # On Windows a reused address would let two instances share a port. Elsewhere it only lets a
    # start bind the port right after another instance has ended (taking over after an update).
    allow_reuse_address = sys.platform != "win32"


def serve(state, port=0):
    """Starts the server in a background thread. Returns (server, base URL)."""
    server = _Server(("127.0.0.1", port), make_handler(state))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="dynamicswar-http", daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"
