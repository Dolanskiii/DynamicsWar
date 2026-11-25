(() => {
  "use strict";

  const SESSION = document.querySelector('meta[name="session"]').content;
  const main = document.getElementById("main");
  const input = document.getElementById("q");
  const state = { status: null, query: "", groups: null, seq: 0, pending: null, sort: {}, screen: null, from: null };

  // ------------------------------------------------------------ helpers

  // The streaming service this window works with ("Qobuz", "Tidal") - every text names it.
  const svc = () => (state.status && state.status.service && state.status.service.label) || "Qobuz";

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  const mark = () => document.querySelector(".brand .mark").cloneNode(true);

  // ----------------------------------------------------------- explanations

  // Short explanations shown when the mouse rests on a value (elements with data-tip).
  const TIP = {
    rank: "Rank. Ordered by dynamics. Editions with the same master are ordered by resolution, then by number of tracks.",
    get format() { return `Bit depth / sampling rate in kHz, as delivered by ${svc()}. 16/44.1 is CD quality, everything above is hi-res.\n` +
      "A higher format is not a better master: it only decides between editions of the same master."; },
    dynamics: "Distance between peak level and average loudness, averaged over the compared tracks. " +
      "Higher = more dynamic, less compressed.\nNot the DR value of the Dynamic Range Database: that one is measured from the audio.",
    steps: "Steps: 1 below 6 dB \u00b7 2 from 6 \u00b7 3 from 7 \u00b7 4 from 8.5 \u00b7 5 from 10 \u00b7 6 from 13 dB.",
    loudness: "Average loudness of the compared tracks in LUFS. Closer to 0 = louder master: " +
      "\u22129 LUFS is 5 dB louder than \u221214 LUFS.\nLoudness explains the result but does not rank it.",
    roon: "Roon volume leveling (target \u221214 LUFS). Negative: Roon turns this master down by that much. " +
      "Roon never turns music up into clipping, so quiet masters often show +0.0 dB.",
    peak: "Highest peak level among the compared tracks, in dB relative to full scale (0 dBFS is the maximum).\n" +
      "Close to 0 together with low dynamics points to a master pushed against the limit.",
    tracks: "Tracks compared / tracks on this edition. Only the album's own tracks are compared, bonus material is ignored.",
    best: "First in the ranking: the most dynamic master. With the same master, the higher resolution, then the edition with more tracks.",
    same: "Identical loudness on every track, or evenly shifted by the same amount: the same mastering, delivered in another resolution or at another level.",
    foreign: "A release under another name that contains the album's tracks as the same recordings, such as a box set.",
    extra: "Tracks beyond the album itself (bonus material). They are not part of the comparison.",
    rerecorded: "A new performance of the album, not a remaster of the original recording.",
    gain: "How much more dynamic these tracks are here than on the best edition, on average.",
    partialCount: "Album tracks on this release that are the very same recording (same ISRC) / tracks of the album.",
  };

  const tipBox = h("div", { class: "tip", role: "tooltip", hidden: true });
  document.body.append(tipBox);
  let tipTimer = 0;
  let tipFor = null;

  function hideTip() {
    clearTimeout(tipTimer);
    tipFor = null;
    tipBox.hidden = true;
  }

  function showTip(el) {
    tipBox.textContent = el.dataset.tip;
    tipBox.hidden = false;
    const r = el.getBoundingClientRect();
    const t = tipBox.getBoundingClientRect();
    const x = Math.max(8, Math.min(r.left + r.width / 2 - t.width / 2, window.innerWidth - t.width - 8));
    let y = r.top - t.height - 8;
    if (y < 8) y = r.bottom + 8;
    tipBox.style.left = `${Math.round(x)}px`;
    tipBox.style.top = `${Math.round(y)}px`;
  }

  document.addEventListener("mouseover", (e) => {
    const el = e.target.closest ? e.target.closest("[data-tip]") : null;
    if (el === tipFor) return;
    hideTip();
    if (!el) return;
    tipFor = el;
    tipTimer = setTimeout(() => showTip(el), 300);
  });
  document.addEventListener("mouseleave", hideTip);
  window.addEventListener("scroll", hideTip, true);
  document.addEventListener("mousedown", hideTip);
  document.addEventListener("keydown", hideTip);

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const show = (...nodes) => { state.screen = null; main.replaceChildren(...nodes); window.scrollTo(0, 0); };

  function setNav(name) {
    document.getElementById("nav-compare").classList.toggle("on", name === "compare");
    document.getElementById("nav-library").classList.toggle("on", name === "library");
    document.getElementById("nav-timeline").classList.toggle("on", name === "timeline");
    document.getElementById("nav-settings").classList.toggle("on", name === "settings");
  }

  async function api(path, body) {
    const opts = { headers: { "X-Session": SESSION } };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      const err = new Error("DynamicsWar is no longer running. Close this window and start it again.");
      err.status = -1;
      throw err;
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* no body */ }
    if (res.status === 403) taken();
    if (!res.ok) {
      const err = new Error((data && data.error) || `Request failed (${res.status})`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // The server no longer knows this window: DynamicsWar was started again (after an update) and
  // has opened a window of its own. This one closes if the browser lets it, else it says so.
  let takenOver = false;
  function taken() {
    if (takenOver) return;
    takenOver = true;
    window.close();
    document.body.replaceChildren(h("div", { class: "notice taken" },
      h("h1", {}, "DynamicsWar was started again"),
      h("p", {}, "This window belongs to the earlier start. Continue in the new window, or use this one."),
      h("button", { type: "button", class: "primary", onclick: () => location.reload() }, "Reload this window")));
  }

  function num(x, digits = 1) {
    if (x === null || x === undefined) return "–";
    return x.toFixed(digits).replace("-", "−");
  }
  const unit = (x, u, digits = 1) => (x === null || x === undefined ? "–" : `${num(x, digits)} ${u}`);

  function cover(url, text) {
    const initials = (text || "?").split(/\s+/).slice(0, 2).map((w) => w[0] || "").join("").toUpperCase();
    const box = h("div", { class: "cover" }, initials);
    if (url) {
      const img = h("img", { src: url, alt: "", loading: "lazy" });
      img.addEventListener("error", () => img.remove());     // initials stay visible
      box.append(img);
    }
    return box;
  }

  function notice(text, cls) {
    return h("div", { class: "notice" + (cls ? " " + cls : "") }, cls ? null : h("div", { class: "spinner" }), text);
  }

  // --------------------------------------------------------------- home

  function showHome() {
    state.seq++;
    setNav("compare");
    show(h("section", { class: "hero-empty" },
      mark(),
      h("h1", {}, "Find the best-sounding edition"),
      h("p", {}, `Search for an album. DynamicsWar compares every edition on ${svc()} and shows which master ` +
        "has the widest dynamics, and why."),
      h("div", { class: "steps" },
        h("div", {}, h("b", {}, "1. Search"), "Type an artist or an album into the field above."),
        h("div", {}, h("b", {}, "2. Pick the album"), "All editions of an album are grouped under one cover."),
        h("div", {}, h("b", {}, "3. Compare"), "Every edition is ranked on the album's own tracks."))));
  }

  // ------------------------------------------------------------- search

  let timer = 0;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length >= 3) timer = setTimeout(() => runSearch(q), 450);
  });
  document.getElementById("search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    clearTimeout(timer);
    runSearch(input.value);
  });
  document.getElementById("home").addEventListener("click", () => { input.value = ""; showHome(); input.focus(); });
  document.getElementById("nav-compare").addEventListener("click", () => (state.groups ? showResults() : showHome()));
  document.getElementById("nav-library").addEventListener("click", () => showLibrary());
  document.getElementById("nav-timeline").addEventListener("click", () => showTimeline());

  async function runSearch(raw) {
    const q = raw.trim();
    if (q.length < 2) return showHome();
    const seq = ++state.seq;
    setNav("compare");
    state.query = q;
    show(notice(`Searching for “${q}” …`));
    try {
      const data = await api("/api/search?q=" + encodeURIComponent(q));
      if (seq !== state.seq) return;
      state.groups = data.groups;
      showResults();
    } catch (e) {
      if (seq !== state.seq) return;
      if (e.status === 401) needLogin(() => runSearch(q));
      show(notice(e.message, "error"));
    }
  }

  function showResults() {
    state.seq++;
    setNav("compare");
    const groups = state.groups || [];
    if (!groups.length) {
      return show(notice(`No albums found for “${state.query}”. Check the spelling or try fewer words.`, "empty"));
    }
    show(
      h("div", { class: "section-head" },
        h("h1", {}, `Albums for “${state.query}”`),
        h("span", {}, `${groups.length} album${groups.length === 1 ? "" : "s"}`)),
      h("div", { class: "grid" }, groups.map((g) => {
        const meta = [g.year, `${g.count} edition${g.count === 1 ? "" : "s"}`, g.format && `up to ${g.format}`]
          .filter(Boolean).join(" · ");
        return h("button", { class: "card", type: "button", onclick: () => startCompare(g) },
          cover(g.cover, g.title),
          h("div", {}, h("b", { title: g.title }, g.title), h("div", { class: "by" }, g.artist),
            h("div", { class: "meta" }, meta)));
      })));
  }

  // ------------------------------------------------------------ compare

  const STAGES = {
    search: () => `Searching ${svc()} for editions …`,
    load: (j) => `Loading edition ${j.done} of ${j.total} …`,
    tracks: (j) => `Looking for editions under other names (${j.done}/${j.total}) …`,
    measure: (j) => {
      const rate = (state.status && state.status.service && state.status.service.loudness_rate) || 1;
      const left = Math.ceil((j.total - j.done) / rate);
      return `Reading loudness from ${svc()}: track ${j.done + 1} of ${j.total}, about ${left} s left. Each track is read only once.`;
    },
    analyse: () => "Comparing …",
  };

  function albumHead(title, artist, coverUrl, facts) {
    return h("section", { class: "album" }, cover(coverUrl, title),
      h("div", {}, h("h1", {}, title), h("div", { class: "by" }, artist),
        facts ? h("div", { class: "facts" }, facts) : null));
  }

  function backButton() {
    if (state.from === "library") {
      return h("button", { class: "back", type: "button", onclick: () => showLibrary() }, "← Back to library");
    }
    if (state.from === "timeline") {
      return h("button", { class: "back", type: "button", onclick: () => showTimeline() }, "← Back to timeline");
    }
    return h("button", { class: "back", type: "button", onclick: () => (state.groups ? showResults() : showHome()) },
      "← Back to results");
  }

  async function startCompare(group, from) {
    const seq = ++state.seq;
    state.from = from || null;
    setNav(from || "compare");
    const text = h("p", {}, STAGES.search());
    const bar = h("div", { class: "bar busy" }, h("i"));
    let running = null;
    // ends the comparison on the server too: where loudness costs a request per track, a
    // comparison nobody waits for must not go on sending them
    const halt = () => { if (running) api("/api/compare/stop", { job: running }).catch(() => {}); };
    const stopButton = h("button", { class: "ghost sm", type: "button", hidden: "", onclick: halt }, "Stop");
    show(backButton(), albumHead(group.title, group.artist, group.cover),
      h("div", { class: "progress" }, text, bar, stopButton));
    try {
      const { job } = await api("/api/compare",
        { artist: group.artist, artist_id: group.artist_id, title: group.title, ids: group.ids });
      running = job;
      for (;;) {
        await sleep(300);
        if (seq !== state.seq) return halt();
        const j = await api("/api/job/" + job);
        if (seq !== state.seq) return halt();
        if (j.state === "running") {
          text.textContent = (STAGES[j.stage] || STAGES.search)(j);
          stopButton.hidden = j.stage !== "measure";
          const known = j.total > 0 && j.stage !== "search" && j.stage !== "analyse";
          bar.classList.toggle("busy", !known);
          bar.firstChild.style.width = known ? `${Math.round(100 * j.done / j.total)}%` : "";
        } else if (j.state === "done") {
          refreshStatus();
          return showCompare(group, j.result);
        } else {
          const err = new Error(j.error.message);
          err.status = j.error.status;
          throw err;
        }
      }
    } catch (e) {
      if (seq !== state.seq) return;
      if (e.status === 401) needLogin(() => startCompare(group, from));
      show(backButton(), albumHead(group.title, group.artist, group.cover), notice(e.message, "error"));
    }
  }

  const SORTS = {
    rank: { get: (v) => v.rank, dir: 1 },
    format: { get: (v) => v.bits * 1000 + v.rate, dir: -1 },
    dynamics: { get: (v) => v.dynamics, dir: -1 },
    loudness: { get: (v) => v.loudness, dir: 1 },
    peak: { get: (v) => v.peak_db, dir: 1 },
  };

  const signed = (x) => `${x > 0 ? "+" : ""}${num(x)} dB`;

  // Roon shows the album value when an album plays through, the track value otherwise.
  function roonLine(v) {
    const album = v.roon_album, track = v.roon_track;
    if (album === null && track === null) return null;
    const shown = album === null ? track : album;
    const lines = [TIP.roon];
    if (album !== null) lines.push(`Album mode (the album plays through): ${signed(album)}, set by the loudest track of this edition.`);
    if (track !== null) lines.push(`Track mode (single tracks, playlists): ${signed(track)} on average over the compared tracks.`);
    return h("small", { "data-tip": lines.join("\n") }, `Roon ${signed(shown)}`);
  }

  function editionRow(v, best) {
    const tag = (text, tip, cls) => h("span", { class: "tag" + (cls ? " " + cls : ""), "data-tip": tip }, text);
    const tags = [];
    if (best) tags.push(tag("Best", TIP.best, "gold"));
    if (v.same_as) tags.push(tag(`Same master as #${v.same_as}`, TIP.same));
    if (v.rerecorded) tags.push(tag("Re-recording", TIP.rerecorded));
    if (v.foreign) tags.push(tag("Box set / compilation", TIP.foreign));
    if (v.group === "expanded") tags.push(tag(`+${v.extra} tracks`, TIP.extra));
    const sub = [v.company, v.year, v.stream && v.stream !== "?" ? `on ${svc()} since ${v.stream}` : null]
      .filter((x) => x && x !== "?").join(" \u00b7 ");
    const a = v.assessment;
    return h("div", { class: "row" + (best ? " best" : "") },
      h("div", { class: "rank", "data-tip": TIP.rank }, v.rank || "\u2013"),
      cover(v.thumb || v.cover, v.label),
      h("div", { class: "name" },
        h("div", { class: "line" }, h("b", { title: v.label }, v.label), tags),
        h("div", { class: "sub" }, sub)),
      h("div", { class: "fmt" + (v.bits > 16 ? " hires" : ""), "data-tip": TIP.format }, v.format),
      h("div", { class: "val num dyn",
        "data-tip": TIP.dynamics + (a ? `\nThis edition: step ${a.level} of 6. ${TIP.steps}` : "") },
        h("b", {}, unit(v.dynamics, "dB")),
        a ? h("span", { class: `meter l${a.level}` }, h("i"), h("i"), h("i"), h("i"), h("i"), h("i")) : null,
        a ? h("small", {}, a.label) : null),
      h("div", { class: "val num loud" },
        h("b", { "data-tip": TIP.loudness }, unit(v.loudness, "LUFS")),
        roonLine(v)),
      h("div", { class: "num peak" }, h("span", { "data-tip": TIP.peak }, unit(v.peak_db, "dBFS"))),
      h("div", { class: "num cnt" }, h("span", { "data-tip": TIP.tracks }, `${v.compared}/${v.total}`)),
      h("div", { class: "why" }, h("p", {}, (v.reasons || []).join(" ")),
        h("button", { type: "button", onclick: () => api("/api/open", { url: v.url }).catch(() => {}) },
          `Open in ${svc()} \u2197`)));
  }

  function editionTable(group) {
    const wrap = h("div", { class: "table" });
    const draw = () => {
      const sort = state.sort[group.key] || { col: "rank", dir: 1 };
      const rule = SORTS[sort.col];
      const rows = group.ranked.slice().sort((x, y) => {
        const a = rule.get(x), b = rule.get(y);
        if (a === null || a === undefined) return 1;
        if (b === null || b === undefined) return -1;
        return (a - b) * sort.dir || x.rank - y.rank;
      }).concat(group.unranked);
      const head = (col, label, cls) => h("button", {
        type: "button",
        class: (sort.col === col && !(col === "rank" && sort.dir > 0) ? "on" + (sort.dir > 0 ? " up" : "") : ""),
        "data-tip": TIP[col] + "\nClick to sort.",
        onclick: () => {
          state.sort[group.key] = { col, dir: sort.col === col ? -sort.dir : SORTS[col].dir };
          draw();
        },
      }, label);
      wrap.replaceChildren(
        h("div", { class: "row head" },
          h("div", { class: "rank" }, head("rank", "#")), h("div"), h("div", {}, "Edition"),
          h("div", {}, head("format", "Format")),
          h("div", { class: "num" }, head("dynamics", "Dynamics")),
          h("div", { class: "num loud" }, head("loudness", "Loudness")),
          h("div", { class: "num peak" }, head("peak", "Peak")),
          h("div", { class: "num cnt" }, h("span", { "data-tip": TIP.tracks }, "Tracks"))),
        ...rows.map((v) => editionRow(v, v.id === group.best && (v.rank === 1 || !group.ranked.length))));
    };
    draw();
    return wrap;
  }

  function trackTable(group) {
    if (!group.tracks.length || group.ranked.length < 2) return null;
    const byDyn = group.metric === "plr";
    const key = byDyn ? "dynamics" : "loudness";
    const body = group.tracks.map((t) => {
      const vals = group.ranked.map((v) => (t.values[v.id] ? t.values[v.id][key] : null));
      const real = vals.filter((x) => x !== null && x !== undefined);
      const top = real.length ? (byDyn ? Math.max(...real) : Math.min(...real)) : null;
      return h("tr", {}, h("td", {}, t.title), group.ranked.map((v, i) => {
        const cell = t.values[v.id];
        if (!cell || vals[i] === null) return h("td", { class: "none" }, "–");
        return h("td", { class: vals[i] === top ? "lead" : "" }, num(vals[i]) + (cell.variant ? "*" : ""));
      }));
    });
    const anyVariant = group.tracks.some((t) => Object.values(t.values).some((c) => c.variant));
    return h("details", {},
      h("summary", {}, `Track by track (${byDyn ? "dynamics in dB" : "loudness in LUFS"})`),
      h("div", { class: "scroll" }, h("table", { class: "tracks" },
        h("thead", {}, h("tr", {}, h("th", {}, "Track"),
          group.ranked.map((v) => h("th", { title: v.label }, `#${v.rank} · ${v.format}`)))),
        h("tbody", {}, body))),
      anyVariant ? h("p", { class: "foot" }, "* only available as a mix, live or demo take on this edition") : null);
  }

  function showCompare(group, r) {
    const nodes = [backButton()];
    const album = r.album || { title: group.title, artist: group.artist, cover: group.cover };
    const facts = r.album
      ? `${r.found} release${r.found === 1 ? "" : "s"} found · ${r.ranked} ranked · album has ${r.album.tracks} tracks · ${r.seconds} s`
      : null;
    nodes.push(albumHead(group.title, album.artist || group.artist, album.cover || group.cover, facts));
    if (!r.groups.length) {
      nodes.push(notice(r.found
        ? `None of the releases found can be ranked: they are live, mono or remixed versions, or ${svc()} has no loudness data for them.`
        : `No edition with tracks was found on ${svc()}.`, "empty"));
    }
    for (const note of r.notes || []) nodes.push(h("div", { class: "callout" }, h("b", {}, "Note: "), note));
    for (const g of r.groups) {
      const best = g.ranked.concat(g.unranked).find((v) => v.id === g.best);
      nodes.push(h("section", { class: "group" },
        h("header", {}, h("h2", {}, g.title), h("span", {}, [g.hint, g.basis].filter(Boolean).join(" "))),
        g.verdict ? h("div", { class: "verdict" }, best ? cover(best.cover || best.thumb, best.label) : null,
          h("div", {}, h("small", {}, "Best edition"), h("p", {}, g.verdict))) : null,
        editionTable(g),
        trackTable(g)));
    }
    if ((r.partials || []).length) {
      nodes.push(h("section", { class: "group partial" },
        h("header", {}, h("h2", {}, "Better masters of single tracks"),
          h("span", {}, "Not editions of the album, but they hold some of its tracks as the same studio " +
            "recording in a more dynamic master.")),
        h("div", { class: "table" }, r.partials.map((v) => h("div", { class: "row" },
          cover(v.thumb || v.cover, v.label),
          h("div", { class: "name" }, h("div", { class: "line" }, h("b", { title: v.label }, v.label)),
            h("div", { class: "sub" }, [v.company, v.year].filter((x) => x && x !== "?").join(" \u00b7 "))),
          h("div", { class: "fmt" + (v.bits > 16 ? " hires" : "") }, v.format),
          h("div", { class: "val num" }, h("b", { "data-tip": TIP.dynamics }, unit(v.dynamics, "dB")),
            h("small", { "data-tip": TIP.gain }, `+${num(v.gain)} dB`)),
          h("div", { class: "num cnt" }, h("span", { "data-tip": TIP.partialCount }, `${v.count}/${v.album_size}`)),
          h("div", { class: "why" }, h("p", {}, v.summary),
            h("button", { type: "button", onclick: () => api("/api/open", { url: v.url }).catch(() => {}) },
              `Open in ${svc()} \u2197`)),
          h("div", { class: "chips" }, v.tracks.map((t) => h("span", { class: "chip",
            "data-tip": `Dynamics of this track: ${num(t.here)} dB here, ${num(t.best)} dB on the best edition.` },
            `${t.title} +${num(t.gain)} dB`))))))));
    }
    if (r.excluded.length) {
      nodes.push(h("section", { class: "group other" }, h("details", {},
        h("summary", {}, `Found but not ranked (${r.excluded.length})`),
        h("div", { class: "table" }, r.excluded.map((v) => h("div", { class: "row" },
          cover(v.thumb || v.cover, v.label),
          h("div", { class: "name" }, h("div", { class: "line" }, h("b", { title: v.label }, v.label)),
            h("div", { class: "reason" }, v.excluded_reason)),
          h("div", { class: "fmt" + (v.bits > 16 ? " hires" : "") }, v.format),
          h("div", { class: "val num" }, h("b", { "data-tip": TIP.dynamics }, unit(v.dynamics, "dB"))),
          h("div", { class: "val num loud" }, h("b", { "data-tip": TIP.loudness }, unit(v.loudness, "LUFS")))))))));
    }
    const s = r.sources || {};
    nodes.push(h("div", { class: "foot" },
      h("p", {}, "Dynamics is the distance between peak level and loudness of each track, calculated from the " +
        `loudness data ${svc()} stores per track. Higher means more dynamic. All editions are compared on the ` +
        "album's own tracks; bonus material is ignored. Within the same master the higher resolution ranks " +
        "first, then the edition with more tracks. A release counts as an edition when it holds the album's " +
        "tracks as the same recordings (same length). No audio is downloaded."),
      h("p", {}, `Found via album search: ${s.search || 0} · discography: ${s.discography || 0} · track search: ${s.tracks || 0}.` +
        ` ${r.requests} ${svc()} requests in ${r.seconds} s.` +
        (r.truncated ? ` More editions exist than the ${r.limit} that were loaded.` : ""))));
    show(...nodes);
  }


  // ------------------------------------------------------------ library

  const LIB_TIP = {
    gain: "How much more dynamic the best edition is than yours, averaged over the album's tracks.\n" +
      "Below 1 dB two masters count as practically the same and are not reported.",
    get mine() { return `The edition you marked as a favourite on ${svc()}.`; },
    bestOn: "The first edition in the ranking of this album: the most dynamic master, in its highest resolution.",
    select: "Select the album for the next run.",
    compare: "Opens the full comparison of all editions of this album.",
    get again() { return `Checks this album again right away (${perCheck()}). The current result stays until the new one is in.`; },
    unsure: "Shown because the numbers say so - but they may not be comparable here.",
    upgrade: "Your edition in a higher resolution, if it exists - otherwise the first edition of the ranking with the same master.",
    also: "The same master and resolution with more tracks: first in the album's ranking, because more content wins. " +
      "Shown second, because the edition named above is the direct upgrade of the one in your library.",
    full: "The limit for one run is reached. Start the check, then select the next albums.",
  };
  const lib = { data: null, sel: new Set(), filter: "", all: false, job: null, message: null, pausedUntil: 0, open: {} };

  async function showLibrary(refresh) {
    const seq = ++state.seq;
    setNav("library");
    state.from = null;
    if (!lib.data || refresh) {
      show(notice(`Reading your ${svc()} favourites …`));
      try {
        const data = await api("/api/library" + (refresh ? "?refresh=1" : ""));
        if (seq !== state.seq) return;
        lib.data = data;
        lib.pausedUntil = Date.now() + data.paused * 1000;
        const known = new Set(data.albums.map((a) => a.id));
        for (const id of [...lib.sel]) if (!known.has(id)) lib.sel.delete(id);
      } catch (e) {
        if (seq !== state.seq) return;
        if (e.status === 401) needLogin(() => showLibrary(refresh));
        return show(notice(e.message, "error"));
      }
    }
    drawLibrary(true);
    if (lib.data.run && !lib.job) followRun(lib.data.run);
  }

  const fmtSpan = (v) => h("span", { class: "fmt" + (v.bits > 16 ? " hires" : ""), "data-tip": TIP.format }, v.format);

  function edCell(v, extra) {
    return h("div", { class: "ed" }, h("b", { title: v.label }, v.label),
      h("div", { class: "sub" }, fmtSpan(v), v.year && v.year !== "?" ? ` · ${v.year}` : ""), extra || null);
  }

  function dynCell(v, lead) {
    if (!v || v.dynamics === null || v.dynamics === undefined) return h("div", { class: "dyn none" }, "–");
    return h("div", { class: "val num dyn", "data-tip": TIP.dynamics + (v.level ? `\nStep ${v.level} of 6. ${TIP.steps}` : "") },
      h("b", { class: lead ? "lead" : "" }, unit(v.dynamics, "dB")),
      v.level ? h("span", { class: `meter l${v.level}` }, h("i"), h("i"), h("i"), h("i"), h("i"), h("i")) : null,
      v.verdict ? h("small", {}, v.verdict) : null);
  }

  function albumCell(a) {
    return h("div", { class: "name" }, h("div", { class: "line" }, h("b", { title: a.title }, a.title)),
      h("div", { class: "sub" }, a.artist));
  }

  function openCompare(a) {
    const q = (a.check && a.check.query) || { title: a.title, artist: a.artist, artist_id: a.artist_id, ids: [a.id] };
    startCompare({ title: q.title, artist: q.artist, artist_id: q.artist_id, ids: q.ids, cover: a.cover }, "library");
  }

  // Starts a run for these albums. Albums that already have a result keep it until the new one is in.
  async function startRun(ids, done) {
    if (lib.job || !ids.length) return;
    lib.message = null;
    try {
      const { job } = await api("/api/library/check", { ids });
      if (done) done();
      followRun(job, ids);
    } catch (e) {
      if (e.status === 401) return needLogin(() => showLibrary());
      lib.message = { text: e.message, error: true };
      if (state.screen === "library") drawLibrary();
    }
  }

  const checkAgain = (a) => startRun([a.id]);

  // Checks the albums of a group again, oldest result first, never more than one run allows.
  function recheck(rows) {
    const oldest = rows.slice().sort((x, y) => String(x.check.when || "").localeCompare(String(y.check.when || "")));
    startRun(oldest.slice(0, lib.data.limit).map((a) => a.id));
  }

  // What one check costs, in words. A service that reads loudness track by track has stored the
  // values of an album it checked before, so a second check only searches again.
  function perCheck(n) {
    const lazy = lib.data && lib.data.lazy;
    if (n === undefined) return lazy ? `around 25 requests to ${svc()}, the loudness values are already stored` : `around 30 requests to ${svc()}`;
    return lazy ? `about ${n * 25} requests to ${svc()}, the loudness values are already stored` : `about ${n * 30} requests to ${svc()}`;
  }

  function recheckButton(rows, paused) {
    const n = Math.min(rows.length, lib.data.limit);
    const all = n === rows.length;
    return h("button", { class: "ghost sm recheck", type: "button", disabled: !!lib.job || paused > 0,
      "data-tip": `Checks ${all ? "these albums" : `the ${n} albums with the oldest result`} again - one run, ${perCheck(n)}. ` +
        "The current results stay until the new ones are in.",
      onclick: () => recheck(rows) }, all ? `Check ${n === 1 ? "it" : `all ${n}`} again` : `Check the ${n} oldest again`);
  }

  // "2026-11-25 17:26" -> "today 17:26", "3 days ago", "2 weeks ago" ...
  function age(when) {
    const t = new Date(String(when || "").replace(" ", "T"));
    if (isNaN(t)) return when || "";
    const day = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const days = Math.round((day(new Date()) - day(t)) / 86400000);
    if (days <= 0) return `today ${when.slice(11)}`;
    if (days === 1) return "yesterday";
    if (days < 14) return `${days} days ago`;
    if (days < 60) return `${Math.round(days / 7)} weeks ago`;
    return `${Math.round(days / 30)} months ago`;
  }

  function resultRow(a) {
    const c = a.check;
    const mine = c.mine || a;
    const open = (v) => (v && v.url ? h("button", { class: "open", type: "button",
      onclick: () => api("/api/open", { url: v.url }).catch(() => {}) }, `Open in ${svc()} ↗`) : null);
    let middle;
    let second = [];
    const paired = c.kind === "master" || c.kind === "resolution";      // two editions side by side
    if (paired) {
      if (c.also) {
        // second suggestion on its own line: the edition with more content
        const ed = edCell(c.also, open(c.also));
        ed.classList.add("also-ed");
        ed.prepend(h("small", { class: "cap", "data-tip": LIB_TIP.also }, "Also available · more content"));
        const dyn = dynCell(c.also);
        dyn.classList.add("also-dyn");
        // results stored before 0.2.9 carry no flag of their own: there the upgrade's flag applies
        const sameMaster = c.also.same_master === undefined ? c.same_master : c.also.same_master;
        const g = c.also.gain;
        second = [h("span", { class: "also-line" }), h("span", { class: "arrow also-arrow" }, "+"), ed, dyn,
          h("span", { class: "also-tag" },
            h("span", { class: "gain muted", "data-tip": TIP.same },
              sameMaster ? "Same master" : `Practically the same${g === null || g === undefined ? "" : ` (${g > 0 ? "+" : ""}${num(g)} dB)`}`),
            h("span", { class: "gain plain", "data-tip": LIB_TIP.also },
              c.also.more_tracks ? `+${c.also.more_tracks} tracks` : "More content"))];
      }
      middle = [h("span", { class: "arrow" }, "→"), edCell(c.best, open(c.best)), dynCell(c.best, c.kind === "master"),
        c.kind === "master"
          ? h("span", { class: "gain" + (c.unsure ? " unsure" : ""), "data-tip": c.unsure ? `${LIB_TIP.unsure}\n${c.unsure}` : LIB_TIP.gain },
            `+${num(c.gain)} dB`, c.unsure ? h("small", {}, "uncertain") : null)
          : h("span", { class: "gain muted", "data-tip": TIP.same },
            c.same_master ? "Same master" : `Practically the same (${c.gain > 0 ? "+" : ""}${num(c.gain)} dB)`)];
    } else {
      const text = c.kind === "other" ? c.reason
        : c.only ? `Only edition on ${svc()}`
        : c.equal ? `As good as the best of ${c.editions} editions`
        : `Best of ${c.editions} editions on ${svc()}`;
      middle = [h("span", { class: "arrow" }), h("div", { class: "same" }, text)];
    }
    const busy = !!lib.job && lib.job.ids.has(a.id);         // part of the running check: the old result stays until the new one is in
    const info = busy ? (lib.job.current === a.label ? "Checking again …" : "Queued for another check")
      : [c.when ? `Checked ${age(c.when)}` : null, c.requests ? `${c.requests} requests` : null].filter(Boolean).join(" · ");
    return h("div", { class: "row lib" + (c.kind === "master" ? " up" : "") + (paired ? "" : " wide") },
      cover(a.cover, a.title), albumCell(a), edCell(mine), dynCell(c.mine), middle,
      h("div", { class: "acts" },
        h("button", { class: "ghost sm", type: "button", "data-tip": LIB_TIP.compare, onclick: () => openCompare(a) }, "Compare"),
        h("button", { class: "ghost sm", type: "button", disabled: !!lib.job, "data-tip": LIB_TIP.again,
          onclick: () => checkAgain(a) }, "Check again")),
      second,
      h("div", { class: "why" }, h("p", { class: busy ? "busy" : "", "data-tip": !busy && c.when ? `Checked ${c.when}` : null }, info)));
  }

  function resultHead(kind) {
    return h("div", { class: "row lib head" }, h("span"), h("span", {}, "Album"),
      h("span", { "data-tip": LIB_TIP.mine }, "In your library"),
      h("span", { class: "num", "data-tip": TIP.dynamics }, "Dynamics"), h("span"),
      h("span", { "data-tip": kind === "master" ? LIB_TIP.bestOn : LIB_TIP.upgrade }, kind === "master" ? `Best on ${svc()}` : "In higher resolution"),
      h("span", { class: "num", "data-tip": TIP.dynamics }, "Dynamics"),
      h("span", { class: "num", "data-tip": kind === "master" ? LIB_TIP.gain : null }, kind === "master" ? "Gain" : ""),
      h("span"));
  }

  function toggle(id) {
    if (lib.sel.has(id)) lib.sel.delete(id);
    else if (lib.sel.size < lib.data.limit) lib.sel.add(id);
    drawLibrary();
  }

  function todoRow(a, full) {
    const on = lib.sel.has(a.id);
    const off = full && !on;
    const box = h("input", { type: "checkbox", checked: on, disabled: off || !!lib.job,
      "aria-label": `${a.title} – ${a.artist}`, onchange: () => toggle(a.id) });
    return h("label", { class: "row pick" + (on ? " sel" : "") + (off ? " dim" : ""),
      "data-tip": off ? LIB_TIP.full : null },
      h("span", { class: "box" }, box, h("i")), cover(a.cover, a.title), albumCell(a), edCell(a),
      h("span", { class: "state" }, lib.job && lib.job.ids.has(a.id)
        ? (lib.job.current === a.label ? "Checking …" : "Queued") : "Not checked yet"));
  }

  // mark: "hot" (tinted tile, label and number in the contrast colour) or "lit" (label and number only)
  function tile(label, value, sub, mark) {
    return h("div", { class: "tile" + (mark ? " " + mark : "") }, h("small", {}, label), h("b", {}, String(value)), h("em", {}, sub));
  }

  function drawLibrary(top) {
    const d = lib.data;
    const y = window.scrollY;
    const by = { master: [], resolution: [], best: [], other: [] };
    const todo = [];
    for (const a of d.albums) (a.check ? by[a.check.kind] || by.other : todo).push(a);
    by.master.sort((x, y2) => (y2.check.gain || 0) - (x.check.gain || 0));
    const checked = d.albums.length - todo.length;
    const paused = Math.max(0, Math.ceil((lib.pausedUntil - Date.now()) / 60000));
    const nodes = [];

    nodes.push(h("div", { class: "pagehead" }, h("h1", {}, "Library"),
      h("p", {}, `Your ${svc()} favourites. Select the albums you want to check for a better master.`),
      h("div", { class: "right" }, h("span", {}, `${checked} of ${d.albums.length} albums checked`),
        h("button", { class: "ghost sm", type: "button", disabled: !!lib.job, onclick: () => showLibrary(true) }, "Refresh"))));
    if (!d.albums.length) {
      nodes.push(notice(`${svc()} returned no favourite albums. Mark albums with the heart in ${svc()}, then press Refresh.`, "empty"));
      state.screen = "library";
      main.replaceChildren(...nodes);
      return;
    }
    nodes.push(h("div", { class: "tiles" },
      tile("Albums in your library", d.albums.length, `${svc()} favourites`),
      tile("Better master available", by.master.length, `At least ${d.min_gain} dB more dynamic`, by.master.length > 0 && "hot"),
      tile("Higher resolution available", by.resolution.length, "Same master, more bits or kHz", by.resolution.length > 0 && "lit"),
      tile("Already the best edition", by.best.length, "Nothing to do")));
    nodes.push(h("div", { class: "callout limit" }, h("b", {}, `Up to ${d.limit} albums per run. `),
      d.lazy
        ? `For every album ${svc()} is asked for the loudness of each album track on every edition: one request per track, ` +
          `${d.loudness_rate === 2 ? "two" : d.loudness_rate || 1} per second, often 20 to 100 per album. A run ends by itself after about ` +
          `${d.budget} of these requests - start another one for the albums that are left. If ${svc()} turns a request down, ` +
          `the run stops and library checks pause for ${d.cooldown_minutes} minutes. Results and loudness values are saved, so nothing is read twice.`
        : `Every check sends around 30 requests to ${svc()}, one album after the other and at most ${d.rate || 4} per second. If too many arrive in a short ` +
          `time, ${svc()} may limit or block your access: when ${svc()} slows a request down, the run stops by itself and ` +
          `library checks pause for ${d.cooldown_minutes} minutes. Results are saved, so no album is checked twice.`));
    if (paused) {
      nodes.push(h("div", { class: "callout warn" }, h("b", {}, "Paused. "),
        `${svc()} ${d.lazy ? "turned a request down" : "slowed requests down"}. Library checks are possible again in about ${paused} minute${paused === 1 ? "" : "s"}.`));
    }
    if (lib.message) nodes.push(h("div", { class: "callout" + (lib.message.error ? " warn" : "") }, lib.message.text));
    if (lib.job) {
      const j = lib.job;
      nodes.push(h("div", { class: "progress run" },
        h("div", { class: "line" },
          h("p", { id: "run-text" }, runText(j)),
          h("button", { class: "ghost sm", type: "button", onclick: () => api("/api/library/stop", {}).catch(() => {}) }, "Stop")),
        h("div", { class: "bar" }, h("i", { id: "run-bar" }))));
    }

    const section = (title, hint, rows, kind) => h("section", { class: "group" },
      h("header", {}, h("h2", {}, title), h("span", {}, hint), recheckButton(rows, paused)),
      h("div", { class: "table" }, resultHead(kind), rows.map(resultRow)));
    if (by.master.length) {
      nodes.push(section("Better master available", `${by.master.length} album${by.master.length === 1 ? "" : "s"} · biggest gain first`, by.master, "master"));
    }
    if (by.resolution.length) {
      nodes.push(section("Same master in higher resolution", `${by.resolution.length} album${by.resolution.length === 1 ? "" : "s"}`, by.resolution, "resolution"));
    }

    if (todo.length) {
      const words = lib.filter.toLowerCase().split(/\s+/).filter(Boolean);
      const match = todo.filter((a) => words.every((w) => `${a.artist} ${a.label}`.toLowerCase().includes(w)));
      const shown = lib.all || words.length ? match : match.slice(0, 30);
      const full = lib.sel.size >= d.limit;
      const filter = h("input", { class: "filter", type: "search", placeholder: "Filter by artist or album", value: lib.filter,
        spellcheck: "false", "aria-label": "Filter albums" });
      filter.addEventListener("input", () => {
        lib.filter = filter.value;
        drawLibrary();
        const again = main.querySelector("input.filter");
        if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
      });
      const next = () => {
        for (const a of match) { if (lib.sel.size >= d.limit) break; lib.sel.add(a.id); }
        drawLibrary();
      };
      const n = lib.sel.size;
      nodes.push(h("section", { class: "group" },
        h("header", {}, h("h2", {}, "Not checked yet"), h("span", {}, `${todo.length} album${todo.length === 1 ? "" : "s"}`),
          todo.length > 12 ? filter : null),
        h("div", { class: "table" },
          h("div", { class: "row pick head" }, h("span"), h("span"), h("span", {}, "Album"),
            h("span", { "data-tip": LIB_TIP.mine }, "In your library"), h("span", {}, "Result")),
          shown.map((a) => todoRow(a, full)),
          !shown.length ? h("div", { class: "row pick" }, h("span"), h("span"), h("span", { class: "state" }, "No album matches the filter.")) : null,
          shown.length < match.length
            ? h("button", { class: "more", type: "button", onclick: () => { lib.all = true; drawLibrary(); } }, `Show all ${match.length}`) : null,
          h("div", { class: "runbar" },
            h("div", { class: "count" }, h("b", {}, `${n} of ${d.limit} selected`),
              h("span", {}, full ? "Limit for this run reached. The other albums can be selected in the next run."
                : n ? "" : "Tick the albums to check.")),
            n ? h("span", { class: "est" }, d.lazy ? `ends after about ${d.budget} loudness requests` : `about ${n * 30} requests to ${svc()}`)
              : h("span", { class: "est" }),
            h("button", { class: "ghost sm", type: "button", disabled: full || !!lib.job, onclick: next },
              `Select the next ${Math.min(d.limit - n, match.length) || d.limit}`),
            h("button", { class: "ghost sm", type: "button", disabled: !n, onclick: () => { lib.sel.clear(); drawLibrary(); } }, "Clear selection"),
            h("button", { class: "primary sm", type: "button", disabled: !n || !!lib.job || paused > 0, onclick: runCheck },
              n ? `Check ${n} album${n === 1 ? "" : "s"}` : "Check albums")))));
    }

    const folded = (title, rows) => {
      const open = title in lib.open ? lib.open[title] : rows.length <= 6;    // stays as the user left it
      const box = h("details", { open },
        h("summary", {}, `${title} (${rows.length})`),
        h("div", { class: "groupbar" }, recheckButton(rows, paused)),
        h("div", { class: "table" }, rows.map(resultRow)));
      box.addEventListener("toggle", () => { lib.open[title] = box.open; });
      return h("section", { class: "group" }, box);
    };
    if (by.best.length) nodes.push(folded("Already the best edition", by.best));
    if (by.other.length) nodes.push(folded("Not comparable", by.other));
    nodes.push(h("div", { class: "foot" },
      h("p", {}, `The library shows the albums you marked as favourites on ${svc()}. A check is the same comparison as ` +
        "in the search: every edition of the album is ranked on the album's own tracks, then your edition is set " +
        "against the best one. DynamicsWar only reads your library - it never changes your favourites.")));

    state.screen = "library";
    main.replaceChildren(...nodes);
    paintRun();
    window.scrollTo(0, top ? 0 : y);
  }

  function runText(j) {
    if (!j.total) return "Starting …";
    return `Checking album ${Math.min(j.done + 1, j.total)} of ${j.total}: ${j.current || ""}` +
      (j.stage === "measure" ? " \u2013 reading loudness" : "");
  }

  function paintRun() {
    const j = lib.job;
    const text = document.getElementById("run-text");
    const bar = document.getElementById("run-bar");
    if (!j || !text || !bar) return;
    text.textContent = runText(j);
    bar.style.width = j.total ? `${Math.round(100 * j.done / j.total)}%` : "0%";
  }

  const runCheck = () => startRun([...lib.sel], () => lib.sel.clear());

  async function followRun(job, ids) {
    lib.job = { id: job, done: 0, total: 0, current: "", ids: new Set(ids || []) };
    if (state.screen === "library") drawLibrary();
    let seen = 0;
    let failed = 0;
    for (;;) {
      await sleep(500);
      let j;
      try {
        j = await api("/api/job/" + job);
      } catch (e) {
        lib.job = null;
        lib.message = { text: e.message, error: true };
        break;
      }
      const byId = new Map(lib.data.albums.map((a) => [a.id, a]));
      for (const r of j.results.slice(seen)) {
        const album = byId.get(r.id);
        if (!album) continue;
        if (r.check.failed && album.check) failed += 1;       // could not be checked again: the old result stays
        else album.check = r.check;
      }
      const grew = j.results.length > seen;
      seen = j.results.length;
      if (j.state === "running") {
        const moved = lib.job.current !== j.current;
        Object.assign(lib.job, { done: j.done, total: j.total, current: j.current, stage: j.stage });
        if (state.screen === "library") { if (grew || moved) drawLibrary(); else paintRun(); }
        continue;
      }
      lib.job = null;
      if (j.state === "error") {
        lib.message = { text: j.error.message, error: true };
        if (j.error.status === 401) needLogin(() => showLibrary());
      } else if (j.state === "stopped") {
        lib.message = { text: j.message, error: true };
        lib.pausedUntil = Date.now() + lib.data.cooldown_minutes * 60000;
      } else {
        lib.message = { text: (j.message || `Checked ${j.done} album${j.done === 1 ? "" : "s"}.`) +
          (failed ? ` ${failed} could not be checked again and keep${failed === 1 ? "s" : ""} the earlier result.` : "") };
      }
      refreshStatus();
      break;
    }
    if (state.screen === "library") drawLibrary();
  }

  // ----------------------------------------------------------- timeline

  const tl = { data: null, artist: null, view: "chart" };
  const SVG = "http://www.w3.org/2000/svg";

  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v !== null && v !== undefined && v !== false) el.setAttribute(k, v);
    }
    for (const kid of kids.flat()) {
      if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  async function showTimeline() {
    const seq = ++state.seq;
    setNav("timeline");
    state.from = null;
    if (!tl.data) show(notice("Reading your comparisons …"));
    try {
      const data = await api("/api/timeline");
      if (seq !== state.seq) return;
      tl.data = data;
    } catch (e) {
      if (seq !== state.seq) return;
      return show(notice(e.message, "error"));
    }
    drawTimeline(true);
  }

  function openFromTimeline(e) {
    const q = e.query;
    if (!q || !q.title) return;
    startCompare({ title: q.title, artist: q.artist || e.artist, artist_id: q.artist_id, ids: q.ids || [], cover: e.cover }, "timeline");
  }

  const median = (xs) => {
    const v = xs.slice().sort((a, b) => a - b);
    return v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2;
  };

  function timelineChart(albums, focus) {
    const W = Math.max(760, main.clientWidth - 80 - 34), H = 520;
    const L = 58, R = 30, T = 26, B = 46;
    const years = albums.map((e) => e.year), vals = albums.map((e) => e.best.dynamics);
    let x0 = Math.min(...years) - 2, x1 = Math.max(...years) + 2;
    if (x1 - x0 < 12) { x0 -= 4; x1 += 4; }
    const y0 = Math.floor(Math.min(...vals) - 1), y1 = Math.ceil(Math.max(...vals) + 1);
    const sx = (y) => L + (y - x0) / (x1 - x0) * (W - L - R);
    const sy = (v) => T + (y1 - v) / (y1 - y0) * (H - T - B);

    // albums of one artist from the same year stand next to each other instead of on top
    const count = {}, used = {};
    for (const e of albums) count[e.artist + e.year] = (count[e.artist + e.year] || 0) + 1;
    const pts = albums.map((e) => {
      const k = e.artist + e.year, i = used[k] || 0;
      used[k] = i + 1;
      const shift = count[k] === 1 ? 0 : (i - (count[k] - 1) / 2) * 0.7;
      return { e, x: sx(e.year + shift), y: sy(e.best.dynamics), on: !!focus && e.artist === focus };
    });

    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
      "aria-label": "Dynamics of the best edition of each album, by year of release" });
    const yStep = y1 - y0 > 14 ? 4 : 2;
    for (let v = Math.ceil(y0 / yStep) * yStep; v <= y1; v += yStep) {
      if (v === y0) continue;
      svg.append(s("line", { class: "tl-grid", x1: L, x2: W - R, y1: sy(v), y2: sy(v) }),
        s("text", { class: "tl-tick", x: L - 12, y: sy(v) + 4, "text-anchor": "end" }, v === y1 ? `${v} dB` : v));
    }
    if ((y1 - Math.ceil(y0 / yStep) * yStep) % yStep) svg.append(s("text", { class: "tl-tick", x: L - 12, y: T + 4, "text-anchor": "end" }, "dB"));
    const span = x1 - x0;
    const xStep = [1, 2, 5, 10, 20].find((n) => span / n <= 12) || 25;
    for (let y = Math.ceil(x0 / xStep) * xStep; y <= x1; y += xStep) {
      svg.append(s("line", { class: "tl-grid", x1: sx(y), x2: sx(y), y1: H - B, y2: H - B + 6 }),
        s("text", { class: "tl-tick", x: sx(y), y: H - B + 26, "text-anchor": "middle" }, y));
    }
    svg.append(s("line", { class: "tl-axis", x1: L, x2: W - R, y1: H - B, y2: H - B }));

    for (const p of pts) if (!p.on) svg.append(s("circle", { class: "tl-dot" + (focus ? " other" : ""), cx: p.x, cy: p.y, r: 5 }));
    const line = pts.filter((p) => p.on).sort((a, b) => a.x - b.x);
    if (line.length > 1) svg.append(s("polyline", { class: "tl-line", points: line.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ") }));
    for (const p of line) svg.append(s("circle", { class: "tl-dot focus", cx: p.x, cy: p.y, r: 6 }));

    // a few direct labels: the extremes, and the first and last album of the highlighted artist
    const byVal = pts.slice().sort((a, b) => b.e.best.dynamics - a.e.best.dynamics);
    const picks = focus && line.length
      ? [line[0], line[line.length - 1], ...line.slice().sort((a, b) => b.e.best.dynamics - a.e.best.dynamics).slice(0, 1), byVal[0], byVal[byVal.length - 1]]
      : [...byVal.slice(0, 3), ...byVal.slice(-2)];
    const boxes = [];
    const free = (b, own) => b[0] > L && b[2] < W - 4 && b[1] > 2 &&
      boxes.every((o) => b[2] < o[0] || b[0] > o[2] || b[3] < o[1] || b[1] > o[3]) &&
      pts.every((q) => q === own || q.x < b[0] - 5 || q.x > b[2] + 5 || q.y < b[1] - 4 || q.y > b[3] + 4);     // not across other points
    for (const p of [...new Set(picks)]) {
      const name = p.e.album.length > 34 ? p.e.album.slice(0, 32) + "…" : p.e.album;
      const w = (name.length + 6) * 7.3;
      const tries = [[p.x + 12, p.y + 4, "start", [p.x + 8, p.y - 10, p.x + 12 + w, p.y + 8]],
        [p.x - 12, p.y + 4, "end", [p.x - 12 - w, p.y - 10, p.x - 8, p.y + 8]],
        [p.x, p.y - 13, "middle", [p.x - w / 2, p.y - 27, p.x + w / 2, p.y - 9]],
        [p.x, p.y + 22, "middle", [p.x - w / 2, p.y + 9, p.x + w / 2, p.y + 27]]];
      const spot = tries.find((t) => free(t[3], p));
      if (!spot) continue;
      boxes.push(spot[3]);
      svg.append(s("text", { class: "tl-label" + (p.on ? "" : " muted"), x: spot[0], y: spot[1], "text-anchor": spot[2] },
        name + " ", s("tspan", { class: "num" }, num(p.e.best.dynamics))));
    }

    // hover: crosshair, ring and a tooltip for the nearest album
    const cross = s("line", { class: "tl-cross", y1: T, y2: H - B, hidden: true });
    const ring = s("circle", { class: "tl-ring", r: 9, hidden: true });
    svg.append(cross, ring);
    const tip = h("div", { class: "tip charttip", hidden: true });
    let at = null;
    const hide = () => { at = null; cross.setAttribute("hidden", ""); ring.setAttribute("hidden", ""); tip.hidden = true; svg.classList.remove("hot"); };
    svg.addEventListener("mousemove", (ev) => {
      const r = svg.getBoundingClientRect();
      const mx = (ev.clientX - r.left) * (W / r.width), my = (ev.clientY - r.top) * (H / r.height);
      let best = null, far = 30 * 30;
      for (const p of pts) {
        const d = (p.x - mx) ** 2 + (p.y - my) ** 2 - (p.on ? 60 : 0);        // the highlighted artist wins a tie
        if (d < far) { far = d; best = p; }
      }
      if (!best) return hide();
      if (best === at) return;
      at = best;
      const e = best.e;
      cross.removeAttribute("hidden");
      ring.removeAttribute("hidden");
      cross.setAttribute("x1", best.x);
      cross.setAttribute("x2", best.x);
      ring.setAttribute("cx", best.x);
      ring.setAttribute("cy", best.y);
      svg.classList.toggle("hot", !!(e.query && e.query.title));
      tip.replaceChildren(h("b", {}, e.album), h("span", {}, `${e.artist} · ${e.year}`),
        h("span", { class: "v" }, `${num(e.best.dynamics)} dB` + (e.best.verdict ? ` · ${e.best.verdict}` : "")),
        h("span", {}, `Best edition: ${e.best.label} [${e.best.format}]`),
        h("span", {}, e.editions === 1 ? "Only edition compared"
          : `${e.editions} editions compared` + (e.lowest < e.best.dynamics - 0.05 ? ` · the least dynamic has ${num(e.lowest)} dB` : "")),
        e.query && e.query.title ? h("span", { class: "hint" }, "Click to open the comparison") : null);
      tip.hidden = false;
      const box = svg.getBoundingClientRect(), card = tip.parentNode.getBoundingClientRect(), t = tip.getBoundingClientRect();
      const px = box.left - card.left + best.x * (box.width / W), py = box.top - card.top + best.y * (box.height / H);
      tip.style.left = `${Math.round(px + 18 + t.width > card.width - 8 ? px - 18 - t.width : px + 18)}px`;
      tip.style.top = `${Math.round(Math.max(8, py - t.height - 14))}px`;
    });
    svg.addEventListener("mouseleave", hide);
    svg.addEventListener("click", () => { if (at) openFromTimeline(at.e); });

    return h("div", { class: "chartcard" },
      focus ? h("div", { class: "legend" }, h("span", {}, h("i", { class: "k1" }), focus), h("span", {}, h("i", { class: "k2" }), "Other albums")) : null,
      svg, tip);
  }

  function timelineTable(albums) {
    return h("div", { class: "table" },
      h("div", { class: "row tlrow head" }, h("span", { class: "num" }, "Year"), h("span"), h("span", {}, "Album"),
        h("span", { "data-tip": LIB_TIP.bestOn }, "Best edition"), h("span", { class: "num", "data-tip": TIP.dynamics }, "Dynamics"),
        h("span", { class: "num" }, "Editions"), h("span")),
      albums.map((e) => h("div", { class: "row tlrow" },
        h("span", { class: "num yr" }, e.year), cover(e.cover, e.album),
        h("div", { class: "name" }, h("div", { class: "line" }, h("b", { title: e.album }, e.album)), h("div", { class: "sub" }, e.artist)),
        edCell(e.best), dynCell(e.best), h("span", { class: "num" }, e.editions),
        e.query && e.query.title
          ? h("button", { class: "ghost sm", type: "button", onclick: () => openFromTimeline(e) }, "Compare") : h("span"))));
  }

  function drawTimeline(top) {
    const all = tl.data.albums;
    const y = window.scrollY;
    const nodes = [];
    const seg = (name, label) => h("button", { type: "button", class: tl.view === name ? "on" : "",
      onclick: () => { tl.view = name; drawTimeline(); } }, label);
    nodes.push(h("div", { class: "pagehead" }, h("h1", {}, "Timeline"),
      h("p", {}, `Dynamics of the best edition on ${svc()} for the ${all.length} album${all.length === 1 ? "" : "s"} you compared, by year of release.`),
      all.length >= 3 ? h("div", { class: "right" }, h("div", { class: "seg" }, seg("chart", "Chart"), seg("table", "Table"))) : null));
    if (all.length < 3) {
      nodes.push(notice("The timeline fills by itself: every album you compare or check in your library becomes a point. " +
        "It starts with three albums.", "empty"));
      state.screen = "timeline";
      main.replaceChildren(...nodes);
      return;
    }
    const counts = {};
    for (const e of all) counts[e.artist] = (counts[e.artist] || 0) + 1;
    const artists = Object.keys(counts).filter((a) => counts[a] >= 2).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b)).slice(0, 14);
    if (tl.artist && !counts[tl.artist]) tl.artist = null;
    const pill = (artist, label, n) => h("button", { type: "button", class: "pill" + (tl.artist === artist ? " on" : ""),
      "data-tip": artist ? "Highlights the albums of this artist." : null,
      onclick: () => { tl.artist = artist; drawTimeline(); } }, label, n ? h("span", {}, n) : null);
    if (artists.length) nodes.push(h("div", { class: "filters" }, pill(null, "All artists"), artists.map((a) => pill(a, a, counts[a]))));

    const shown = tl.view === "table" && tl.artist ? all.filter((e) => e.artist === tl.artist) : all;
    nodes.push(tl.view === "table" ? timelineTable(shown) : timelineChart(all, tl.artist));

    // one tile per decade, from the first to the last decade with an album
    const decade = (e) => Math.floor(e.year / 10) * 10;
    const eras = [];
    for (let d = Math.min(...all.map(decade)); d <= Math.max(...all.map(decade)); d += 10) eras.push([`${d}s`, (e) => decade(e) === d]);
    nodes.push(h("div", { class: "tiles eras" }, eras.map(([label, test]) => {
      const v = all.filter(test).map((e) => e.best.dynamics);
      const many = v.length >= 3;
      return h("div", { class: "tile", "data-tip": "Median dynamics of the best edition of all albums from this decade - " +
        "half of them are above, half below. Shown from three albums on; with so few albums it is a rough value." },
        h("small", {}, label),
        h("b", {}, many ? num(median(v)) : "–", many ? h("span", {}, "dB median") : null),
        h("em", {}, !v.length ? "No album yet" : many ? `${v.length} albums · ${num(Math.min(...v))} to ${num(Math.max(...v))} dB`
          : `${v.length} album${v.length === 1 ? "" : "s"} · too few for a median`));
    })));
    nodes.push(h("div", { class: "foot" },
      h("p", {}, "Each point is an album you compared or checked in your library, placed at the year it first came out. " +
        `The value is the dynamics of the best edition available on ${svc()} today - not of the mastering of that year. ` +
        "An old album that only exists as a loud remaster stands at its original year with the remaster's value, " +
        "so the chart shows the loudness war more mildly than it was."),
      h("p", {}, "The albums are your selection, not a sample of what was released.")));
    state.screen = "timeline";
    main.replaceChildren(...nodes);
    window.scrollTo(0, top ? 0 : y);
  }

  let tlResize = 0;
  window.addEventListener("resize", () => {
    clearTimeout(tlResize);
    tlResize = setTimeout(() => { if (state.screen === "timeline" && tl.view === "chart") drawTimeline(); }, 150);
  });

  // -------------------------------------------------------------- login

  const dialog = document.getElementById("login");
  const tokenInput = document.getElementById("token");
  const loginError = document.getElementById("login-error");
  const loginState = document.getElementById("login-state");
  const account = document.getElementById("account");

  function paintStatus() {
    const s = state.status || {};
    // status light: green = connected, amber = Qobuz is slowing requests down, red = not connected
    const slow = !!s.connected && s.throttled > 0;
    const minutes = Math.max(1, Math.ceil((s.throttled || 0) / 60));
    account.classList.toggle("on", !!s.connected && !slow);
    account.classList.toggle("slow", slow);
    account.lastElementChild.textContent = !s.connected ? `${svc()} disconnected` : slow ? `${svc()} throttled` : `${svc()} connected`;
    account.title = !s.connected ? `Not connected to ${svc()}. Click to connect.`
      : slow ? `${svc()} slowed requests down. Back to normal in about ${minutes} minute${minutes === 1 ? "" : "s"}, if no further request is slowed down.`
      : `Connected to ${svc()}.`;
    document.getElementById("ver").textContent = s.version ? `Version ${s.version}` : "";
    document.getElementById("demo-banner").hidden = !s.demo;
    document.getElementById("stale-banner").hidden = !s.stale;
  }

  // The status also tells whether the program files changed since the app was started.
  function refreshStatus() {
    api("/api/status").then((s) => {
      state.status = Object.assign(s, { connected: s.connected && (state.status ? state.status.connected : true) });
      paintStatus();
    }).catch(() => {});
  }

  // How to get the token, per service. The steps are the same idea twice: the web player sends
  // the token with every request, and the browser's developer tools show it.
  const LOGIN = {
    Qobuz: {
      why: "Qobuz no longer lets third-party apps sign in with e-mail and password. DynamicsWar needs the " +
        "access token from the Qobuz web player instead, once:",
      steps: () => [
        ["Open ", h("b", {}, "play.qobuz.com"), " in your browser and sign in."],
        ["Press ", h("kbd", {}, "F12"), ", open the ", h("b", {}, "Network"), " tab and type ", h("code", {}, "api.json"), " into the filter."],
        ["Click any album in the player, then select one of the requests in the list."],
        ["Under ", h("b", {}, "Request Headers"), ", copy the value of ", h("code", {}, "X-User-Auth-Token"), "."],
      ],
      fine: "The token stays on this computer, in your user profile. It works like a password for your Qobuz account: do not share it.",
    },
    Tidal: {
      why: "DynamicsWar reads Tidal with the access token of the Tidal web player. Tidal's tokens expire after " +
        "a while; DynamicsWar asks for a new one then.",
      steps: () => [
        ["Open ", h("b", {}, "listen.tidal.com"), " in your browser and sign in."],
        ["Press ", h("kbd", {}, "F12"), ", open the ", h("b", {}, "Network"), " tab and type ", h("code", {}, "api.tidal.com"), " into the filter."],
        ["Click any album in the player, then select one of the requests in the list."],
        ["Under ", h("b", {}, "Request Headers"), ", copy the value of ", h("code", {}, "Authorization"), " (it starts with “Bearer”)."],
      ],
      fine: "The token stays on this computer, in your user profile. It works like a password for your Tidal account: do not share it. " +
        "DynamicsWar reads loudness and format with the request the player sends before it plays a track - one per track, " +
        "at most two per second. No audio is loaded.",
    },
  };

  // service: an entry of the settings (name, label, connected ...) when the dialog is opened for
  // a service other than the one in use; without it the dialog is for the service in use
  function openLogin(message, service) {
    const s = service || state.status || {};
    const label = service ? service.label : svc();
    const how = LOGIN[label] || LOGIN.Qobuz;
    dialog.dataset.service = service ? service.name : "";
    document.getElementById("login-title").textContent = `Connect your ${label} account`;
    document.getElementById("login-why").textContent = how.why;
    document.getElementById("login-steps").replaceChildren(...how.steps().map((parts) => h("li", {}, ...parts)));
    document.getElementById("login-fine").textContent = how.fine;
    loginError.hidden = !message;
    loginError.textContent = message || "";
    loginState.hidden = !s.connected || !!message;
    loginState.textContent = service && !service.active ? "A token is stored." : "A token is stored and in use.";
    document.getElementById("login-disconnect").hidden = !s.connected || !!(state.status || {}).demo;
    tokenInput.value = "";
    if (!dialog.open) dialog.showModal();
    tokenInput.focus();
  }

  function needLogin(retry) {
    state.pending = retry;
    if (state.status) state.status.connected = false;
    paintStatus();
    api("/api/status").then((s) => { state.status = Object.assign(s, { connected: false }); })
      .catch(() => {}).finally(() => openLogin(`${svc()} asks you to sign in: the token is missing or has expired.`));
  }

  async function submitLogin(body) {
    const button = document.getElementById("login-submit");
    button.disabled = true;
    try {
      if (dialog.dataset.service) body.service = dialog.dataset.service;
      state.status = await api("/api/login", body);
      paintStatus();
      dialog.close();
      const retry = state.pending;
      state.pending = null;
      if (retry) retry();
      else if (state.screen === "settings") showSettings();
    } catch (e) {
      loginError.hidden = false;
      loginError.textContent = e.message;
    } finally {
      button.disabled = false;
    }
  }

  document.getElementById("login-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitLogin({ token: tokenInput.value });
  });
  document.getElementById("login-cancel").addEventListener("click", () => dialog.close());
  document.getElementById("login-disconnect").addEventListener("click", async () => {
    state.status = await api("/api/logout", dialog.dataset.service ? { service: dialog.dataset.service } : {});
    paintStatus();
    dialog.close();
    if (state.screen === "settings") showSettings();
  });
  account.addEventListener("click", () => showSettings());
  document.getElementById("nav-settings").addEventListener("click", () => showSettings());

  // ----------------------------------------------------------- settings

  const SERVICE_FACTS = {
    qobuz: { loudness: "Comes with the album data. A comparison sends around 30 requests and takes a few seconds." },
    tidal: { loudness: "Read once per track, two requests per second. The first comparison of an album takes up to a minute, every later one a few seconds." },
  };

  function tokenText(s) {
    if (!s.connected) return s.expires && s.expires * 1000 < Date.now() ? "Expired - copy a new one from the web player." : "None stored.";
    if (!s.expires) return "Stored on this computer.";
    const left = s.expires * 1000 - Date.now();
    const when = new Date(s.expires * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    return `Stored on this computer. Valid until ${when}` + (left < 3600e3 ? ` - less than an hour left.` : ".");
  }

  function serviceCard(s, d) {
    const paused = s.connected && s.blocked > 0;
    const stateText = !s.connected ? "Not connected" : paused ? `Paused for ${Math.max(1, Math.ceil(s.blocked / 60))} min` : "Connected";
    const fact = (label, value) => [h("dt", {}, label), h("dd", {}, value)];
    const works = ["Search", "Compare", s.library ? "Library" : null, "Timeline"].filter(Boolean).join(" \u00b7 ")
      + (s.library ? "" : " \u2014 the library is not built for this service yet");
    const stored = [`${s.compared} album${s.compared === 1 ? "" : "s"} compared`,
      s.stored_tracks === null ? null : `loudness of ${s.stored_tracks} track${s.stored_tracks === 1 ? "" : "s"} stored`].filter(Boolean).join(" \u00b7 ");
    const use = async () => {
      try {
        state.status = await api("/api/service", { service: s.name });
        state.groups = null; state.query = ""; lib.data = null; tl.data = null; lib.sel.clear();
        input.value = "";
        paintStatus();
        showSettings();
      } catch (e) { settings.error = e.message; drawSettings(); }
    };
    return h("div", { class: "service" + (s.active ? " active" : "") },
      h("div", { class: "service-head" },
        h("h3", {}, s.label),
        s.active ? h("span", { class: "tag gold" }, "In use") : null,
        h("span", { class: "state" + (!s.connected ? "" : paused ? " slow" : " on") }, h("i"), stateText)),
      h("dl", { class: "facts" },
        fact("Token", tokenText(s)),
        fact("Works for", works),
        fact("Loudness", (SERVICE_FACTS[s.name] || {}).loudness || ""),
        fact("So far", stored)),
      h("div", { class: "actions" },
        s.active ? null : h("button", { class: "primary sm", type: "button", disabled: !s.connected || d.busy || d.demo, onclick: use,
          "data-tip": !s.connected ? "Connect the account first." : d.busy ? "A comparison or library check is running." : null },
          `Use ${s.label}`),
        h("button", { class: "ghost sm", type: "button", disabled: d.demo, onclick: () => openLogin(null, s) }, s.connected ? "New token" : "Connect"),
        s.connected ? h("button", { class: "ghost sm", type: "button", disabled: d.demo, onclick: async () => {
          state.status = await api("/api/logout", { service: s.name });
          paintStatus();
          showSettings();
        } }, "Disconnect") : null));
  }

  const settings = { data: null, error: null };

  function drawSettings() {
    const d = settings.data;
    const row = (label, value) => [h("dt", {}, label), h("dd", {}, value)];
    const nodes = [
      h("div", { class: "section-head" }, h("h1", {}, "Settings")),
      settings.error ? h("p", { class: "problem" }, settings.error) : null,
      h("section", { class: "group first" },
        h("header", {}, h("h2", {}, "Streaming service"),
          h("span", {}, "Search, comparison, library and timeline work with the service in use.")),
        h("div", { class: "services" }, d.services.map((s) => serviceCard(s, d)))),
      h("section", { class: "group" },
        h("header", {}, h("h2", {}, "On this computer")),
        h("dl", { class: "facts wide" },
          row("Settings and tokens", [h("code", {}, d.folder), h("br"),
            "A token works like a password for the account. It is sent to its own service only and never leaves this computer otherwise."]),
          row("Reports", "Every comparison writes a file into the debug folder next to the program: what was asked, how the service answered and what was decided."),
          row("Version", (state.status && state.status.version) || ""))),
    ];
    show(...nodes.filter(Boolean));
    state.screen = "settings";
  }

  async function showSettings() {
    const seq = ++state.seq;
    setNav("settings");
    state.from = null;
    if (!settings.data) show(notice("Reading the settings \u2026"));
    try {
      const d = await api("/api/settings");
      if (seq !== state.seq) return;
      settings.data = d;
      drawSettings();
      settings.error = null;
    } catch (e) {
      if (seq !== state.seq) return;
      show(notice(e.message, "error"));
    }
  }

  // ------------------------------------------------------------ sidebar

  const sideToggle = document.getElementById("side-toggle");
  function paintSide(collapsed) {
    document.body.classList.toggle("side-collapsed", collapsed);
    sideToggle.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
    sideToggle.setAttribute("aria-label", sideToggle.title);
  }
  sideToggle.addEventListener("click", () => {
    const collapsed = !document.body.classList.contains("side-collapsed");
    paintSide(collapsed);
    try { localStorage.setItem("dw-side", collapsed ? "collapsed" : "open"); } catch (e) { /* not kept */ }
  });
  try { paintSide(localStorage.getItem("dw-side") === "collapsed"); } catch (e) { /* default: open */ }

  // -------------------------------------------------------------- start

  setInterval(() => { api("/api/ping").catch(() => {}); }, 5000);
  setInterval(refreshStatus, 30000);          // the status light follows a pause that ends by itself
  window.addEventListener("pagehide", () => {
    navigator.sendBeacon("/api/bye?s=" + encodeURIComponent(SESSION));
  });

  showHome();
  api("/api/status").then((s) => {
    state.status = s;
    paintStatus();
    if (!s.connected) openLogin();
    else input.focus();
  }).catch((e) => show(notice(e.message, "error")));
})();
