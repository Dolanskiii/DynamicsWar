"""Search: group search hits into albums, and find every edition of one album.

The title only selects candidates. Whether a candidate really is the same album is
decided later from its track list (editions.py), so matching here can be generous.
"""
import math
import re
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor

from . import core

# Words that describe an edition, not the album itself.
EDITION_WORDS = frozenset("""
    deluxe super edition editions expanded remaster remastered remasters anniversary legacy
    special collector collectors bonus track tracks version limited definitive ultimate
    complete extended hd hires ultra platinum
""".split())
NUMBER = re.compile(r"^\d+(st|nd|rd|th)?$")
FORMAT_UNITS = frozenset(("khz", "hz", "bit"))          # 'Album - 192 kHz', 'Album 24 Bit'
FORMAT_SUFFIX = re.compile(r"\s*[-\u2013\u2014(\[]\s*\d+(?:[.,]\d+)?\s*(?:k?hz|bit)\b.*$", re.I)
MAX_VERSIONS = 40
WORKERS = 6


def _strip_edition(tokens):
    """Drops trailing edition words: 'rumours 2004 remaster' -> 'rumours'. A bare number
    stays ('chicago 17'), it is only dropped after an edition word ('40th anniversary')."""
    tokens, stripped = list(tokens), False
    while len(tokens) > 1:
        last = tokens[-1]
        if last in EDITION_WORDS or (last in FORMAT_UNITS and NUMBER.match(tokens[-2])):
            stripped = True
        elif not (stripped and NUMBER.match(last)):
            break
        tokens.pop()
    return tokens


def album_key(title, version=None):
    """Normalised album name without edition additions - the identity used for grouping."""
    base = core.split_title(title, version)[0]
    return " ".join(_strip_edition(base.split()))


def edition_text(title, version=None):
    """Everything in a title that describes the edition (brackets, version field, trailing words)."""
    base, tags = core.split_title(title, version)
    tokens = base.split()
    tail = tokens[len(_strip_edition(tokens)):]
    return " | ".join(tags + ([" ".join(tail)] if tail else []))


def title_matches(title, version, want_key):
    """Same album name, optionally followed by edition words or numbers ('Thriller 25')."""
    key = album_key(title, version)
    if key == want_key:
        return True
    if want_key and key.startswith(want_key + " "):
        return all(NUMBER.match(t) or t in EDITION_WORDS for t in key[len(want_key) + 1:].split())
    return False


def display_title(title):
    """Title without edition brackets: 'Rumours (Deluxe Edition)' -> 'Rumours'.
    Brackets that belong to the name stay: '(What's the Story) Morning Glory?'."""
    title = title or ""
    clean = FORMAT_SUFFIX.sub("", title) or title
    clean = core.PAREN.sub(lambda m: " " if core.TAGWORD.search(m.group(1)) else m.group(0), clean)
    m = core.DASH_SUFFIX.search(clean)
    if m and core.TAGWORD.search(m.group(1)):
        clean = clean[:m.start()]
    return " ".join(clean.split()) or title


def _artist_key(name):
    key = core.norm(name)
    return key[4:] if key.startswith("the ") else key


def cover(item, size="large"):
    image = item.get("image") or {}
    return image.get(size) or image.get("large") or image.get("small") or image.get("thumbnail") or ""


def _year(item):
    y = str(item.get("release_date_original") or "")[:4]
    return int(y) if y.isdigit() else None


def _merge_numbered(groups):
    """'Thriller 25' and 'Thriller 40' belong to 'Thriller': a number after the album name is an
    anniversary when it matches the years since the album came out (or both carry the album's
    year). 'Chicago 17' does not fit that and stays an album of its own."""
    for key in list(groups):
        artist_key, title_key = key
        tokens = title_key.split()
        cut = len(tokens)
        while cut > 1 and NUMBER.match(tokens[cut - 1]):
            cut -= 1
        base = (artist_key, " ".join(tokens[:cut]))
        if cut == len(tokens) or base not in groups:
            continue
        base_years = [y for y in map(_year, groups[base]) if y]
        own_years = [y for y in map(_year, groups[key]) if y]
        if not base_years or not own_years:
            continue
        number = int(re.match(r"\d+", tokens[-1]).group())
        gap = min(own_years) - min(base_years)
        if gap == 0 or abs(gap - number) <= 1 or number == min(own_years):
            groups[base] += groups.pop(key)


def group_albums(items):
    """One entry per album: all editions of the same album by the same artist together."""
    groups = OrderedDict()
    for it in items:
        key = (_artist_key(core.artist_name(it)), album_key(it.get("title"), it.get("version")))
        if it.get("id") and key[1]:
            groups.setdefault(key, []).append(it)
    _merge_numbered(groups)
    out = []
    for (artist_key, title_key), members in groups.items():
        # the plainest title represents the album
        rep = min(members, key=lambda it: (len(core.split_title(it.get("title"), it.get("version"))[1]),
                                           len(it.get("title") or "")))
        ids = list(OrderedDict.fromkeys(str(it["id"]) for it in members))
        years = [int(y) for y in (str(it.get("release_date_original") or "")[:4] for it in members)
                 if y.isdigit()]
        best = max(members, key=lambda it: (it.get("maximum_bit_depth") or 0,
                                            it.get("maximum_sampling_rate") or 0))
        bits, rate = best.get("maximum_bit_depth"), best.get("maximum_sampling_rate")
        out.append({
            "key": f"{artist_key}|{title_key}",
            "title": display_title(rep.get("title")),
            "artist": core.artist_name(rep) or "Unknown artist",
            "artist_id": (rep.get("artist") or {}).get("id"),
            "ids": ids,
            "count": len(ids),
            "cover": cover(rep),
            "year": min(years) if years else None,
            # a service that only says "hi-res" in its lists (Tidal) gets no invented sampling rate
            "format": f"{bits}/{rate:g}" if bits and rate else ("24-bit" if (bits or 0) > 16 else ""),
            "tracks": max((it.get("tracks_count") or 0) for it in members),
        })
    return out


def search(client, query):
    """Album search for the result grid."""
    return group_albums(client.find_albums(query, 100))


# ------------------------------------------------------------ all editions

def _safe(fn, *args, **kw):
    """Runs a lookup; a failing one counts as 'nothing found' - except an expired login."""
    try:
        return fn(*args, **kw)
    except core.ApiError as e:
        if e.status == 401:
            raise
        return None


def discover(client, artist, title, artist_id=None, seed_ids=(), progress=None,
             max_versions=MAX_VERSIONS, workers=WORKERS, debug=None):
    """Finds the editions of an album through album search, discography and track search.
    Returns (album JSONs, how many each source contributed, True if more were found than loaded).
    debug: optional list that receives every release Qobuz offered and whether it was taken."""
    say = progress or (lambda stage, done, total: None)
    want = album_key(title)
    source, order, albums = {}, [], OrderedDict()
    noted = {}

    def note(items, src, taken):
        """Remembers what was offered, for tuning the search."""
        if debug is None:
            return
        for it in items:
            one, ok = str(it.get("id")), bool(taken(it))
            if one in noted:
                if ok and not noted[one]["taken"]:      # rejected by name, accepted through its songs
                    noted[one].update(source=src, taken=True)
                continue
            if len(debug) >= 800:
                continue
            noted[one] = {"id": one, "title": it.get("title"), "version": it.get("version"),
                          "artist": core.artist_name(it), "source": src, "taken": ok,
                          "tracks": it.get("tracks_count"),
                          "format": f"{it.get('maximum_bit_depth')}/{it.get('maximum_sampling_rate')}"}
            debug.append(noted[one])

    def artist_ok(it, allow_unknown=False):
        name = core.artist_name(it)
        return (core.same_artist(name, artist) or (allow_unknown and not core.norm(name))
                or bool(artist_id) and str((it.get("artist") or {}).get("id")) == str(artist_id))

    def fits(it, allow_unknown=False):
        return artist_ok(it, allow_unknown) and title_matches(it.get("title"), it.get("version"), want)

    def add(ids, src):
        for one in ids:
            one = str(one)
            if one not in source:
                source[one] = src
                order.append(one)

    def load(pool):
        todo = [i for i in order if i not in albums][:max(0, max_versions - len(albums))]
        base = len(albums)
        for n, (one, data) in enumerate(zip(todo, pool.map(lambda i: _safe(client.album, i), todo)), 1):
            say("load", base + n, base + len(todo))
            if data and ((data.get("tracks") or {}).get("items")):
                albums[one] = data
            else:
                albums[one] = None          # not available (region, withdrawn): remember, skip
        return [i for i in todo if albums.get(i)]

    def track_round(pool, batch, with_titles):
        described = [core.describe(albums[i]) for i in batch]
        titles = []
        if with_titles:
            count = Counter(k for d in described for k, t in d["tracks"].items() if not t["variant"])
            titles = [k for k, _ in sorted(count.items(), key=lambda kv: (-kv[1], -len(kv[0])))][:10]
        isrcs = list(OrderedDict.fromkeys(
            t["isrc"] for d in described for t in list(d["tracks"].values())[:2] if t.get("isrc")))[:8]
        queries = [("title", f"{artist} {t}") for t in titles] + [("isrc", i) for i in isrcs]
        queries = [q for q in queries if q[1] not in asked]
        asked.update(q[1] for q in queries)
        hits, cand = Counter(), OrderedDict()
        results = pool.map(lambda q: _safe(client.albums_with_track, q[1]), queries)
        for n, ((kind, _), items) in enumerate(zip(queries, results), 1):
            say("tracks", n, len(queries))
            seen = set()
            for it in items or []:
                one = str(it.get("id"))
                if one in source:
                    continue
                cand.setdefault(one, it)
                if kind == "title" and one not in seen and artist_ok(it):
                    seen.add(one)
                    hits[one] += 1
        # Same album under another name (box sets, retitled reissues): most of its songs are on it.
        need = max(3, math.ceil(0.6 * len(titles)))
        taken = lambda it: fits(it) or bool(titles and hits[str(it.get("id"))] >= need)
        note([it for it in cand.values() if artist_ok(it)], "tracks", taken)
        add((i for i, it in cand.items() if taken(it)), "tracks")

    asked = set()
    add(seed_ids, "search")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        say("search", 0, 0)
        f_hits = pool.submit(client.find_albums, f"{artist} {title}", 200)
        # the editions already known from the result list load while the search is running
        warm = [pool.submit(_safe, client.album, i) for i in order[:max_versions]]
        disco = client.releases(pool, artist, artist_id)
        hits_ = f_hits.result()
        for f in warm:
            f.result()
        note(hits_, "search", fits)
        note(disco, "discography", lambda it: fits(it, allow_unknown=True))
        add((it["id"] for it in hits_ if it.get("id") and fits(it)), "search")
        add((it["id"] for it in disco if it.get("id") and fits(it, allow_unknown=True)), "discography")
        batch = load(pool)
        for with_titles in (True, False):   # 2nd round: ISRCs of newly found editions lead to more
            if not batch:
                break
            track_round(pool, batch, with_titles)
            batch = load(pool)

    found = [a for a in albums.values() if a]
    stats = Counter(source[i] for i, a in albums.items() if a)
    return found, {k: stats.get(k, 0) for k in ("search", "discography", "tracks")}, len(order) > len(albums)
