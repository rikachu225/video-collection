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
    play_all: () => document.querySelectorAll(".ws-video, .theater-video, #video-grid .thumb-video").forEach((v) => v.play().catch(() => {})),
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

  orb.addEventListener("click", () => { panel.classList.toggle("hidden"); if (!panel.classList.contains("hidden")) input.focus(); });
  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  $(".ai-reset").addEventListener("click", () => { history = []; msgs.innerHTML = ""; });

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
