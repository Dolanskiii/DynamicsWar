"""What has been compared so far, kept for the timeline: one entry per album with the year it
came out and the dynamics of its best edition. Filled by every comparison; the diagnostic
reports of earlier comparisons are read in once, so the timeline starts with what is there."""
import json
import threading
import time
from pathlib import Path

from . import discover, editions, library


def _year(v):
    y = str(v.get("year") or "")[:4]
    return int(y) if y.isdigit() else None


def record(result, query=None, when=None, service=None):
    """Timeline entry for the result of a comparison, or None when there is nothing to plot:
    no edition with a dynamics value, or a box set that was only compared with itself."""
    groups = result.get("groups") or []
    if not groups:
        return None
    group = groups[0]
    eds = (group.get("ranked") or []) + (group.get("unranked") or [])
    best = next((v for v in eds if v["id"] == group.get("best")), None)
    if not best or best.get("dynamics") is None:
        return None
    if len(eds) == 1 and ((best.get("total") or 0) >= library.BOX_TRACKS
                          or str(best.get("release_type") or "").lower() in library.BOX_TYPES):
        return None
    # editions without loudness data cannot be ranked, but they are editions: the original of
    # "Thriller" (1982) is one of them, and it says when the album came out
    silent = [v for v in result.get("excluded") or [] if "no loudness data" in str(v.get("excluded_reason"))]
    years = [y for y in map(_year, eds + silent) if y]
    values = [v["dynamics"] for v in eds if v.get("dynamics") is not None]
    if not years:
        return None
    query = {k: (query or {}).get(k) for k in ("artist", "artist_id", "title", "ids")}
    album = result.get("album") or {}
    title = query["title"] or album.get("title") or best["label"]
    artist = album.get("artist") or best.get("artist") or query["artist"] or "?"
    mark = best.get("assessment") or {}
    service = service or result.get("service") or "qobuz"
    # the same album on another service is an entry of its own
    return {"key": f"{discover._artist_key(artist)}|{discover.album_key(title)}"
                   + ("" if service == "qobuz" else f"|{service}"),
            "service": service, "artist": artist, "album": discover.display_title(title),
            "year": min(years),                 # the earliest year among the editions: when the album came out
            "cover": best.get("thumb") or album.get("cover") or "",
            "best": {"id": str(best["id"]), "label": best["label"], "format": best["format"], "bits": best["bits"],
                     "dynamics": best["dynamics"], "level": mark.get("level"), "verdict": mark.get("label")},
            "lowest": min(values), "editions": len(eds),
            "when": when or time.strftime("%Y-%m-%d %H:%M:%S"),
            "query": query if query["title"] else None}


class Store:
    """Timeline entries, kept between sessions (path=None: only in memory)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.items = self._read()

    def _read(self):
        if self.path:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("albums"), dict):
                    return data["albums"]
            except (OSError, ValueError):
                pass
        return {}

    def add(self, entry, save=True):
        """Keeps the newer entry per album. Returns True when the entry was taken."""
        if not entry:
            return False
        with self.lock:
            old = self.items.get(entry["key"])
            if old and str(old.get("when")) > str(entry.get("when")):
                return False
            self.items[entry["key"]] = entry
            if save:
                self._save()
        return True

    def _save(self):
        if not self.path:
            return
        # a second window (another service) may have added entries since this one read the file
        for key, entry in self._read().items():
            mine = self.items.get(key)
            if isinstance(entry, dict) and (mine is None or str(entry.get("when")) > str(mine.get("when"))):
                self.items[key] = entry
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"albums": self.items}, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def save(self):
        with self.lock:
            self._save()

    def albums(self, service=None):
        """One entry per album, oldest album first. Two entries that end at the same best edition
        are the same album compared under two names (an anniversary title, a second favourite):
        the newer comparison stays. service: only what was compared on this service (entries
        from before there was a second service are Qobuz ones)."""
        with self.lock:
            newest = {}
            for entry in sorted(self.items.values(), key=lambda e: str(e.get("when"))):
                where = entry.get("service") or "qobuz"
                if service and where != service:
                    continue
                newest[(where, entry["best"]["id"])] = entry
        return sorted(newest.values(), key=lambda e: (e["year"], e["artist"].lower(), e["album"].lower()))


def import_reports(store, folder):
    """Reads the diagnostic reports of earlier comparisons. Each one is replayed with the current
    rules, so results that an older version got wrong do not end up in the timeline."""
    taken = 0
    for path in sorted(Path(folder).glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(report, dict) or not isinstance(report.get("query"), dict):
                continue                                    # some other file, not a report of a comparison
            query = report["query"]
            service = report.get("service") if isinstance(report.get("service"), str) else "qobuz"
            result = (editions.compare(report["albums"], query.get("title"), service.capitalize())
                      if isinstance(report.get("albums"), list) and report["albums"] else report.get("result") or {})
            taken += store.add(record(result, query, report.get("when"), service), save=False)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    if taken:
        store.save()
    return taken
