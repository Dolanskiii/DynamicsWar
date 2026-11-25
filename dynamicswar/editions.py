"""Turns the editions of one album into the result the GUI shows.

1. Decide what each release is: an edition of the album (standard or expanded: deluxe,
   anniversary, box set), or something that is found but not ranked (live, mono, a
   different mix, another track list).
2. Rank all editions together with the engine (core.analyse), on the album's own tracks.
3. Say in plain English why each edition sits where it does.
"""
import math
import re
from collections import Counter

from . import core
from .discover import EDITION_WORDS, NUMBER, album_key, cover, edition_text

KIND_RULES = (
    ("live", re.compile(r"\b(live|concert|unplugged|bbc)\b")),
    ("mono", re.compile(r"\bmono\b")),
    ("mix", re.compile(r"\b(mix|mixes|remix|remixed|remixes|atmos|surround|spatial|binaural|"
                       r"quad|quadio|quadraphonic|quadrophonic|5 1|drumless|stems|slowed|nightcore|8d)\b")),
    ("other", re.compile(r"\b(demo|demos|instrumental|instrumentals|karaoke|acoustic|a cappellas?|"
                         r"acapellas?|orchestral|symphonic|commentary|rehearsal|rehearsals|outtake|outtakes|session|"
                         r"sessions|tribute|interview)\b")),
)
KIND_REASON = {
    "live": "Live recording",
    "mono": "Mono version",
    "mix": "Different mix (remix, new stereo or surround mix)",
    "other": "Alternate material (demos, instrumental, acoustic or similar)",
}
# Words in a track's brackets that say nothing about the take: edition and remaster wording.
NEUTRAL_WORDS = EDITION_WORDS | frozenset("stereo album lp original explicit clean digital master".split())
RERECORDED = re.compile(r"\b(taylor s version|re recorded|rerecorded|re recording|rerecording|"
                        r"new recording|newly recorded)\b")
PARTIAL_MIN_TRACKS = 2  # a release with at least this many album tracks in a better master is worth a hint
PARTIAL_MIN_GAIN = 1.0  # ... if those are on average this many dB more dynamic than the best edition
EXPANDED_EXTRA = 3      # this many more tracks than the album itself = expanded edition
KIND_SHARE = 0.6        # share of live/mix takes that makes a release a live/mix release
# Measured on Qobuz data: the same recording differs by 0-2 s between editions, live and
# alternate takes by far more. Within this tolerance two tracks count as the same recording.
DURATION_TOLERANCE = 3
# Remasters are editions and are ranked (in 75 reports every release that calls itself a remaster
# is within this tolerance). A wider tolerance for them was tried on 25.11.2026 and taken out the
# same evening: its only case, "Aerosmith (2024 Remastered)" on Tidal with three tracks 4-6 s
# longer, turned out to be no remaster but a release put together from other releases.
SINGLE_TYPES = ("single", "ep", "epmini")


def text_kind(text):
    text = core.norm(text)
    for kind, rx in KIND_RULES:
        if rx.search(text):
            return kind
    return None


def special_words(tags):
    """What remains of a track's bracket texts once edition and remaster wording is removed:
    ['drumless edition'] -> ['drumless'], ['2012 remaster'] -> []."""
    out = []
    for tag in tags:
        left = " ".join(w for w in tag.split() if w not in NEUTRAL_WORDS and not NUMBER.match(w))
        if left:
            out.append(left)
    return out


def track_marks(album_json):
    """Per base title the special words of the chosen take (same choice as core.pick_tracks)."""
    marks, variant = {}, {}
    for t in ((album_json.get("tracks") or {}).get("items") or []):
        base, tags = core.split_title(t.get("title"), t.get("version"))
        is_var = core.is_variant(tags)
        if base not in marks or (variant[base] and not is_var):
            marks[base], variant[base] = special_words(tags), is_var
    return marks


def common_mark(v, keys):
    """A special word that (nearly) every one of these tracks carries, e.g. 'drumless'."""
    keys = list(keys)
    if not keys:
        return None
    count = Counter(m for k in keys for m in set(v["track_marks"].get(k, ())))
    if not count:
        return None
    mark, n = count.most_common(1)[0]
    return mark if n >= 0.8 * len(keys) else None


def track_kinds(album_json):
    """Per base title the kind of the chosen take (None = regular), mirroring core.pick_tracks."""
    kinds, variant = {}, {}
    for t in ((album_json.get("tracks") or {}).get("items") or []):
        base, tags = core.split_title(t.get("title"), t.get("version"))
        is_var = core.is_variant(tags)
        if base not in kinds or (variant[base] and not is_var):
            kinds[base] = (text_kind(" | ".join(tags)) or "other") if is_var else None
            variant[base] = is_var
    return kinds


def describe(album_json):
    v = core.describe(album_json)
    text = edition_text(album_json.get("title"), album_json.get("version"))
    v.update({
        "key": album_key(album_json.get("title"), album_json.get("version")),
        "full_title": f"{album_json.get('title') or ''} {album_json.get('version') or ''}",
        "exact": True,
        "foreign": False,
        "cover": cover(album_json, "large"),
        "thumb": cover(album_json, "small"),
        "artist_id": (album_json.get("artist") or {}).get("id"),
        "url": album_json.get("url"),                   # set by the service the album was loaded from
        "release_type": str(album_json.get("release_type") or "").lower(),
        "kind": text_kind(text),
        "rerecorded": bool(RERECORDED.search(core.norm(text))),
        "album_peak": max((p for p in ((t.get("audio_info") or {}).get("replaygain_track_peak")
                                       for t in ((album_json.get("tracks") or {}).get("items") or [])) if p),
                          default=None),
        "track_kinds": track_kinds(album_json),
        "track_marks": track_marks(album_json),
        "group": None,
        "excluded_reason": None,
    })
    return v


def choose_anchor(versions):
    """The release that stands for the album itself: among the releases carrying the album's
    name, the smallest regular one whose tracks most others contain as well. Deluxe editions
    contain the album, so the album wins even when deluxe editions are in the majority."""
    plain = [v for v in versions if not v["kind"]] or list(versions)
    plain = [v for v in plain if v["exact"]] or plain
    cands = [v for v in plain if v["release_type"] not in SINGLE_TYPES and len(v["tracks"]) >= 4] or plain

    def coverage(a):
        others = [v for v in cands if v is not a]
        if not others:
            return 1.0
        keys = set(a["tracks"])
        return sum(1 for v in others if len(keys & set(v["tracks"])) >= 0.8 * len(keys)) / len(others)

    scored = [(coverage(a), a) for a in cands]
    good = [(c, a) for c, a in scored if c >= 0.5]
    if good:
        return min(good, key=lambda ca: (len(ca[1]["tracks"]), ca[1]["total"], -ca[0]))[1]
    return min(scored, key=lambda ca: (-ca[0], len(ca[1]["tracks"]), ca[1]["total"]))[1]


def _close(a, t, tolerance=DURATION_TOLERANCE):
    da, dt = a.get("duration"), t.get("duration")
    return not da or not dt or abs(da - dt) <= tolerance


def same_recording(a, t, tolerance=DURATION_TOLERANCE):
    """Same ISRC, or the same kind of take with (nearly) the same length. A take marked as
    live, demo or alternate is another recording even when it happens to run as long."""
    if a.get("isrc") and a["isrc"] == t.get("isrc"):
        return True
    if bool(a.get("variant")) != bool(t.get("variant")):
        return False
    return _close(a, t, tolerance)


def _contained(x, y):
    """One title's words all occur in the other: 'war pigs' / 'war pigs luke s wall'."""
    sx, sy = set(x.split()), set(y.split())
    small, big = (sx, sy) if len(sx) <= len(sy) else (sy, sx)
    return bool(small) and small <= big


def match_album(anchor, v):
    """Finds the album's tracks on release v: {album key: (v's key, v's track)}. Same title
    first; otherwise the same ISRC, or a title contained in the other with matching length
    (track names differ between remasters: 'War Pigs' / 'War Pigs / Luke's Wall')."""
    ref, mine = anchor["tracks"], v["tracks"]
    out = {k: (k, mine[k]) for k in ref if k in mine}
    left = [k for k in mine if k not in out]
    for k, a in ref.items():
        if k in out:
            continue
        for kv in left:
            t = mine[kv]
            if (a.get("isrc") and a["isrc"] == t.get("isrc")) or (_contained(k, kv) and _close(a, t)
                                                                  and a.get("duration") and t.get("duration")):
                out[k] = (kv, t)
                left.remove(kv)
                break
    return out


def classify(versions, want_key=None):
    """Sets group ('standard' / 'expanded' / 'excluded') and the reason on every release.

    A release counts as an edition of the album when it contains (nearly) all of the album's
    tracks as the same recordings. That admits deluxe editions and box sets and keeps out
    compilations, live albums and bootlegs - whatever they are called."""
    for v in versions:
        v["exact"] = want_key is None or v["key"] == want_key
        # 'foreign': a release under another name (box set, compilation); 'Album 25' is not
        v["foreign"] = not (v["exact"] or v["key"].startswith(want_key + " "))
        if not v["exact"] and not v["kind"]:        # another name: the whole title may say 'Live at ...'
            v["kind"] = text_kind(v["full_title"])
    anchor = choose_anchor(versions)
    album = anchor["tracks"]
    size = len(album)
    need = max(1, size - max(1 if size >= 7 else 0, int(0.15 * size)))
    own_mark = common_mark(anchor, album)       # e.g. a soundtrack: every track 'from the motion picture'
    for v in versions:
        matched = match_album(anchor, v)
        same = {k: t for k, (kv, t) in matched.items() if same_recording(album[k], t)}
        v["album_tracks"], v["album_same"], v["album_size"] = len(matched), len(same), size
        v["extra"] = max(0, v["total"] - anchor["total"])
        v["on_album"] = same
        if not v["kind"] and matched:
            # most album tracks only as live/mix/mono takes -> it is that kind of release
            top = Counter(v["track_kinds"].get(kv) for kv, _ in matched.values()).most_common(1)[0]
            if top[0] and top[1] >= KIND_SHARE * len(matched) and len(matched) >= need:
                v["kind"] = top[0]
        # Every track marked the same way and the album itself is not: another version of the
        # album, even when the lengths match (e.g. 'Drumless Edition'). Named re-recordings stay in.
        mark = None if (v["kind"] or v["rerecorded"]) else common_mark(v, [kv for kv, _ in matched.values()])
        if mark:
            # no difference if the tracks only repeat the album's own title ('Original Soundtrack'),
            # or if they carry the album's ISRCs - then it is the same recording whatever it says
            in_title = set(mark.split()) <= set(core.norm(anchor["full_title"]).split())
            same_code = sum(1 for k, (kv, t) in matched.items()
                            if t.get("isrc") and t["isrc"] == album[k].get("isrc"))
            if in_title or same_code >= need:
                mark = None
        if v["kind"]:
            v["group"], v["excluded_reason"] = "excluded", KIND_REASON[v["kind"]]
        elif mark and mark != own_mark and len(matched) >= need:
            v["kind"], v["group"] = "version", "excluded"
            v["excluded_reason"] = f"Different version of the album (every track is marked \u201c{mark.title()}\u201d)"
        elif len(same) >= need:
            v["group"] = "expanded" if v["extra"] >= EXPANDED_EXTRA else "standard"
        elif len(matched) >= need and v["rerecorded"]:
            v["on_album"] = {k: t for k, (kv, t) in matched.items()}
            v["group"] = "expanded" if v["extra"] >= EXPANDED_EXTRA else "standard"
        elif len(matched) >= need:
            v["group"] = "excluded"
            v["excluded_reason"] = ("Different recordings of the album's songs, live or alternate takes "
                                    f"(only {len(same)} of {size} tracks match in length)")
        else:
            single = v["release_type"] in SINGLE_TYPES or (v["total"] <= 3 < size)
            v["group"] = "excluded"
            v["excluded_reason"] = (f"Single or EP (has {len(same)} of the album's {size} tracks)" if single
                                    else f"Not this album ({len(same)} of its {size} tracks are on it as the "
                                         "same recording)")
    return anchor


def needed(album_jsons, title=None):
    """Which tracks the ranking will read: for every release that counts as an edition of the
    album, its takes of the album's own tracks. Returns [(album_json, [track ids])].

    Deciding what is an edition needs titles, lengths and ISRCs only. A service whose loudness
    values cost one request per track (Tidal) reads them after this, and only for these tracks -
    not for bonus material, and not for compilations or live albums that merely were candidates."""
    pairs = [(a, describe(a)) for a in album_jsons]
    pairs = [(a, v) for a, v in pairs if v["tracks"]]
    if not pairs:
        return []
    classify([v for _, v in pairs], album_key(title) if title else None)
    return [(a, [t["id"] for t in v["on_album"].values() if t.get("id") is not None])
            for a, v in pairs if v["group"] != "excluded"]


# ------------------------------------------------------------- wording

def fmt(x, digits=1):
    return f"{x:.{digits}f}"


# Six steps for the meter, 1 = worst, 6 = best. The first class borders (7, 10, 13 dB) stay;
# the crowded range below 10 dB is split finer, because most releases sit there.
STEPS = (6.0, 7.0, 8.5, 10.0, 13.0)


def assess(v):
    """Short verdict on one edition: (label, level 1-6)."""
    plr, gain = v.get("plr_mean"), v.get("gain_mean")
    if plr is not None:
        d = round(plr, 1)
        level = 1 + sum(d >= step for step in STEPS)
        return ("Very dynamic" if d >= 13 else "Dynamic" if d >= 10
                else "Moderately compressed" if d >= 7 else "Heavily compressed"), level
    if gain is None:
        return None
    roon = round(gain + core.ROON_OFFSET, 1)        # without peak levels: judged by loudness
    level = 1 + sum(roon > step for step in (-7.0, -5.5, -4.0, -2.5)) + (roon >= -1)
    return ("Quiet master" if roon >= -1 else "Fairly dynamic" if roon > -4
            else "Loud master" if roon > -7 else "Very loud master"), level


def lufs(gain):
    return None if gain is None else -18 - gain


def louder_text(v, ref):
    """', mastered about 2.1 dB louder' relative to ref, or '' if within half a dB."""
    if v.get("gain_mean") is None or ref.get("gain_mean") is None:
        return ""
    diff = ref["gain_mean"] - v["gain_mean"]        # positive: v is louder
    if abs(diff) < 0.5:
        return ""
    return f", mastered about {fmt(abs(diff))} dB {'louder' if diff > 0 else 'quieter'}"


def res(v):
    return (v["bits"], v["rate"])


def edge(first, second):
    """Why `first` stands above `second` when both are the same master."""
    if res(first) > res(second):
        return "resolution"
    if first.get("foreign") != second.get("foreign"):
        return "name"
    if first["total"] > second["total"]:
        return "tracks"
    return None


def reasons(v, ranked):
    by_dyn = v["metric"] == "plr"
    best, out = ranked[0], []
    siblings = [o for o in ranked if o is not v and (o["same_as"] == v["rank"])]
    if v["rank"] == 1:
        others = [o for o in ranked[1:] if o["same_as"] != 1]
        if others:
            nxt = others[0]
            if abs(nxt["delta"]) < 0.5:
                out.append(f"Practically tied with #{nxt['rank']} ({fmt(abs(nxt['delta']), 2)} dB apart) "
                           "- let your ears decide.")
            elif by_dyn:
                out.append(f"Most dynamic master: {fmt(v['plr_mean'])} dB between peaks and loudness, "
                           f"{fmt(abs(nxt['delta']))} dB more than #{nxt['rank']}.")
            else:
                out.append(f"Quietest master, {fmt(abs(nxt['delta']))} dB below #{nxt['rank']} "
                           "- usually the most dynamic.")
        elif len(ranked) > 1:
            out.append("All editions use the same master.")
    elif v["same_as"]:
        ref = next(o for o in ranked if o["rank"] == v["same_as"])
        why = edge(ref, v)
        tail = {"resolution": "in lower resolution",
                "name": "in the same resolution, as part of another release",
                "tracks": f"in the same resolution with {ref['total'] - v['total']} fewer track(s)",
                None: "in the same resolution"}[why]
        if abs(v["offset"]) <= 0.05:
            out.append(f"Same master as #{ref['rank']} (identical loudness on every track), {tail}.")
        else:
            out.append(f"Same master as #{ref['rank']}, evenly {fmt(abs(v['offset']), 2)} dB "
                       f"{'louder' if v['offset'] < 0 else 'quieter'} on every track, {tail} "
                       "- not a different mastering.")
    else:
        if by_dyn:
            out.append(f"{fmt(abs(v['delta']))} dB less dynamic than #{best['rank']}"
                       f"{louder_text(v, best)}.")
        else:
            out.append(f"{fmt(abs(v['delta']))} dB louder than #{best['rank']}.")
    if siblings:                                # say why this one leads its own master
        nxt = min(siblings, key=lambda o: o["rank"])
        why = edge(v, nxt)
        if why == "resolution":
            out.append(f"Highest resolution of this master (ahead of #{nxt['rank']}).")
        elif why == "tracks":
            out.append(f"Same master and resolution as #{nxt['rank']}, but {v['total'] - nxt['total']} "
                       "more track(s).")
        elif why == "name":
            out.append(f"Same master and resolution as #{nxt['rank']}, which is part of another release.")
    if v.get("variants"):
        out.append(f"{v['variants']} track(s) only as a mix, live or demo take here "
                   "- comparison is limited.")
    if v.get("rerecorded"):
        out.append("Re-recording: a new performance, not a remaster of the original tapes.")
    return out


def name(v):
    return f"{v['label']} [{v['format']}]"


def verdict(ranked):
    best = ranked[0]
    others = [o for o in ranked[1:] if o["same_as"] != best["rank"]]
    if not others:
        if len(ranked) == 1:
            return f"{name(best)} is the only edition with comparable data."
        why = edge(best, ranked[1])
        pick = {"resolution": f"Take the highest resolution: {name(best)}.",
                "tracks": f"In the same resolution, {name(best)} has the most tracks.",
                "name": f"Take the album itself: {name(best)}.",
                None: f"They are equal in resolution and content - {name(best)} is as good as any."}[why]
        return f"All {len(ranked)} editions use the same master. {pick}"
    nxt = others[0]
    if abs(nxt["delta"]) < 0.5:
        return (f"{name(best)} and #{nxt['rank']} are practically tied "
                f"({fmt(abs(nxt['delta']), 2)} dB apart). Let your ears decide.")
    what = "has the widest dynamics" if best["metric"] == "plr" else "is the quietest master"
    return f"{name(best)} {what}, {fmt(abs(nxt['delta']))} dB ahead of #{nxt['rank']}."


# ------------------------------------------------------------- result

def rnd(x, digits=2):
    return None if x is None else round(x, digits) + 0.0


def roon_level(v):
    """What Roon's volume leveling (track mode, target -14 LUFS) shows on average for the compared
    tracks. Roon never turns a track up into clipping: a positive adjustment ends at the track's
    peak. Qobuz gives the sample peak, Roon uses the true peak, so positive values are an upper bound."""
    values = []
    for t in v.get("used", {}).values():
        if t.get("gain") is None:
            continue
        level = t["gain"] + core.ROON_OFFSET
        if level > 0 and t.get("peak"):
            level = min(level, max(0.0, -20 * math.log10(t["peak"])))
        values.append(level)
    return sum(values) / len(values) if values else None


def roon_album_level(v):
    """What Roon shows in album mode, i.e. when the album plays through: one adjustment for all
    tracks, set by the loudest track of the edition (bonus tracks included). As in track mode, a
    positive adjustment ends at the peak - here the highest peak of the edition."""
    if v.get("album_gain") is None:
        return None
    level = v["album_gain"] + core.ROON_OFFSET
    if level > 0 and v.get("album_peak"):
        level = min(level, max(0.0, -20 * math.log10(v["album_peak"])))
    return level


def export(v):
    gain, peak = v.get("gain_mean"), v.get("peak_max")
    verdict_ = assess(v)
    return {
        "id": v["id"], "label": v["label"], "artist": v["artist"], "format": v["format"],
        "bits": v["bits"], "rate": v["rate"], "year": v["year"], "stream": v["stream"],
        "company": v["company"], "copyright": v["copyright"],
        "cover": v["cover"], "thumb": v["thumb"],
        "url": v.get("url") or f"https://play.qobuz.com/album/{v['id']}",
        "total": v["total"], "extra": v.get("extra", 0),
        "group": v["group"], "kind": v["kind"], "rerecorded": v["rerecorded"], "foreign": v["foreign"],
        "release_type": v.get("release_type") or "",
        "excluded_reason": v["excluded_reason"],
        "rank": v.get("rank"), "delta": rnd(v.get("delta")), "same_as": v.get("same_as"),
        "compared": v.get("n", 0), "metric": v.get("metric"),
        "dynamics": rnd(v.get("plr_mean")),
        "loudness": rnd(lufs(gain)),
        "peak_db": rnd(20 * math.log10(peak)) if peak and peak > 0 else None,
        "roon_track": rnd(roon_level(v), 1),
        "roon_album": rnd(roon_album_level(v), 1),
        "assessment": {"label": verdict_[0], "level": verdict_[1]} if verdict_ else None,
        "reasons": v.get("reasons", []),
    }


def track_table(ranked, shared):
    rows, order = [], {}
    for v in ranked:                        # album order instead of alphabetical
        for pos, k in enumerate(v["tracks"]):
            order.setdefault(k, pos)
    for k in sorted(shared, key=lambda k: order.get(k, 9999)):
        title = next((v["used"][k]["title"] for v in ranked if k in v["used"]), k)
        cells = {}
        for v in ranked:
            t = v["used"].get(k)
            if t:
                cells[v["id"]] = {"dynamics": rnd(t["plr"], 1), "loudness": rnd(lufs(t["gain"]), 1),
                                  "variant": t["variant"]}
        rows.append({"title": title, "values": cells})
    return rows


def better_tracks(excluded, members, best, album):
    """Releases that are no edition of the album but hold some of its tracks as the very same
    recording (same ISRC) in a clearly more dynamic master - e.g. an old box set. They stay
    out of the ranking, because the album cannot be heard complete in that master."""
    if not best:
        return []
    codes = {}                              # album track -> ISRCs it carries on any edition
    for v in members:
        for k, t in v["tracks"].items():
            if t.get("isrc"):
                codes.setdefault(k, set()).add(t["isrc"])
    out = []
    for v in excluded:
        if v["kind"]:
            continue
        rows = []
        for k, a in album.items():
            ref = best["tracks"].get(k)
            hit = next((t for t in v["tracks"].values() if t.get("isrc") in codes.get(k, ())), None)
            if hit and ref and hit["plr"] is not None and ref["plr"] is not None:
                rows.append({"title": a["title"], "here": rnd(hit["plr"], 1), "best": rnd(ref["plr"], 1),
                             "gain": rnd(hit["plr"] - ref["plr"], 1)})
        if len(rows) < PARTIAL_MIN_TRACKS:
            continue
        gain = sum(r["here"] - r["best"] for r in rows) / len(rows)
        if gain < PARTIAL_MIN_GAIN:
            continue
        item = export(v)
        item.update(tracks=rows, count=len(rows), album_size=len(album), gain=rnd(gain, 1),
                    dynamics=rnd(sum(r["here"] for r in rows) / len(rows), 1),
                    summary=(f"{len(rows)} of the album's {len(album)} tracks as the same recording, on average "
                             f"{fmt(gain)} dB more dynamic than on {name(best)}."))
        out.append(item)
    return sorted(out, key=lambda x: -x["gain"])[:5]


def compare(album_jsons, title=None, service="Qobuz"):
    """Full result for the GUI from the album/get answers of all editions. `title` is the album
    the user picked; releases carrying that name define the album's track list.

    One ranking for all editions of the album - standard, deluxe, anniversary, box set -
    compared on the album's own tracks only, so bonus material does not shift the result."""
    versions = [describe(a) for a in album_jsons]
    versions = [v for v in versions if v["tracks"]]
    if not versions:
        return {"groups": [], "excluded": [], "found": 0, "ranked": 0, "album": None, "notes": [],
                "partials": []}
    anchor = classify(versions, album_key(title) if title else None)
    album = dict(anchor["tracks"])
    assembled = []
    for v in versions:
        # A release whose album tracks are served from several other releases has no master of
        # its own: its value is a mix of masters. Measured on Tidal: "Aerosmith (2024 Remastered)"
        # plays three tracks of the plain album, one of the expanded edition and four of older
        # releases, and stood on rank 1 with it. It is shown with its values, but not ranked.
        sources = {t["source"] for t in v.get("on_album", {}).values() if t.get("source") is not None}
        if v["group"] != "excluded" and len(sources) > 1:
            v["group"], v["assembled"] = "excluded", len(sources)
            v["excluded_reason"] = (f"Put together by {service} from tracks of {len(sources)} different releases "
                                    "- it has no master of its own")
            assembled.append(v)
    excluded = [v for v in versions if v["group"] == "excluded"]
    members = [v for v in versions if v["group"] != "excluded"]
    for v in members:                       # rank on the album's tracks, keep the rest for reference
        v["all_tracks"] = v["tracks"]
        v["tracks"] = {k: v["on_album"][k] for k in album if k in v["on_album"]}
    ranked, rest, shared = core.analyse(members, keep_all=True) if members else ([], [], [])
    no_data = [v for v in rest if v.get("gain_mean") is None]
    for v in no_data:
        v["excluded_reason"] = f"{service} provides no loudness data for this edition"
        excluded.append(v)
    unranked = [v for v in rest if v.get("gain_mean") is not None]
    for v in ranked:
        v["reasons"] = reasons(v, ranked)
    for v in unranked:
        v["reasons"] = (["Only edition with loudness data - nothing to compare it with."] if not ranked
                        else ["Shares no track with loudness data with the other editions."])
    for v in excluded:                      # own values, so they can still be looked at
        if "n" not in v:
            core.analyse([v], keep_all=True)
    best = ranked[0] if ranked else (unranked[0] if len(unranked) == 1 else None)
    # a release put together from others is no place to send someone for 'better masters of
    # single tracks': what plays there can change with the releases it borrows from
    partials = better_tracks([v for v in excluded if not v.get("assembled")], members, best, album)
    shown = {x["id"] for x in partials}
    excluded = [v for v in excluded if v["id"] not in shown]
    notes = []
    if assembled:
        names = ", ".join(f"\u201c{name(v)}\u201d" for v in assembled[:3]) + (" ..." if len(assembled) > 3 else "")
        notes.append(f"{service} plays {names} from tracks of several other releases. "
                     + ("It is" if len(assembled) == 1 else "They are")
                     + " listed below with " + ("its" if len(assembled) == 1 else "their")
                     + " values, but not ranked: a mix of masters is no master.")
    if no_data:
        names = ", ".join(name(v) for v in no_data[:3]) + (" ..." if len(no_data) > 3 else "")
        notes.append(f"{len(no_data)} edition(s) of the album cannot be ranked because {service} stores no "
                     f"loudness data for them: {names}.")
    groups = []
    if ranked or unranked:
        by_dyn = bool(ranked) and ranked[0]["metric"] == "plr"
        groups.append({
            "key": "editions", "title": "Editions",
            "hint": "All editions of the album, compared on its own tracks. Bonus material is ignored.",
            "metric": (ranked[0]["metric"] if ranked else None),
            "basis": (f"Ranked by dynamics over {len(shared)} album track(s)." if by_dyn else
                      f"Ranked by loudness over {len(shared)} album track(s) - {service} has no peak "
                      "levels for some of them." if ranked else ""),
            "verdict": verdict(ranked) if ranked else None,
            "best": ranked[0]["id"] if ranked else (unranked[0]["id"] if len(unranked) == 1 else None),
            "ranked": [export(v) for v in ranked],
            "unranked": [export(v) for v in unranked],
            "tracks": track_table(ranked, shared),
        })
    return {
        "album": {"title": anchor["title"], "artist": anchor["artist"], "cover": anchor["cover"],
                  "tracks": len(album), "reference": anchor["label"]},
        "groups": groups,
        "excluded": [export(v) for v in excluded],
        "found": len(versions),
        "ranked": len(ranked),
        "notes": notes,
        "partials": partials,
    }
