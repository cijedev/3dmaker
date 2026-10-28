(() => {
  "use strict";

  /** Base path for API calls — works at / and under /3dmaker/ */
  function apiBase() {
    const path = window.location.pathname || "/";
    // If served under .../3dmaker/ or .../3dmaker/index.html
    const m = path.match(/^(.*?\/3dmaker)(?:\/|$)/);
    if (m) return m[1];
    // StaticFiles at root: API is sibling of /
    // If path ends with /static-ish pages, strip filename
    if (path.endsWith(".html")) {
      return path.replace(/\/[^/]+$/, "") || "";
    }
    // Trailing directory
    if (path.endsWith("/")) return path.slice(0, -1);
    // Bare file at root → ""
    const last = path.lastIndexOf("/");
    if (last <= 0) return "";
    return path.slice(0, last);
  }

  const BASE = apiBase();

  const promptEl = document.getElementById("prompt");
  const charCount = document.getElementById("charCount");
  const generateBtn = document.getElementById("generateBtn");
  const statusCard = document.getElementById("statusCard");
  const statusPill = document.getElementById("statusPill");
  const statusMsg = document.getElementById("statusMsg");
  const jobIdEl = document.getElementById("jobId");
  const progress = document.getElementById("progress");
  const downloadBtn = document.getElementById("downloadBtn");
  const resetBtn = document.getElementById("resetBtn");
  const progressWrap = document.getElementById("progressWrap");
  const progressBar = document.getElementById("progressBar");
  const progressPct = document.getElementById("progressPct");
  const elapsedEl = document.getElementById("elapsed");
  const viewBtn = document.getElementById("viewBtn");
  const modal = document.getElementById("viewerModal");
  const modalDownload = document.getElementById("modalDownload");
  const LS_KEY = "cijes3dgen.currentJob";

  let pollTimer = null;
  let tickTimer = null;
  let currentJobId = null;
  let startedAt = null;
  let failCount = 0;
  let shownModalFor = null;
  let pollSeq = 0;           // bumps on every new job/reset; stale responses are dropped
  let suppressModal = false; // true when restoring an already-finished job on page load
  const MAX_AGE_MS = 24 * 60 * 60 * 1000;

  function fmt(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }
  // No fake percentage: the server doesn't report real progress, so show an
  // indeterminate (moving) bar plus elapsed time until the job is ready.
  function tick() {
    if (!startedAt) return;
    elapsedEl.textContent = `${fmt(Date.now() - startedAt)} elapsed`;
  }
  function startTick() {
    progress.classList.add("indet");
    progressPct.textContent = "Working…";
    if (!tickTimer) tickTimer = setInterval(tick, 1000);
    tick();
  }
  function stopTick() { clearInterval(tickTimer); tickTimer = null; progress.classList.remove("indet"); }

  function saveJob(id) { localStorage.setItem(LS_KEY, JSON.stringify({ id, ts: Date.now() })); }
  function loadJob() {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return null;
    try { const o = JSON.parse(raw); if (o && o.id) return o; } catch (_) { /* legacy plain id */ }
    return { id: raw, ts: 0 };
  }

  function openModal(id) {
    const url = `${BASE}/api/jobs/${id}/download`;
    modalDownload.href = url;
    modalDownload.setAttribute("download", "");
    modal.hidden = false;
    const go = () => window.show3D ? window.show3D(url + "?v=" + Date.now()) : setTimeout(go, 200);
    go();
  }
  function closeModal() { modal.hidden = true; if (window.stop3D) window.stop3D(); }
  document.getElementById("viewerClose").onclick = closeModal;
  document.getElementById("viewerClose2").onclick = closeModal;
  modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modal.hidden) closeModal(); });
  viewBtn.addEventListener("click", () => currentJobId && openModal(currentJobId));

  function updateCount() {
    const n = promptEl.value.length;
    charCount.textContent = `${n} / 500`;
  }

  promptEl.addEventListener("input", updateCount);
  updateCount();

  function setStatus(status, message) {
    statusPill.textContent = status;
    statusPill.className = "status-pill " + status;
    statusMsg.textContent = message;
    const busy = status === "queued" || status === "generating";
    progressWrap.hidden = !busy;
    if (busy) startTick(); else stopTick();
    if (!busy) { progressBar.style.width = "100%"; progressPct.textContent = "100%"; }
    downloadBtn.hidden = status !== "ready";
    viewBtn.hidden = status !== "ready";
  }

  function stopPoll() {
    clearTimeout(pollTimer);
    pollTimer = null;
  }

  async function pollJob(id, seq) {
    let data = null;
    try {
      const res = await fetch(`${BASE}/api/jobs/${encodeURIComponent(id)}?t=${Date.now()}`, { cache: "no-store" });
      if (seq !== pollSeq) return; // a newer job took over
      if (res.status === 404) {
        localStorage.removeItem(LS_KEY);
        setStatus("failed", "That job no longer exists.");
        generateBtn.disabled = false;
        return;
      }
      if (!res.ok) throw new Error(`Status ${res.status}`);
      data = await res.json();
    } catch (err) {
      if (seq !== pollSeq) return;
      failCount++;
      if (failCount > 20) {
        setStatus("failed", "Lost connection while checking job status. Refresh to try again.");
        generateBtn.disabled = false;
        return;
      }
      statusMsg.textContent = "Connection hiccup — retrying…";
    }
    if (seq !== pollSeq) return;
    if (data) {
      if (data.id !== id) return;
      failCount = 0;
      applyJob(data);
      if (data.status === "ready" || data.status === "failed") return; // done, no more polling
    }
    // Back off for long custom waits so idle tabs don't hammer the server
    const delay = data && data.awaiting_custom ? 5000 : 2000;
    pollTimer = setTimeout(() => pollJob(id, seq), delay);
  }

  function applyJob(data) {
    currentJobId = data.id;
    jobIdEl.textContent = data.id;
    statusCard.hidden = false;

    if (!startedAt) startedAt = Date.parse(data.created_at) || Date.now();
    if (data.status === "ready") {
      setStatus("ready", "Ready! Preview it in 3D or download your STL.");
      downloadBtn.href = `${BASE}/api/jobs/${data.id}/download`;
      downloadBtn.setAttribute("download", "");
      generateBtn.disabled = false;
      localStorage.removeItem(LS_KEY); // finished: don't hijack the next visit
      if (!suppressModal && shownModalFor !== data.id) { shownModalFor = data.id; openModal(data.id); }
      suppressModal = false;
    } else if (data.status === "failed") {
      setStatus("failed", data.error || "Generation failed.");
      localStorage.removeItem(LS_KEY);
      generateBtn.disabled = false;
    } else if (data.awaiting_custom) {
      setStatus("queued", "Waiting for a custom model — this isn't one of the instant shapes, so it can take a while (possibly hours). You can leave this page open or come back later; it will pick up where it left off.");
      generateBtn.disabled = false; // allow starting something else
    } else if (data.status === "generating") {
      setStatus("generating", "Generating your model…");
    } else {
      setStatus("queued", "In the queue — your STL will appear here when it's ready.");
    }
  }

  function startPoll(id) {
    stopPoll();
    failCount = 0;
    const seq = ++pollSeq;
    pollJob(id, seq);
  }

  // Resume a job after refresh (only recent, unfinished ones)
  const saved = loadJob();
  if (saved && saved.ts && Date.now() - saved.ts < MAX_AGE_MS) {
    statusCard.hidden = false;
    jobIdEl.textContent = saved.id;
    setStatus("queued", "Resuming your job…");
    generateBtn.disabled = true;
    suppressModal = true; // if it already finished, show the result but don't pop the viewer
    startPoll(saved.id);
  } else if (saved) {
    localStorage.removeItem(LS_KEY); // stale or legacy entry
  }

  generateBtn.addEventListener("click", async () => {
    const prompt = promptEl.value.trim();
    if (!prompt) {
      promptEl.focus();
      return;
    }
    if (prompt.length > 500) {
      setStatus("failed", "Prompt must be 500 characters or fewer.");
      statusCard.hidden = false;
      return;
    }

    generateBtn.disabled = true;
    downloadBtn.hidden = true;
    stopPoll();
    pollSeq++; // invalidate any in-flight poll for the old job
    localStorage.removeItem(LS_KEY);
    suppressModal = false;
    currentJobId = null;
    progressBar.style.width = "0%";
    startedAt = Date.now();
    shownModalFor = null;
    statusCard.hidden = false;
    setStatus("queued", "Submitting…");
    jobIdEl.textContent = "…";

    try {
      const res = await fetch(`${BASE}/api/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || `Request failed (${res.status})`);
      }
      startedAt = null;
      saveJob(data.id);
      applyJob(data);
      if (data.status !== "ready" && data.status !== "failed") startPoll(data.id);
    } catch (err) {
      setStatus("failed", err.message || "Could not create job.");
      generateBtn.disabled = false;
    }
  });

  resetBtn.addEventListener("click", () => {
    stopPoll();
    pollSeq++;
    stopTick();
    closeModal();
    viewBtn.hidden = true;
    localStorage.removeItem(LS_KEY);
    currentJobId = null;
    startedAt = null;
    statusCard.hidden = true;
    downloadBtn.hidden = true;
    generateBtn.disabled = false;
    promptEl.focus();
  });

  promptEl.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      generateBtn.click();
    }
  });
})();
