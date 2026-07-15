/* AI Assistant — floating orb + glass chat panel. Reuses app.js globals:
   api, toast, state, switchView, openWorkspace, closeWorkspace, openFolder,
   loadTheater, renderTheater, loadFolders, loadPlaylists. */
(function () {
  const $ = (s) => document.querySelector(s);
  const orb = $("#ai-orb"), panel = $("#ai-panel"), msgs = $("#ai-messages"),
        input = $("#ai-input"), sendBtn = $("#ai-send");
  let history = [];          // [{role, text}] kept in-session only
  let busy = false;

  function addMsg(role, text, trace) {
    const el = document.createElement("div");
    el.className = `ai-msg ${role}`;
    el.textContent = text;
    if (trace && trace.length) {
      const t = document.createElement("span");
      t.className = "ai-trace";
      t.textContent = "ran: " + trace.join(" · ");
      el.appendChild(t);
    }
    msgs.appendChild(el);
    msgs.scrollTop = msgs.scrollHeight;
    return el;
  }

  async function checkAvailability() {
    try {
      const cfg = await api.get("/api/ai/config");
      orb.classList.toggle("hidden", !(cfg.available && cfg.enabled));
    } catch { orb.classList.add("hidden"); }
  }

  function buildContext() {
    let openVideo = null;
    if (state.activeVideo) {
      const idx = (state.theaterClips || []).findIndex((c) => c.path === state.activeVideo.path);
      openVideo = {
        name: state.activeVideo.name,
        path: state.activeVideo.path,
        theaterIndex: idx >= 0 ? idx + 1 : null,  // null = open clip isn't in the theater
      };
    }
    return {
      theaterName: state.theaterName,   // user's own word for the theater (e.g. "Sanctuary")
      currentView: state.currentView,
      currentFolder: state.currentFolder,
      currentSourceIndex: state.currentSourceIndex,
      theaterClips: (state.theaterClips || []).map((c, i) => ({
        index: i + 1, name: c.name, path: c.path, folder: c.folder,
        loopStart: c.loopStart, loopEnd: c.loopEnd,
      })),
      currentVideos: (state.currentVideos || []).map((v, i) => ({
        index: i + 1, name: v.name, path: v.path, folder: v.folder, filename: v.filename,
      })),
      playlistNames: (state.playlists || []).map((p) => p.name),
      loadedPlaylist: state.loadedPlaylistName || null,
      openVideo,
    };
  }

  const UI = {
    play_all: () => {
      // Browse-grid previews are created on demand since 2.0 — pin them first (mirrors #btn-play-all-browse)
      if (typeof promoteThumb === "function") {
        document.querySelectorAll("#video-grid .video-thumb").forEach((t) => { t.dataset.pinned = "1"; promoteThumb(t); });
      }
      document.querySelectorAll(".ws-video, .theater-video, #video-grid .thumb-video").forEach((v) => v.play().catch(() => {}));
    },
    pause_all: () => document.querySelectorAll(".ws-video, .theater-video, #video-grid .thumb-video").forEach((v) => v.pause()),
    mute_all: () => document.querySelectorAll(".ws-video, .theater-video, #video-grid .thumb-video").forEach((v) => { v.muted = true; }),
    unmute_all: () => document.querySelectorAll(".ws-video, .theater-video, #video-grid .thumb-video").forEach((v) => { v.muted = false; }),
    open_workspace: (a) => {
      if (a && a.source === "browse" && (state.currentVideos || []).length) {
        const clips = state.currentVideos.map((v) => ({
          path: v.path, name: v.name, filename: v.filename, folder: v.folder,
          loopStart: null, loopEnd: null,
        }));
        openWorkspace(clips);
      } else {
        openWorkspace();
      }
    },
    close_workspace: () => closeWorkspace(),
    set_loaded_playlist: (a) => { state.loadedPlaylistName = a.name || null; },
    switch_view: (a) => switchView(a.view || "browse"),
    open_folder: (a) => {
      const f = (state.folders || []).find((x) => x.name.toLowerCase() === String(a.folder || "").toLowerCase())
            || (state.folders || []).find((x) => x.name.toLowerCase().includes(String(a.folder || "").toLowerCase()));
      if (f) { switchView("browse"); openFolder(f.path, f.sourceIndex); }
    },
  };

  async function applyRefresh(list) {
    if (!list) return;
    if (list.includes("theater")) { await loadTheater(); if (state.currentView === "theater") renderTheater(); }
    if (list.includes("folders")) await loadFolders();
    if (list.includes("playlists")) await loadPlaylists();
  }

  async function send() {
    const text = input.value.trim();
    if (!text || busy) return;
    busy = true; sendBtn.disabled = true; input.value = "";
    addMsg("user", text);
    history.push({ role: "user", text });
    const thinking = document.createElement("div");
    thinking.className = "ai-thinking"; thinking.textContent = "thinking…";
    msgs.appendChild(thinking); msgs.scrollTop = msgs.scrollHeight;

    try {
      const res = await api.post("/api/agent", { message: text, history: history.slice(0, -1), context: buildContext() });
      thinking.remove();
      if (res.error) { addMsg("assistant", res.error); }
      else {
        for (const cmd of (res.ui_commands || [])) { try { UI[cmd.command] && UI[cmd.command](cmd.args || {}); } catch (e) { console.error(e); } }
        await applyRefresh(res.refresh);
        const trace = (res.ui_commands || []).map((c) => c.command.replace(/_/g, " "));
        addMsg("assistant", res.reply || "Done.", trace);
        history.push({ role: "assistant", text: res.reply || "Done." });
      }
    } catch (e) {
      thinking.remove();
      addMsg("assistant", "Couldn't reach the assistant. Try again.");
    } finally {
      busy = false; sendBtn.disabled = false; input.focus();
    }
  }

  // ── Draggable orb + movable/resizable panel ─────────────────
  // Position and size persist per-browser in localStorage; default stays bottom-right.
  const ORB_MARGIN = 12, DRAG_THRESHOLD = 5;
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
    del(k) { try { localStorage.removeItem(k); } catch {} },
  };
  const clampNum = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function applyOrbPos(pos) {
    if (pos) {
      const size = orb.getBoundingClientRect();
      const x = clampNum(pos.x, 0, window.innerWidth - (size.width || 56));
      const y = clampNum(pos.y, 0, window.innerHeight - (size.height || 56));
      orb.style.left = x + "px"; orb.style.top = y + "px";
      orb.style.right = "auto"; orb.style.bottom = "auto";
    } else {
      // Back to the CSS default (bottom-right corner)
      orb.style.left = orb.style.top = orb.style.right = orb.style.bottom = "";
    }
  }

  function applyPanelSize(size) {
    if (!size) return;
    panel.classList.add("custom-size");
    panel.style.width = clampNum(size.w, 300, window.innerWidth * 0.9) + "px";
    panel.style.height = clampNum(size.h, 280, window.innerHeight * 0.85) + "px";
  }

  function positionPanel() {
    if (panel.classList.contains("hidden")) return;
    const o = orb.getBoundingClientRect();
    const p = panel.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    // Open above the orb when it sits in the lower half of the screen, else below;
    // right-align to the orb on the right half, else left-align. Then clamp on-screen.
    let top = (o.top + o.height / 2 > vh / 2) ? o.top - p.height - ORB_MARGIN : o.bottom + ORB_MARGIN;
    let left = (o.left + o.width / 2 > vw / 2) ? o.right - p.width : o.left;
    left = clampNum(left, ORB_MARGIN, Math.max(ORB_MARGIN, vw - p.width - ORB_MARGIN));
    top = clampNum(top, ORB_MARGIN, Math.max(ORB_MARGIN, vh - p.height - ORB_MARGIN));
    panel.style.left = left + "px"; panel.style.top = top + "px";
    panel.style.right = "auto"; panel.style.bottom = "auto";
  }

  let orbDrag = null, suppressClick = false;
  orb.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    const r = orb.getBoundingClientRect();
    orbDrag = { startX: e.clientX, startY: e.clientY, offX: e.clientX - r.left, offY: e.clientY - r.top, moved: false };
  });

  const resizeHandle = panel.querySelector(".ai-resize-handle");
  let panelResize = null;
  if (resizeHandle) resizeHandle.addEventListener("mousedown", (e) => {
    e.preventDefault(); e.stopPropagation();
    const p = panel.getBoundingClientRect();
    panelResize = { startW: p.width, startH: p.height, startX: e.clientX, startY: e.clientY, right: p.right, bottom: p.bottom };
  });

  document.addEventListener("mousemove", (e) => {
    if (orbDrag) {
      if (!orbDrag.moved && Math.hypot(e.clientX - orbDrag.startX, e.clientY - orbDrag.startY) < DRAG_THRESHOLD) return;
      if (!orbDrag.moved) { orbDrag.moved = true; orb.classList.add("dragging"); }
      e.preventDefault();
      applyOrbPos({ x: e.clientX - orbDrag.offX, y: e.clientY - orbDrag.offY });
      positionPanel();
    }
    if (panelResize) {
      e.preventDefault();
      const w = clampNum(panelResize.startW - (e.clientX - panelResize.startX), 300, window.innerWidth * 0.9);
      const h = clampNum(panelResize.startH - (e.clientY - panelResize.startY), 280, window.innerHeight * 0.85);
      panel.classList.add("custom-size");
      panel.style.width = w + "px"; panel.style.height = h + "px";
      // Top-left grip: keep the bottom-right corner pinned while resizing
      panel.style.left = (panelResize.right - w) + "px";
      panel.style.top = (panelResize.bottom - h) + "px";
      panel.style.right = "auto"; panel.style.bottom = "auto";
    }
  });

  document.addEventListener("mouseup", () => {
    if (orbDrag) {
      if (orbDrag.moved) {
        orb.classList.remove("dragging");
        const r = orb.getBoundingClientRect();
        store.set("aiOrbPos", { x: r.left, y: r.top });
        // Swallow the click that follows drag-end. Consumed by the click handler
        // itself (deterministic), with a timeout fallback in case no click fires.
        suppressClick = true;
        setTimeout(() => { suppressClick = false; }, 250);
      }
      orbDrag = null;
    }
    if (panelResize) {
      const p = panel.getBoundingClientRect();
      store.set("aiPanelSize", { w: Math.round(p.width), h: Math.round(p.height) });
      panelResize = null;
    }
  });

  window.addEventListener("resize", () => {
    const saved = store.get("aiOrbPos");
    if (saved) applyOrbPos(saved);
    positionPanel();
  });

  orb.addEventListener("click", () => {
    if (suppressClick) { suppressClick = false; return; }  // consume the post-drag click
    panel.classList.toggle("hidden");
    if (!panel.classList.contains("hidden")) { positionPanel(); input.focus(); }
  });
  orb.addEventListener("dblclick", () => {
    store.del("aiOrbPos");
    applyOrbPos(null);
    positionPanel();
    toast("Assistant orb reset to default corner", "info");
  });
  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  $(".ai-reset").addEventListener("click", () => { history = []; msgs.innerHTML = ""; });

  // Restore saved orb position + panel size on load
  applyOrbPos(store.get("aiOrbPos"));
  applyPanelSize(store.get("aiPanelSize"));

  // ── Settings: BYOK key wiring ──
  async function refreshKeyStatus() {
    const status = $("#ai-key-status");
    if (!status) return;
    try {
      const cfg = await api.get("/api/ai/config");
      $("#ai-enabled").checked = cfg.enabled;
      status.textContent = !cfg.available ? "SDK not installed (pip install google-genai)."
        : cfg.envKey ? "Configured via GEMINI_API_KEY environment variable."
        : cfg.configured ? "Key configured." : "Not configured — paste a key below.";
    } catch { status.textContent = "Status unavailable."; }
  }
  const saveBtn = $("#ai-save-key");
  if (saveBtn) saveBtn.addEventListener("click", async () => {
    const key = $("#ai-key-input").value.trim();
    const enabled = $("#ai-enabled").checked;
    await api.post("/api/ai/config", { ...(key ? { geminiApiKey: key } : {}), enabled });
    $("#ai-key-input").value = "";
    toast("AI settings saved", "success");
    await refreshKeyStatus(); await checkAvailability();
  });
  const testBtn = $("#ai-test-key");
  if (testBtn) testBtn.addEventListener("click", async () => {
    const r = await api.post("/api/agent", { message: "Reply with the single word: ready", history: [], context: {} });
    toast(r.error ? r.error : "Connection OK", r.error ? "error" : "success");
  });

  // Settings open: refresh status (extra listener — app.js bound openSettings by value,
  // so wrapping the global wouldn't fire; addEventListener stacks).
  const gear = $("#btn-settings");
  if (gear) gear.addEventListener("click", refreshKeyStatus);
  checkAvailability();
})();
