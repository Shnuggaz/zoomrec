(function () {
  "use strict";

  // --- Recorder status on the start page ---------------------------------
  const cfg = window.ZOOMREC;
  const statusBox = document.getElementById("status");

  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString("de-AT", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function fmtClock(iso) {
    return iso ? new Date(iso).toLocaleTimeString("de-AT", { hour: "2-digit", minute: "2-digit" }) : "";
  }

  function elapsed(iso) {
    if (!iso) return "";
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
  }

  function render(s) {
    const title = document.getElementById("status-title");
    const detail = document.getElementById("status-detail");
    const preview = document.getElementById("preview");
    let state = s.state;
    if (s.stale) state = "stale";
    statusBox.dataset.state = state;

    const meeting = (s.meeting || "").replace(/_/g, " ");
    if (state === "recording") {
      title.textContent = `Aufnahme läuft: ${meeting}`;
      detail.textContent = `seit ${fmtClock(s.recording_since)} (${elapsed(s.recording_since)}), spätestens bis ${fmtClock(s.planned_end)}`;
    } else if (state === "joining") {
      title.textContent = `Tritt bei: ${meeting}`;
      detail.textContent = `Wartet seit ${fmtClock(s.since)} auf das Meeting (evtl. im Warteraum)`;
    } else if (state === "finishing") {
      title.textContent = `Aufnahme wird gespeichert: ${meeting}`;
      detail.textContent = "";
    } else if (state === "idle") {
      title.textContent = "Bereit, keine Aufnahme aktiv";
      detail.textContent = s.last_recording ? `Letzte Aufnahme: ${fmtTime(s.last_recording_at)}` : "";
    } else {
      title.textContent = "Recorder nicht erreichbar";
      detail.textContent = s.updated ? `Letztes Lebenszeichen: ${fmtTime(s.updated)}` : "Der Recorder-Container läuft nicht oder hat noch keinen Status geschrieben.";
    }

    document.getElementById("next-meeting").textContent =
      s.next_meeting ? `${s.next_meeting.replace(/_/g, " ")} – ${fmtTime(s.next_meeting_at)}` : "–";
    document.getElementById("disk-free").textContent = `${s.disk_free} (${s.disk_free_percent} %)`;

    const errBox = document.getElementById("last-error-box");
    errBox.hidden = !s.last_error;
    if (s.last_error) document.getElementById("last-error").textContent = `${fmtTime(s.last_error_at)}: ${s.last_error}`;

    preview.hidden = !s.preview;
    if (s.preview) preview.src = `${cfg.previewUrl}?t=${Date.now()}`;
  }

  let lastState = statusBox ? statusBox.dataset.state : null;

  async function poll() {
    try {
      const response = await fetch(cfg.statusUrl, { cache: "no-store" });
      if (response.status === 401) { window.location.reload(); return; }
      const s = await response.json();
      // A recording was just saved: reload so it shows up in the library
      if ((lastState === "recording" || lastState === "finishing") && s.state === "idle") {
        window.location.reload();
        return;
      }
      lastState = s.state;
      render(s);
    } catch (e) {
      render({ state: "unknown", stale: true });
    }
  }

  if (statusBox && cfg) {
    poll();
    setInterval(poll, 5000);
  }

  // --- Course filter -----------------------------------------------------
  const filter = document.getElementById("course-filter");
  if (filter) {
    filter.addEventListener("change", () => {
      document.querySelectorAll(".library li").forEach((li) => {
        li.hidden = filter.value !== "" && li.dataset.course !== filter.value;
      });
    });
  }

  // --- Player --------------------------------------------------------------
  const video = document.querySelector("video.player");
  if (video) {
    const key = "zoomrec-pos:" + video.getAttribute("src");
    // Resume where the recording was left
    video.addEventListener("loadedmetadata", () => {
      try {
        const pos = parseFloat(localStorage.getItem(key) || "0");
        if (pos > 5 && pos < video.duration - 5) video.currentTime = pos;
      } catch (e) { /* storage unavailable */ }
    });
    video.addEventListener("timeupdate", () => {
      try { localStorage.setItem(key, String(video.currentTime)); } catch (e) { /* ignore */ }
    });
    document.querySelectorAll("button.speed").forEach((b) => {
      b.addEventListener("click", () => { video.playbackRate = parseFloat(b.dataset.rate); });
    });
    document.addEventListener("keydown", (e) => {
      if (e.target.tagName === "INPUT" || e.target === video) return;
      if (e.key === " ") { e.preventDefault(); video.paused ? video.play() : video.pause(); }
      if (e.key === "ArrowRight") video.currentTime += 10;
      if (e.key === "ArrowLeft") video.currentTime -= 10;
    });
  }
})();
