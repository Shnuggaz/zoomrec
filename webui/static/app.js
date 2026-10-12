(function () {
  "use strict";

  const cfg = window.ZOOMREC;
  const $ = (id) => document.getElementById(id);
  const store = {
    get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch (e) { return null; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* ignore */ } },
  };
  const posKey = (name) => `zoomrec-pos:${name}`;
  const seenKey = (name) => `zoomrec-seen:${name}`;

  // --- Formatting ------------------------------------------------------------
  const pretty = (s) => (s || "").replace(/_/g, " ");
  const clock = (iso) => iso ? new Date(iso).toLocaleTimeString("de-AT", { hour: "2-digit", minute: "2-digit" }) : "";
  const dayTime = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    const today = new Date();
    const tomorrow = new Date(); tomorrow.setDate(today.getDate() + 1);
    const time = clock(iso);
    if (d.toDateString() === today.toDateString()) return `Heute, ${time}`;
    if (d.toDateString() === tomorrow.toDateString()) return `Morgen, ${time}`;
    return d.toLocaleDateString("de-AT", { weekday: "long", day: "numeric", month: "numeric" }) + `, ${time}`;
  };
  const span = (ms) => {
    const minutes = Math.max(0, Math.round(ms / 60000));
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} h ${minutes % 60} min`;
    const days = Math.floor(hours / 24);
    return `${days} ${days === 1 ? "Tag" : "Tagen"} ${hours % 24} h`;
  };

  // --- Recorder status (live pill on every page, details on the start page) --
  const hero = $("status");
  let lastState = hero ? hero.dataset.state : null;
  let lastStatus = null;

  function renderPill(s, state) {
    const pill = $("live-pill");
    if (!pill) return;
    const active = state === "recording" || state === "joining";
    pill.hidden = !active || Boolean(hero);
    pill.classList.toggle("wait", state === "joining");
    $("live-pill-text").textContent = state === "recording" ? `REC · ${pretty(s.meeting)}` : `Tritt bei · ${pretty(s.meeting)}`;
  }

  function renderHero(s, state) {
    hero.dataset.state = state;
    const meeting = pretty(s.meeting);
    const labels = {
      recording: "Aufnahme läuft", joining: "Tritt bei", finishing: "Wird gespeichert",
      idle: "Bereit", stale: "Recorder nicht erreichbar", unknown: "Recorder nicht erreichbar",
    };
    $("state-label").textContent = labels[state] || state;

    const progress = $("rec-progress");
    progress.hidden = state !== "recording";
    if (state === "recording") {
      $("status-title").textContent = meeting;
      const start = new Date(s.recording_since).getTime();
      const end = new Date(s.planned_end).getTime();
      $("status-detail").textContent = `Läuft seit ${span(Date.now() - start)} · endet, wenn die Vorlesung vorbei ist`;
      $("rec-progress-fill").style.width = `${Math.min(100, Math.max(1, (Date.now() - start) * 100 / (end - start)))}%`;
      $("rec-progress-start").textContent = clock(s.recording_since);
      $("rec-progress-end").textContent = `spätestens ${clock(s.planned_end)}`;
    } else if (state === "joining") {
      $("status-title").textContent = meeting;
      $("status-detail").textContent = `Wartet seit ${clock(s.since)} auf das Meeting, evtl. im Warteraum`;
    } else if (state === "finishing") {
      $("status-title").textContent = meeting;
      $("status-detail").textContent = "Die Aufnahme wird gespeichert und erscheint gleich in der Bibliothek.";
    } else if (state === "idle") {
      $("status-title").textContent = "Keine Aufnahme aktiv";
      $("status-detail").textContent = s.last_recording_at ? `Letzte Aufnahme: ${dayTime(s.last_recording_at)}` : "Der Recorder wartet auf die nächste Vorlesung.";
    } else {
      $("status-title").textContent = "Keine Verbindung zum Recorder";
      $("status-detail").textContent = s.updated ? `Letztes Lebenszeichen: ${dayTime(s.updated)}` : "Läuft der Container „zoomrec“?";
    }

    $("next-meeting").textContent = s.next_meeting ? pretty(s.next_meeting) : "Nichts geplant";
    $("next-countdown").textContent = s.next_meeting_at
      ? `${dayTime(s.next_meeting_at)} · in ${span(new Date(s.next_meeting_at) - Date.now())}` : "";

    if (s.disk_free) {
      $("disk-free").textContent = `${s.disk_free} von ${s.disk_total}`;
      $("disk-meter").style.width = `${100 - s.disk_free_percent}%`;
      $("disk-meter").parentElement.classList.toggle("low", s.disk_free_percent < 10);
    }

    $("last-error-box").hidden = !s.last_error;
    if (s.last_error) {
      $("last-error").textContent = s.last_error;
      $("last-error-time").textContent = dayTime(s.last_error_at);
      const shot = $("last-error-shot");
      shot.hidden = !s.error_screenshot;
      if (s.error_screenshot) shot.href = `${cfg.errorShotUrl}?t=${encodeURIComponent(s.last_error_at || "")}`;
    }

    const preview = $("preview");
    const live = Boolean(s.preview);
    preview.hidden = !live;
    $("live-tag").hidden = state !== "recording" || !live;
    $("screen-idle").hidden = live;
    if (live) preview.src = `${cfg.previewUrl}?t=${Date.now()}`;
  }

  async function poll() {
    try {
      const response = await fetch(cfg.statusUrl, { cache: "no-store" });
      if (response.status === 401) { window.location.reload(); return; }
      const s = await response.json();
      const state = s.stale ? "stale" : s.state;
      // A recording was just saved or started: reload the start page so the library is current
      if (hero && ((["recording", "finishing"].includes(lastState) && state === "idle") ||
                   (lastState !== "recording" && state === "recording" && lastState !== null))) {
        window.location.reload();
        return;
      }
      lastState = state;
      lastStatus = s;
      renderPill(s, state);
      if (hero) renderHero(s, state);
    } catch (e) {
      if (hero) renderHero({}, "unknown");
    }
  }

  if (cfg && cfg.statusUrl) {
    poll();
    setInterval(poll, hero ? 5000 : 15000);
  }

  // --- Library: watched progress, "new" badges, search and course filter ---
  document.querySelectorAll(".card-rec[data-name], .series a[data-name]").forEach((el) => {
    const name = el.dataset.name;
    const pos = store.get(posKey(name));
    const bar = el.querySelector(".watched");
    if (bar && pos && pos.d) {
      bar.classList.add("show");
      bar.firstElementChild.style.width = `${Math.min(100, pos.t * 100 / pos.d)}%`;
    }
    const badge = el.querySelector(".new-tag");
    if (badge && !el.classList.contains("recording") && !store.get(seenKey(name)) && !pos) badge.hidden = false;
  });

  const search = $("search");
  const chips = $("course-chips");
  let course = "";
  function applyFilter() {
    const q = search ? search.value.trim().toLowerCase() : "";
    let visible = 0;
    document.querySelectorAll("#library .card-rec").forEach((card) => {
      const show = (!course || card.dataset.course === course) && (!q || card.dataset.search.includes(q));
      card.hidden = !show;
      if (show) visible++;
    });
    const none = $("no-results");
    if (none) none.hidden = visible > 0;
  }
  if (search) search.addEventListener("input", applyFilter);
  if (chips) {
    chips.addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      chips.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c === chip));
      course = chip.dataset.course;
      applyFilter();
    });
  }

  // --- Player ---------------------------------------------------------------
  const video = $("player");
  if (video) {
    const name = video.dataset.name;
    store.set(seenKey(name), true);
    const speed = $("speed");
    const setRate = (rate) => {
      video.playbackRate = rate;
      speed.querySelectorAll("button").forEach((b) => b.classList.toggle("active", parseFloat(b.dataset.rate) === rate));
      store.set("zoomrec-rate", rate);
    };
    speed.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) setRate(parseFloat(b.dataset.rate));
    });
    video.addEventListener("loadedmetadata", () => {
      const pos = store.get(posKey(name));
      if (pos && pos.t > 5 && pos.t < video.duration - 10) video.currentTime = pos.t;
      setRate(store.get("zoomrec-rate") || 1);
    });
    let lastSave = 0;
    video.addEventListener("timeupdate", () => {
      if (Date.now() - lastSave < 2000 || !video.duration) return;
      lastSave = Date.now();
      store.set(posKey(name), { t: video.currentTime, d: video.duration });
    });
    video.addEventListener("ratechange", () => {
      speed.querySelectorAll("button").forEach((b) => b.classList.toggle("active", parseFloat(b.dataset.rate) === video.playbackRate));
    });
    document.addEventListener("keydown", (e) => {
      if (["INPUT", "TEXTAREA"].includes(e.target.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.target === video && [" ", "ArrowLeft", "ArrowRight"].includes(e.key)) return; // native handling
      if (e.key === " " || e.key === "k") { e.preventDefault(); video.paused ? video.play() : video.pause(); }
      else if (e.key === "ArrowRight" || e.key === "l") video.currentTime = Math.min(video.duration, video.currentTime + 10);
      else if (e.key === "ArrowLeft" || e.key === "j") video.currentTime = Math.max(0, video.currentTime - 10);
      else if (e.key === "f") {
        if (document.fullscreenElement) document.exitFullscreen();
        else if (video.requestFullscreen) video.requestFullscreen();
        else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
      }
    });
  }

  // --- Meeting form: duration presets and end time ------------------------
  const form = $("meeting-form");
  if (form) {
    const time = $("time");
    const duration = $("duration");
    const summary = $("time-summary");
    const update = () => {
      const [h, m] = (time.value || "").split(":").map(Number);
      const d = parseInt(duration.value, 10);
      if (Number.isNaN(h) || Number.isNaN(d)) { summary.textContent = ""; return; }
      const end = h * 60 + m + d;
      const pad = (n) => String(n).padStart(2, "0");
      summary.textContent = `${time.value}–${pad(Math.floor(end / 60) % 24)}:${pad(end % 60)} · Beitritt um ${pad(Math.floor((h * 60 + m - 1 + 1440) % 1440 / 60))}:${pad((m + 59) % 60)}`;
      form.querySelectorAll("[data-duration]").forEach((b) => b.classList.toggle("active", parseInt(b.dataset.duration, 10) === d));
    };
    form.querySelectorAll("[data-duration]").forEach((b) => b.addEventListener("click", () => { duration.value = b.dataset.duration; update(); }));
    time.addEventListener("input", update);
    duration.addEventListener("input", update);
    update();
  }
})();
