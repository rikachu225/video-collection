/**
 * Video Collection - Frontend Application
 * Single-page app for browsing, playing, and managing video collections.
 */

// ── Video Prefetch Cache ────────────────────────────────────
// Background-fetches videos into Blob URLs so workspace opens instantly.
// Memory is freed when workspace closes or user navigates away.
const prefetchCache = {
  _blobs: new Map(),      // path → blobUrl
  _pending: new Map(),    // path → AbortController
  _queue: [],             // paths waiting to be fetched
  _active: false,         // is the queue processor running?
  _currentContext: null,   // "theater" | folderKey — invalidated on navigation

  /**
   * Start prefetching a list of video paths in the background.
   * Fetches one at a time to avoid HDD seek thrashing.
   * @param {string[]} paths - Video paths to prefetch
   * @param {string} context - Cache context key (e.g. "theater" or folder path)
   */
  warm(paths, context) {
    // If context changed, flush old cache (user navigated to different view)
    if (context !== this._currentContext) {
      this.flush();
      this._currentContext = context;
    }
    // Queue only paths not already cached or in-flight
    for (const path of paths) {
      if (!this._blobs.has(path) && !this._pending.has(path) && !this._queue.includes(path)) {
        this._queue.push(path);
      }
    }
    this._processQueue();
  },

  /** Get cached blob URL for a path, or null if not yet cached. */
  get(path) {
    return this._blobs.get(path) || null;
  },

  /** Flush all cached blobs and abort pending fetches. Free memory. */
  flush() {
    // Abort in-flight fetches
    for (const [, controller] of this._pending) {
      controller.abort();
    }
    this._pending.clear();
    this._queue.length = 0;
    // Revoke blob URLs to free memory
    for (const [, blobUrl] of this._blobs) {
      URL.revokeObjectURL(blobUrl);
    }
    this._blobs.clear();
    this._active = false;
    this._currentContext = null;
  },

  /** Process the queue one item at a time (HDD-friendly). */
  async _processQueue() {
    if (this._active || this._queue.length === 0) return;
    this._active = true;

    while (this._queue.length > 0) {
      const path = this._queue.shift();
      // Skip if already cached (might have been fetched while queued)
      if (this._blobs.has(path)) continue;

      const controller = new AbortController();
      this._pending.set(path, controller);

      try {
        const url = `/api/stream/${encodeURIComponent(path)}`;
        const res = await fetch(url, { signal: controller.signal });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        const blobUrl = URL.createObjectURL(blob);
        this._blobs.set(path, blobUrl);
      } catch (e) {
        if (e.name !== "AbortError") {
          // Silently skip failed fetches — workspace will fall back to streaming
        }
      } finally {
        this._pending.delete(path);
      }
    }

    this._active = false;
  },
};

// ── State ────────────────────────────────────────────────────
const state = {
  folders: [],
  currentFolder: null,
  currentVideos: [],
  allVideos: [],
  theaterClips: [],
  playlists: [],
  currentView: "browse",
  currentSourceIndex: null,
  searchQuery: "",
  theaterPlaying: false,
  workspaceOpen: false,
  workspaceClips: [],
  workspaceSource: null, // "theater" | "browse"
  workspaceFolderKey: null, // folder key for browse workspace layout persistence
  workspaceFullscreenToggling: false, // prevents fullscreenchange from closing workspace
  loadedPlaylistName: null, // tracks which playlist is loaded for auto-save
  siteName: "My Collection",
  theaterName: "My Theater",
};

// ── DOM Refs ─────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const dom = {
  folderList: $("#folder-list"),
  videoGrid: $("#video-grid"),
  theaterGrid: $("#theater-grid"),
  theaterEmpty: $("#theater-empty"),
  playlistsList: $("#playlists-list"),
  playlistsEmpty: $("#playlists-empty"),
  breadcrumb: $("#breadcrumb"),
  stats: $("#stats"),
  searchInput: $("#search-input"),
  mainPlayer: $("#main-player"),
  playerSource: $("#player-source"),
  playerTitle: $("#player-title"),
  modalOverlay: $("#modal-overlay"),
  playlistNameInput: $("#playlist-name-input"),
  toastContainer: $("#toast-container"),
  workspaceOverlay: $("#workspace-overlay"),
  workspaceCanvas: $("#workspace-canvas"),
};

// ── API Helpers ──────────────────────────────────────────────
const api = {
  async get(url) {
    const res = await fetch(url);
    return res.json();
  },
  async post(url, data) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    return res.json();
  },
  async del(url) {
    const res = await fetch(url, { method: "DELETE" });
    return res.json();
  },
  async delBody(url, data) {
    const res = await fetch(url, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    return res.json();
  },
};

// ── Toast Notifications ──────────────────────────────────────
function toast(message, type = "info") {
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = message;
  dom.toastContainer.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 300);
  }, 2500);
}

// ── Navigation ───────────────────────────────────────────────
function switchView(view) {
  state.currentView = view;
  $$(".view").forEach((v) => v.classList.remove("active"));
  $(`#view-${view}`).classList.add("active");
  $$(".nav-btn").forEach((b) => b.classList.remove("active"));
  $(`.nav-btn[data-view="${view}"]`)?.classList.add("active");

  // Show/hide browse media controls
  const hasFolderVideos = view === "browse" && state.currentFolder;
  $("#browse-media-controls").style.display = hasFolderVideos ? "flex" : "none";

  if (view === "theater") loadTheater();
  if (view === "playlists") loadPlaylists();
  if (view === "browse" && !state.currentFolder) showFolderGrid();
}

$$(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const view = btn.dataset.view;
    if (view === "browse") {
      $("#view-player").classList.remove("active");
      state.currentFolder = null;
      switchView("browse");
    } else {
      switchView(view);
    }
  });
});

// ── Sidebar Toggle ───────────────────────────────────────────
$("#btn-sidebar-toggle").addEventListener("click", () => {
  $("#sidebar").classList.toggle("collapsed");
});

// ── Browse Media Controls ────────────────────────────────────
$("#btn-play-all-browse").addEventListener("click", () => {
  $$("#video-grid .thumb-video").forEach((v) => { v.currentTime = 2; v.play().catch(() => {}); });
  toast("All previews playing", "info");
});

$("#btn-pause-all-browse").addEventListener("click", () => {
  $$("#video-grid .thumb-video").forEach((v) => v.pause());
  toast("All previews paused", "info");
});

$("#btn-unmute-all-browse").addEventListener("click", () => {
  $$("#video-grid .thumb-video").forEach((v) => { v.muted = false; });
  toast("All unmuted", "info");
});

$("#btn-mute-all-browse").addEventListener("click", () => {
  $$("#video-grid .thumb-video").forEach((v) => { v.muted = true; });
  toast("All muted", "info");
});

$("#btn-workspace-browse").addEventListener("click", () => {
  if (state.currentVideos.length === 0) {
    toast("No videos in this folder", "error");
    return;
  }
  const clips = state.currentVideos.map((v) => ({
    path: v.path,
    name: v.name,
    filename: v.filename,
    folder: v.folder,
    loopStart: null,
    loopEnd: null,
  }));
  openWorkspace(clips);
});

// ── Folder Sidebar ───────────────────────────────────────────
async function loadFolders() {
  state.folders = await api.get("/api/folders");
  let totalVideos = 0;
  dom.folderList.innerHTML = "";

  // Group folders by source
  const sourceGroups = new Map();
  state.folders.forEach((folder) => {
    totalVideos += folder.count;
    const src = folder.source || "Library";
    if (!sourceGroups.has(src)) sourceGroups.set(src, []);
    sourceGroups.get(src).push(folder);
  });

  const multiSource = sourceGroups.size > 1;

  sourceGroups.forEach((folders, sourceName) => {
    if (multiSource) {
      const header = document.createElement("div");
      header.className = "source-header";
      header.textContent = sourceName;
      dom.folderList.appendChild(header);
    }

    folders.forEach((folder) => {
      const el = document.createElement("button");
      el.className = "folder-item" + (folder.hidden ? " folder-hidden" : "");
      el.dataset.folder = folder.path;
      el.dataset.source = folder.sourceIndex;
      const eyeIcon = folder.hidden
        ? `<svg class="folder-eye" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`
        : `<svg class="folder-eye" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
      el.innerHTML = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <span class="folder-name">${folder.name}</span>
        <span class="folder-count">${folder.count}</span>
        <span class="folder-visibility-toggle" title="${folder.hidden ? "Show folder" : "Hide folder"}">${eyeIcon}</span>
      `;
      el.addEventListener("click", (e) => {
        if (e.target.closest(".folder-visibility-toggle")) {
          e.stopPropagation();
          toggleFolderVisibility(folder.sourceIndex, folder.name);
          return;
        }
        if (!folder.hidden) openFolder(folder.path, folder.sourceIndex);
      });
      dom.folderList.appendChild(el);
    });
  });

  const visibleFolders = state.folders.filter((f) => !f.hidden);
  const visibleVideos = visibleFolders.reduce((sum, f) => sum + f.count, 0);
  if (multiSource) {
    dom.stats.textContent = `${sourceGroups.size} sources · ${visibleFolders.length} folders · ${visibleVideos} videos`;
  } else {
    dom.stats.textContent = `${visibleFolders.length} folders · ${visibleVideos} videos`;
  }
}

async function toggleFolderVisibility(sourceIndex, folderName) {
  const key = `${sourceIndex}:${folderName}`;
  try {
    const result = await api.post("/api/folders/toggle-visibility", { key });
    toast(result.visible ? `"${folderName}" visible` : `"${folderName}" hidden`, "success");
    await loadFolders();
    showFolderGrid();
  } catch (err) {
    toast("Failed to toggle folder", "error");
  }
}

// ── Folder Grid (home view shows all folders as cards) ───────
function showFolderGrid() {
  state.currentFolder = null;
  state.currentSourceIndex = null;
  dom.breadcrumb.innerHTML = `<span class="crumb-home">Library</span>`;
  dom.videoGrid.innerHTML = "";

  // Hide browse media controls on folder grid
  $("#browse-media-controls").style.display = "none";

  $$(".folder-item").forEach((f) => f.classList.remove("active"));

  if (state.folders.length === 0) {
    dom.videoGrid.innerHTML = `
      <div class="empty-state" style="grid-column: 1 / -1;">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" opacity="0.3"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <p>No media sources configured</p>
        <p class="hint">Click the <strong>gear icon</strong> in the sidebar to add a media folder</p>
      </div>`;
    return;
  }

  // Group by source for display
  const sourceGroups = new Map();
  state.folders.forEach((folder) => {
    const src = folder.source || "Library";
    if (!sourceGroups.has(src)) sourceGroups.set(src, []);
    sourceGroups.get(src).push(folder);
  });

  const multiSource = sourceGroups.size > 1;

  sourceGroups.forEach((folders, sourceName) => {
    if (multiSource) {
      const header = document.createElement("div");
      header.className = "source-grid-header";
      header.textContent = sourceName;
      dom.videoGrid.appendChild(header);
    }

    folders.forEach((folder) => {
      if (folder.hidden) return; // Don't show hidden folders in the grid
      const card = document.createElement("div");
      card.className = "folder-card";
      card.innerHTML = `
        <div class="folder-card-icon">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        </div>
        <div class="folder-card-info">
          <span class="folder-card-name">${folder.name}</span>
          <span class="folder-card-count">${folder.count} videos</span>
        </div>
      `;
      card.addEventListener("click", () => openFolder(folder.path, folder.sourceIndex));
      dom.videoGrid.appendChild(card);
    });
  });

  // "New Collection" card at the end
  const newCard = document.createElement("div");
  newCard.className = "folder-card new-collection-card";
  newCard.innerHTML = `
    <div class="folder-card-icon">
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
      </svg>
    </div>
    <div class="folder-card-info">
      <span class="folder-card-name">New Collection</span>
      <span class="folder-card-count">Create a folder</span>
    </div>
  `;
  newCard.addEventListener("click", openNewCollectionModal);
  dom.videoGrid.appendChild(newCard);
}

// ── Open Folder & Show Videos ────────────────────────────────
async function openFolder(folderPath, sourceIndex) {
  state.currentFolder = folderPath;
  state.currentSourceIndex = sourceIndex;
  const url = sourceIndex !== undefined
    ? `/api/videos/${encodeURIComponent(folderPath)}?source=${sourceIndex}`
    : `/api/videos/${encodeURIComponent(folderPath)}`;
  state.currentVideos = await api.get(url);

  // Update sidebar active state
  $$(".folder-item").forEach((f) => {
    f.classList.toggle("active", f.dataset.folder === folderPath && f.dataset.source == sourceIndex);
  });

  // Show browse media controls
  $("#browse-media-controls").style.display = "flex";

  // Show download/import buttons when inside a folder
  $("#btn-download-url-folder").style.display = "";
  $("#btn-import-video").style.display = "";

  // Breadcrumb
  dom.breadcrumb.innerHTML = `
    <span class="crumb-link" data-action="home">Library</span>
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
    <span class="crumb-current">${folderPath}</span>
  `;
  $(".crumb-link")?.addEventListener("click", () => {
    switchView("browse");
    showFolderGrid();
  });

  // Ensure browse view is active
  if (state.currentView !== "browse") switchView("browse");
  $("#view-player").classList.remove("active");
  $("#view-browse").classList.add("active");

  renderVideoGrid(state.currentVideos);

  // Background-prefetch folder videos for instant workspace loading
  const folderKey = sourceIndex != null ? `${sourceIndex}:${folderPath}` : folderPath;
  const folderPaths = state.currentVideos.map((v) => v.path);
  if (folderPaths.length > 0) prefetchCache.warm(folderPaths, folderKey);
}

// ── Render Video Grid ────────────────────────────────────────
function renderVideoGrid(videos) {
  dom.videoGrid.innerHTML = "";
  if (videos.length === 0) {
    dom.videoGrid.innerHTML = `
      <div class="empty-state" style="grid-column: 1 / -1;">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" opacity="0.3"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <p>This folder is empty</p>
        <p class="hint">Download a video from a URL or import one from your computer</p>
      </div>`;
    return;
  }

  videos.forEach((video) => {
    const card = document.createElement("div");
    card.className = "video-card";
    card.innerHTML = `
      <div class="video-thumb" data-path="${video.path}">
        <video class="thumb-video" src="/api/stream/${encodeURIComponent(video.path)}#t=2" preload="metadata" muted></video>
        <div class="thumb-overlay">
          <svg width="36" height="36" viewBox="0 0 24 24" fill="white" opacity="0.9"><polygon points="5 3 19 12 5 21 5 3"/></svg>
        </div>
        <button class="add-theater-btn" data-tooltip="Add to ${state.theaterName}" data-video='${JSON.stringify(video).replace(/'/g, "&#39;")}'>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        </button>
        <button class="delete-video-btn" data-tooltip="Delete video" data-path="${video.path}">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        </button>
      </div>
      <div class="video-info">
        <span class="video-name" title="${video.filename}">${video.name}</span>
        <span class="video-meta">${formatSize(video.size)}</span>
      </div>
    `;

    // Click thumbnail to play
    card.querySelector(".video-thumb").addEventListener("click", (e) => {
      if (e.target.closest(".add-theater-btn") || e.target.closest(".delete-video-btn")) return;
      playVideo(video);
    });

    // Hover preview
    const thumbVid = card.querySelector(".thumb-video");
    card.querySelector(".video-thumb").addEventListener("mouseenter", () => {
      thumbVid.currentTime = 2;
      thumbVid.play().catch(() => {});
    });
    card.querySelector(".video-thumb").addEventListener("mouseleave", () => {
      thumbVid.pause();
      thumbVid.currentTime = 2;
    });

    // Add to theater button
    card.querySelector(".add-theater-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      const videoData = JSON.parse(e.currentTarget.dataset.video);
      addToTheater(videoData);
    });

    // Delete video button
    card.querySelector(".delete-video-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteVideo(video);
    });

    dom.videoGrid.appendChild(card);
  });
}

// ── Play Single Video (Draggable/Resizable Popup) ────────────
// Cache folder layouts in memory so we don't re-fetch every open
const folderLayoutCache = {};

async function getFolderLayouts(folderKey) {
  if (!folderKey) return {};
  if (folderLayoutCache[folderKey]) return folderLayoutCache[folderKey];
  try {
    const data = await api.get(`/api/folder-layouts/${encodeURIComponent(folderKey)}`);
    folderLayoutCache[folderKey] = data;
    return data;
  } catch { return {}; }
}

async function saveFolderLayout(folderKey, videoPath, layout) {
  if (!folderKey) return;
  // Update cache
  if (!folderLayoutCache[folderKey]) folderLayoutCache[folderKey] = {};
  folderLayoutCache[folderKey][videoPath] = layout;
  try {
    await api.post(`/api/folder-layouts/${encodeURIComponent(folderKey)}`, { videoPath, layout });
  } catch (err) { console.error("Save folder layout error:", err); }
}

async function playVideo(video) {
  const folderKey = state.currentSourceIndex != null
    ? `${state.currentSourceIndex}:${state.currentFolder}`
    : state.currentFolder;

  // Get saved layout for this video in this folder
  const layouts = await getFolderLayouts(folderKey);
  const saved = layouts[video.path] || null;

  // Create overlay (click-through background)
  const overlay = document.createElement("div");
  overlay.className = "video-popup-overlay video-popup-draggable-overlay";

  // Create the popup panel
  const popup = document.createElement("div");
  popup.className = "video-popup video-popup-draggable";
  popup.innerHTML = `
    <div class="video-popup-header">
      <span class="video-popup-title">${video.folder} / ${video.name}</span>
      <div class="video-popup-actions">
        <button class="cyber-btn accent" data-action="popup-add-theater" data-tooltip="Add to ${state.theaterName}">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          Add to ${state.theaterName}
        </button>
        <button class="cyber-btn speed-btn" data-action="popup-speed" data-tooltip="Playback Speed">1x</button>
        <button class="cyber-btn speed-btn" data-action="popup-volume" data-tooltip="Volume Boost">100%</button>
        <button class="icon-btn" data-action="popup-close" data-tooltip="Close (Esc)">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
    </div>
    <video controls autoplay src="${getVideoSrc(video)}"></video>
    <div class="popup-resize-handle" data-resize="se"></div>
  `;

  // Apply saved layout or center defaults
  if (saved) {
    popup.style.left = `${saved.left}px`;
    popup.style.top = `${saved.top}px`;
    popup.style.width = `${saved.width}px`;
    popup.style.height = `${saved.height}px`;
  } else {
    popup.style.width = "80vw";
    popup.style.height = "75vh";
    popup.style.left = "50%";
    popup.style.top = "50%";
    popup.style.transform = "translate(-50%, -50%)";
  }

  overlay.appendChild(popup);

  // After appending, if centered with transform, convert to absolute coords
  if (!saved) {
    requestAnimationFrame(() => {
      const rect = popup.getBoundingClientRect();
      popup.style.left = `${rect.left}px`;
      popup.style.top = `${rect.top}px`;
      popup.style.transform = "none";
    });
  }

  // ── Drag from header ──
  let popupDrag = null;
  popup.querySelector(".video-popup-header").addEventListener("mousedown", (e) => {
    if (e.target.closest("button")) return;
    e.preventDefault();
    popupDrag = {
      startX: e.clientX - popup.offsetLeft,
      startY: e.clientY - popup.offsetTop,
    };
  });

  // ── Resize from corner handle ──
  let popupResize = null;
  popup.querySelector(".popup-resize-handle").addEventListener("mousedown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    popupResize = {
      startX: e.clientX,
      startY: e.clientY,
      startW: popup.offsetWidth,
      startH: popup.offsetHeight,
    };
  });

  function onMouseMove(e) {
    if (popupDrag) {
      popup.style.left = `${e.clientX - popupDrag.startX}px`;
      popup.style.top = `${e.clientY - popupDrag.startY}px`;
      popup.style.transform = "none";
    }
    if (popupResize) {
      const newW = Math.max(320, popupResize.startW + (e.clientX - popupResize.startX));
      const newH = Math.max(200, popupResize.startH + (e.clientY - popupResize.startY));
      popup.style.width = `${newW}px`;
      popup.style.height = `${newH}px`;
    }
  }

  function onMouseUp() {
    if (popupDrag || popupResize) {
      popupDrag = null;
      popupResize = null;
      // Save layout on every drag/resize end
      saveFolderLayout(folderKey, video.path, {
        left: popup.offsetLeft,
        top: popup.offsetTop,
        width: popup.offsetWidth,
        height: popup.offsetHeight,
      });
    }
  }

  document.addEventListener("mousemove", onMouseMove);
  document.addEventListener("mouseup", onMouseUp);

  // Close on overlay click (outside popup)
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closePopup();
  });

  popup.querySelector('[data-action="popup-close"]').addEventListener("click", closePopup);
  popup.querySelector('[data-action="popup-add-theater"]').addEventListener("click", () => {
    addToTheater(video);
  });
  popup.querySelector('[data-action="popup-speed"]').addEventListener("click", (e) => {
    const btn = e.currentTarget;
    const vid = popup.querySelector("video");
    const speeds = [1, 1.5, 2];
    const idx = speeds.indexOf(vid.playbackRate);
    const next = speeds[(idx + 1) % speeds.length];
    vid.playbackRate = next;
    btn.textContent = next + "x";
  });

  // Volume boost via Web Audio API GainNode (100% → 150% → 200%)
  popup.querySelector('[data-action="popup-volume"]').addEventListener("click", (e) => {
    const btn = e.currentTarget;
    const vid = popup.querySelector("video");
    if (!vid._gainNode) {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const source = ctx.createMediaElementSource(vid);
      const gain = ctx.createGain();
      source.connect(gain);
      gain.connect(ctx.destination);
      vid._gainNode = gain;
      vid._audioCtx = ctx;
    }
    const levels = [1.0, 1.5, 2.0];
    const labels = ["100%", "150%", "200%"];
    const idx = levels.indexOf(vid._gainNode.gain.value);
    const next = (idx + 1) % levels.length;
    vid._gainNode.gain.value = levels[next];
    btn.textContent = labels[next];
  });

  // Apply loop from sanctuary clip if set
  {
    const vid = popup.querySelector("video");
    if (video.loopStart != null && video.loopEnd != null) {
      setupVideoLoop(vid, video.loopStart, video.loopEnd);
    }
  }

  function onEsc(e) {
    if (e.key === "Escape" && !state.workspaceOpen) closePopup();
  }
  document.addEventListener("keydown", onEsc);

  function closePopup() {
    // Save final position before closing
    saveFolderLayout(folderKey, video.path, {
      left: popup.offsetLeft,
      top: popup.offsetTop,
      width: popup.offsetWidth,
      height: popup.offsetHeight,
    });
    const vid = popup.querySelector("video");
    if (vid) { vid.pause(); vid.src = ""; }
    overlay.remove();
    document.removeEventListener("keydown", onEsc);
    document.removeEventListener("mousemove", onMouseMove);
    document.removeEventListener("mouseup", onMouseUp);
  }

  document.body.appendChild(overlay);
}

// Legacy back button (still works if anyone's in that view)
$("#btn-back").addEventListener("click", () => {
  dom.mainPlayer.pause();
  dom.mainPlayer.src = "";
  $("#view-player").classList.remove("active");
  $("#view-browse").classList.add("active");
});

// ── Theater ──────────────────────────────────────────────────
async function addToTheater(video) {
  const clip = {
    path: video.path,
    name: video.name,
    filename: video.filename,
    folder: video.folder,
    loopStart: null,
    loopEnd: null,
  };
  const data = await api.post("/api/theater", clip);
  state.theaterClips = data.clips || [];
  toast(`Added "${video.name}" to ${state.theaterName}`, "success");
}

async function loadTheater() {
  const data = await api.get("/api/theater");
  state.theaterClips = data.clips || [];
  renderTheater();
}

function renderTheater() {
  dom.theaterGrid.innerHTML = "";
  const hasClips = state.theaterClips.length > 0;
  dom.theaterEmpty.classList.toggle("hidden", hasClips);
  dom.theaterGrid.classList.toggle("hidden", !hasClips);

  if (!hasClips) return;

  // Dynamic grid sizing
  const count = state.theaterClips.length;
  let cols;
  if (count <= 1) cols = 1;
  else if (count <= 4) cols = 2;
  else if (count <= 9) cols = 3;
  else cols = 4;
  dom.theaterGrid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;

  state.theaterClips.forEach((clip) => {
    const cell = document.createElement("div");
    cell.className = "theater-cell";
    cell.dataset.path = clip.path;

    const hasLoop = clip.loopStart !== null && clip.loopEnd !== null;
    const loopStartDisplay = hasLoop ? formatTime(clip.loopStart) : "";
    const loopEndDisplay = hasLoop ? formatTime(clip.loopEnd) : "";

    cell.innerHTML = `
      <div class="theater-video-wrap">
        <video class="theater-video" src="${getVideoSrc(clip)}" preload="none" loop muted></video>
        <div class="theater-cell-overlay">
          <span class="theater-clip-name">${clip.name}</span>
          <span class="theater-clip-folder">${clip.cached ? "Cached" : clip.url ? "External URL" : clip.folder}</span>
        </div>
        <div class="theater-cell-controls">
          <button class="icon-btn-sm" data-action="fullscreen" data-tooltip="Expand">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
          </button>
          <button class="icon-btn-sm" data-action="toggle-mute" data-tooltip="Toggle Sound">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>
          </button>
          <button class="icon-btn-sm danger-btn" data-action="remove" data-tooltip="Remove from ${state.theaterName}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
      </div>
      <div class="loop-controls">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>
        <input type="text" class="loop-input" data-field="start" placeholder="0:00" value="${loopStartDisplay}" data-tooltip="Loop start (m:ss)" />
        <span class="loop-dash">\u2192</span>
        <input type="text" class="loop-input" data-field="end" placeholder="0:00" value="${loopEndDisplay}" data-tooltip="Loop end (m:ss)" />
        <button class="icon-btn-xs" data-action="set-loop" data-tooltip="Apply Loop">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
        </button>
      </div>
    `;

    // URL badge for external clips
    if (clip.url) {
      const badge = document.createElement("div");
      badge.className = "url-clip-badge";
      badge.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`;
      cell.querySelector(".theater-video-wrap").appendChild(badge);
    }

    // Cached badge for yt-dlp downloaded clips
    if (clip.cached) {
      const badge = document.createElement("div");
      badge.className = "cached-clip-badge";
      badge.title = "Cached from: " + (clip.sourceUrl || "external");
      badge.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`;
      cell.querySelector(".theater-video-wrap").appendChild(badge);
    }

    // Bind events
    const video = cell.querySelector(".theater-video");

    cell.querySelector('[data-action="fullscreen"]').addEventListener("click", () => {
      if (video.requestFullscreen) video.requestFullscreen();
    });

    cell.querySelector('[data-action="toggle-mute"]').addEventListener("click", (e) => {
      video.muted = !video.muted;
      e.currentTarget.classList.toggle("unmuted", !video.muted);
    });

    cell.querySelector('[data-action="remove"]').addEventListener("click", async () => {
      const data = await api.del(`/api/theater/${encodeURIComponent(clip.path)}`);
      state.theaterClips = data.clips || [];
      // Auto-save to loaded playlist
      if (state.loadedPlaylistName) {
        await api.post("/api/playlists", {
          name: state.loadedPlaylistName,
          clips: state.theaterClips,
        });
      }
      renderTheater();
      toast(`Removed "${clip.name}"`, "info");
    });

    cell.querySelector('[data-action="set-loop"]').addEventListener("click", async () => {
      const startInput = cell.querySelector('.loop-input[data-field="start"]');
      const endInput = cell.querySelector('.loop-input[data-field="end"]');
      const startSec = parseTime(startInput.value);
      const endSec = parseTime(endInput.value);

      if (startSec === null || endSec === null || endSec <= startSec) {
        toast("Invalid loop times. Use format m:ss (e.g., 1:20)", "error");
        return;
      }

      await api.post("/api/theater/loop", {
        path: clip.path,
        loopStart: startSec,
        loopEnd: endSec,
      });

      clip.loopStart = startSec;
      clip.loopEnd = endSec;
      setupVideoLoop(video, startSec, endSec);
      toast(`Loop set: ${formatTime(startSec)} \u2192 ${formatTime(endSec)}`, "success");
    });

    // Setup existing loop
    if (hasLoop) {
      setupVideoLoop(video, clip.loopStart, clip.loopEnd);
    }

    // Click video area to open popup player (not control buttons)
    cell.querySelector(".theater-video-wrap").addEventListener("click", (e) => {
      if (e.target.closest(".theater-cell-controls") || e.target.closest(".icon-btn-sm")) return;
      playVideo({
        path: clip.path,
        name: clip.name,
        folder: clip.folder || "Theater",
        url: clip.url || null,
        cached: clip.cached || false,
        loopStart: clip.loopStart,
        loopEnd: clip.loopEnd,
      });
    });

    dom.theaterGrid.appendChild(cell);
  });

  // Staggered loading: load videos one-by-one to reduce HDD seek contention
  const theaterVideos = dom.theaterGrid.querySelectorAll(".theater-video");
  let theaterIdx = 0;
  function loadNextTheaterVideo() {
    if (theaterIdx >= theaterVideos.length) return;
    const v = theaterVideos[theaterIdx];
    v.preload = "metadata";
    v.load();

    let advanced = false;
    function advance() {
      if (advanced) return;
      advanced = true;
      theaterIdx++;
      setTimeout(loadNextTheaterVideo, 100);
    }

    v.addEventListener("loadedmetadata", advance, { once: true });
    v.addEventListener("error", advance, { once: true });
    // Fallback: 5s per video
    setTimeout(advance, 5000);
  }
  if (theaterVideos.length > 0) loadNextTheaterVideo();

  // Background-prefetch local videos for instant workspace loading
  const localPaths = state.theaterClips
    .filter((c) => !c.url && !c.cached)
    .map((c) => c.path);
  if (localPaths.length > 0) prefetchCache.warm(localPaths, "theater");
}

function setupVideoLoop(videoEl, startSec, endSec) {
  videoEl.currentTime = startSec;
  // Remove old listener if any
  videoEl._loopHandler && videoEl.removeEventListener("timeupdate", videoEl._loopHandler);

  const handler = () => {
    if (videoEl.currentTime >= endSec || videoEl.currentTime < startSec - 0.5) {
      videoEl.currentTime = startSec;
    }
  };
  videoEl._loopHandler = handler;
  videoEl.addEventListener("timeupdate", handler);
}

// Theater controls
$("#btn-play-all").addEventListener("click", () => {
  $$(".theater-video").forEach((v) => v.play().catch(() => {}));
  state.theaterPlaying = true;
  toast("All clips playing", "info");
});

$("#btn-stop-all").addEventListener("click", () => {
  $$(".theater-video").forEach((v) => v.pause());
  state.theaterPlaying = false;
  toast("All clips paused", "info");
});

$("#btn-unmute-all-theater").addEventListener("click", () => {
  $$(".theater-video").forEach((v) => { v.muted = false; });
  $$(".theater-cell-controls [data-action='toggle-mute']").forEach((b) => b.classList.add("unmuted"));
  toast("All clips unmuted", "info");
});

$("#btn-mute-all-theater").addEventListener("click", () => {
  $$(".theater-video").forEach((v) => { v.muted = true; });
  $$(".theater-cell-controls [data-action='toggle-mute']").forEach((b) => b.classList.remove("unmuted"));
  toast("All clips muted", "info");
});

$("#btn-clear-theater").addEventListener("click", async () => {
  if (!confirm(`Clear all clips from ${state.theaterName}?`)) return;
  for (const clip of [...state.theaterClips]) {
    await api.del(`/api/theater/${encodeURIComponent(clip.path)}`);
  }
  state.theaterClips = [];
  state.loadedPlaylistName = null;
  renderTheater();
  toast(`${state.theaterName} cleared`, "info");
});

// ══════════════════════════════════════════════════════════════
// ── WORKSPACE MODE ───────────────────────────────────────────
// ══════════════════════════════════════════════════════════════

let dragState = null;
let resizeState = null;

async function openWorkspace(clips) {
  const workspaceClips = clips || state.theaterClips;
  if (workspaceClips.length === 0) {
    toast("No videos to display", "error");
    return;
  }
  state.workspaceClips = workspaceClips;
  state.workspaceSource = clips ? "browse" : "theater";
  state.workspaceOpen = true;
  dom.workspaceOverlay.classList.remove("hidden");
  dom.workspaceCanvas.innerHTML = "";
  document.body.style.overflow = "hidden";

  // If opening from browse, restore saved folder layouts onto clips
  if (state.workspaceSource === "browse" && state.currentFolder) {
    const folderKey = state.currentSourceIndex != null
      ? `${state.currentSourceIndex}:${state.currentFolder}`
      : state.currentFolder;
    state.workspaceFolderKey = folderKey;
    const layouts = await getFolderLayouts(folderKey);
    state.workspaceClips.forEach((clip) => {
      const saved = layouts[clip.path];
      if (saved) {
        clip.wsLeft = saved.left;
        clip.wsTop = saved.top;
        clip.wsWidth = saved.width;
        clip.wsHeight = saved.height;
      }
    });
  } else {
    state.workspaceFolderKey = null;
  }

  // Enter browser fullscreen
  if (document.documentElement.requestFullscreen) {
    document.documentElement.requestFullscreen().catch(() => {});
  }

  buildWorkspacePanels();

  // Wait for fullscreen to settle, then restore layout and start videos
  setTimeout(() => {
    restoreWorkspaceLayout();

    const wsVideos = Array.from(document.querySelectorAll(".ws-video"));

    // Check if all videos are prefetched (blob URLs = already in memory)
    const allPrefetched = wsVideos.every((v) => v.src.startsWith("blob:"));

    if (allPrefetched) {
      // Fast path: all videos are in memory — play them all immediately
      wsVideos.forEach((v) => {
        v.preload = "auto";
        v.load();
        v.play().catch(() => {});
      });
    } else {
      // Staggered loading one-by-one to reduce HDD seek contention
      let wsLoadIdx = 0;
      function loadNextWsVideo() {
        if (wsLoadIdx >= wsVideos.length) return;
        const v = wsVideos[wsLoadIdx];
        v.preload = "auto";
        v.load();
        wsLoadIdx++;

        let advanced = false;
        function advance() {
          if (advanced) return;
          advanced = true;
          v.play().catch(() => {});
          setTimeout(loadNextWsVideo, 200);
        }

        v.addEventListener("canplay", advance, { once: true });
        v.addEventListener("error", advance, { once: true });
        // Fallback if canplay never fires
        setTimeout(advance, 5000);
      }
      if (wsVideos.length > 0) loadNextWsVideo();
    }
  }, 100);
}

function closeWorkspace() {
  state.workspaceOpen = false;
  dom.workspaceOverlay.classList.add("hidden");

  // Pause and release all video elements
  $$(".ws-video").forEach((v) => { v.pause(); v.src = ""; });

  // Free prefetch cache memory (blob URLs revoked)
  prefetchCache.flush();

  dom.workspaceCanvas.innerHTML = "";
  document.body.style.overflow = "";

  // Exit browser fullscreen
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  }
}

function buildWorkspacePanels() {
  state.workspaceClips.forEach((clip, i) => {
    const panel = document.createElement("div");
    panel.className = "ws-panel";
    panel.dataset.index = i;
    panel.dataset.path = clip.path;

    const hasLoop = clip.loopStart !== null && clip.loopEnd !== null;

    panel.innerHTML = `
      <div class="ws-panel-titlebar">
        <span class="ws-panel-name">${clip.name}</span>
        <div class="ws-panel-btns">
          <button class="ws-btn" data-action="ws-toggle-play" data-tooltip="Play/Pause">
            <svg class="icon-play" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            <svg class="icon-pause" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
          </button>
          <button class="ws-btn speed-btn" data-action="ws-speed" data-tooltip="Speed">1x</button>
          <button class="ws-btn speed-btn" data-action="ws-volume" data-tooltip="Volume Boost">100%</button>
          <button class="ws-btn" data-action="ws-toggle-mute" data-tooltip="Toggle Sound">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>
          </button>
          <button class="ws-btn ws-btn-danger" data-action="ws-remove" data-tooltip="Remove clip">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
      </div>
      <video class="ws-video" src="${prefetchCache.get(clip.path) || getVideoSrc(clip)}" loop muted preload="${prefetchCache.get(clip.path) ? 'auto' : 'none'}"></video>
      <div class="ws-panel-resize-handle ws-resize-tl" data-resize="tl"></div>
      <div class="ws-panel-resize-handle ws-resize-tr" data-resize="tr"></div>
      <div class="ws-panel-resize-handle ws-resize-bl" data-resize="bl"></div>
      <div class="ws-panel-resize-handle ws-resize-br" data-resize="br"></div>
    `;

    const video = panel.querySelector(".ws-video");

    // Setup loop
    if (video && hasLoop) {
      setupVideoLoop(video, clip.loopStart, clip.loopEnd);
      video.currentTime = clip.loopStart;
    }

    // Any click anywhere on the panel brings it to front
    panel.addEventListener("mousedown", () => {
      $$(".ws-panel").forEach((p) => p.style.zIndex = "1");
      panel.style.zIndex = "10";
    });

    // Drag from anywhere on the panel (except buttons, resize handle, video controls)
    panel.addEventListener("mousedown", (e) => {
      // Skip if clicking interactive elements inside panel
      if (e.target.closest(".ws-btn, .ws-panel-resize-handle, video")) return;
      e.preventDefault();

      dragState = {
        panel,
        startX: e.clientX - panel.offsetLeft,
        startY: e.clientY - panel.offsetTop,
      };
    });

    // Resize handles (all four corners)
    panel.querySelectorAll(".ws-panel-resize-handle").forEach((handle) => {
      handle.addEventListener("mousedown", (e) => {
        e.preventDefault();
        e.stopPropagation();

        resizeState = {
          panel,
          corner: handle.dataset.resize,
          startX: e.clientX,
          startY: e.clientY,
          startW: panel.offsetWidth,
          startH: panel.offsetHeight,
          startLeft: panel.offsetLeft,
          startTop: panel.offsetTop,
        };
      });
    });

    // Toggle mute
    panel.querySelector('[data-action="ws-toggle-mute"]').addEventListener("click", (e) => {
      video.muted = !video.muted;
      e.currentTarget.classList.toggle("unmuted", !video.muted);
    });

    // Toggle play/pause
    const playBtn = panel.querySelector('[data-action="ws-toggle-play"]');
    playBtn.classList.add("playing"); // starts playing by default
    playBtn.addEventListener("click", () => {
      if (video.paused) {
        video.play().catch(() => {});
        playBtn.classList.add("playing");
      } else {
        video.pause();
        playBtn.classList.remove("playing");
      }
    });

    // Playback speed toggle (1x → 1.5x → 2x → 1x)
    panel.querySelector('[data-action="ws-speed"]').addEventListener("click", (e) => {
      const btn = e.currentTarget;
      const speeds = [1, 1.5, 2];
      const idx = speeds.indexOf(video.playbackRate);
      const next = speeds[(idx + 1) % speeds.length];
      video.playbackRate = next;
      btn.textContent = next + "x";
    });

    // Volume boost (100% → 150% → 200%)
    panel.querySelector('[data-action="ws-volume"]').addEventListener("click", (e) => {
      const btn = e.currentTarget;
      const labels = ["100%", "150%", "200%"];

      // Web Audio GainNode path
      if (!video._gainNode) {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const source = ctx.createMediaElementSource(video);
        const gain = ctx.createGain();
        source.connect(gain);
        gain.connect(ctx.destination);
        video._gainNode = gain;
        video._audioCtx = ctx;
      }
      const gainLevels = [1.0, 1.5, 2.0];
      const idx = gainLevels.indexOf(video._gainNode.gain.value);
      const next = (idx + 1) % gainLevels.length;
      video._gainNode.gain.value = gainLevels[next];
      btn.textContent = labels[next];
    });

    // Remove clip
    panel.querySelector('[data-action="ws-remove"]').addEventListener("click", async () => {
      try {
        if (state.workspaceSource === "theater") {
          // Remove from sanctuary server-side
          const data = await api.del(`/api/theater/${encodeURIComponent(clip.path)}`);
          state.theaterClips = data.clips || [];
          // Auto-save to loaded playlist
          if (state.loadedPlaylistName) {
            await api.post("/api/playlists", {
              name: state.loadedPlaylistName,
              clips: state.theaterClips,
            });
          }
          toast(`Removed "${clip.name}"`, "info");
        } else {
          toast(`Hidden "${clip.name}"`, "info");
        }
      } catch (err) {
        console.error("Remove clip error:", err);
        toast("Failed to remove clip", "error");
        return;
      }
      // Remove from workspace state and DOM
      const idx = state.workspaceClips.indexOf(clip);
      if (idx !== -1) state.workspaceClips.splice(idx, 1);
      video.pause();
      video.src = "";
      panel.remove();
    });

    dom.workspaceCanvas.appendChild(panel);
  });
}

function autoTileLayout() {
  const panels = $$(".ws-panel");
  const count = panels.length;
  if (count === 0) return;

  const canvas = dom.workspaceCanvas;
  const cw = canvas.clientWidth;
  const ch = canvas.clientHeight;
  const gap = 8;
  const titlebarHeight = 32; // approximate height of .ws-panel-titlebar

  let cols, rows;
  if (count === 1) { cols = 1; rows = 1; }
  else if (count === 2) { cols = 2; rows = 1; }
  else if (count <= 4) { cols = 2; rows = 2; }
  else if (count <= 6) { cols = 3; rows = 2; }
  else if (count <= 9) { cols = 3; rows = 3; }
  else { cols = 4; rows = Math.ceil(count / 4); }

  // Grid slot size (max available space per panel)
  const slotW = Math.floor((cw - gap * (cols + 1)) / cols);
  const slotH = Math.floor((ch - gap * (rows + 1)) / rows);

  panels.forEach((panel, i) => {
    const col = i % cols;
    const row = Math.floor(i / cols);
    const slotX = gap + col * (slotW + gap);
    const slotY = gap + row * (slotH + gap);

    // Get video's natural aspect ratio
    const video = panel.querySelector(".ws-video");
    const vw = video?.videoWidth || 0;
    const vh = video?.videoHeight || 0;
    const ratio = (vw && vh) ? vw / vh : 16 / 9; // fallback 16:9

    // Fit panel within slot preserving aspect ratio
    // Available height for video = slotH - titlebar
    const availH = slotH - titlebarHeight;
    let panelW, videoH;

    if (slotW / availH > ratio) {
      // Slot is wider than video — height-constrained
      videoH = availH;
      panelW = Math.floor(videoH * ratio);
    } else {
      // Slot is taller than video — width-constrained
      panelW = slotW;
      videoH = Math.floor(panelW / ratio);
    }

    const panelH = videoH + titlebarHeight;

    // Center within the grid slot
    const offsetX = Math.floor((slotW - panelW) / 2);
    const offsetY = Math.floor((slotH - panelH) / 2);

    panel.style.left = `${slotX + offsetX}px`;
    panel.style.top = `${slotY + offsetY}px`;
    panel.style.width = `${panelW}px`;
    panel.style.height = `${panelH}px`;
  });
}

function restoreWorkspaceLayout() {
  const panels = $$(".ws-panel");
  const hasLayout = state.workspaceClips.some((c) => c.wsWidth != null);

  if (!hasLayout) {
    autoTileLayout();
    return;
  }

  panels.forEach((panel, i) => {
    const clip = state.workspaceClips[i];
    if (clip && clip.wsWidth != null) {
      panel.style.left = `${clip.wsLeft}px`;
      panel.style.top = `${clip.wsTop}px`;
      panel.style.width = `${clip.wsWidth}px`;
      panel.style.height = `${clip.wsHeight}px`;
    }
    // Restore volume boost if saved
    if (clip && clip.wsVolume != null && clip.wsVolume !== 1.0) {
      const vid = panel.querySelector(".ws-video");
      if (vid) {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const source = ctx.createMediaElementSource(vid);
        const gain = ctx.createGain();
        source.connect(gain);
        gain.connect(ctx.destination);
        vid._gainNode = gain;
        vid._audioCtx = ctx;
        gain.value = clip.wsVolume;
        // Update per-panel volume button text
        const levels = [1.0, 1.5, 2.0];
        const labels = ["100%", "150%", "200%"];
        const idx = levels.indexOf(clip.wsVolume);
        const volBtn = panel.querySelector('[data-action="ws-volume"]');
        if (volBtn && idx !== -1) volBtn.textContent = labels[idx];
      }
    }
  });
}

function captureWorkspaceLayout() {
  const panels = $$(".ws-panel");
  panels.forEach((panel, i) => {
    if (state.workspaceClips[i]) {
      state.workspaceClips[i].wsLeft = panel.offsetLeft;
      state.workspaceClips[i].wsTop = panel.offsetTop;
      state.workspaceClips[i].wsWidth = panel.offsetWidth;
      state.workspaceClips[i].wsHeight = panel.offsetHeight;
      // Capture volume boost level
      const vid = panel.querySelector(".ws-video");
      state.workspaceClips[i].wsVolume = vid?._gainNode ? vid._gainNode.gain.value : 1.0;
    }
  });
}

async function saveWorkspaceLayout() {
  captureWorkspaceLayout();

  try {
    if (state.workspaceSource === "browse" && state.workspaceFolderKey) {
      // Save each clip's layout to folder_layouts.json
      for (const clip of state.workspaceClips) {
        if (clip.wsWidth != null) {
          await saveFolderLayout(state.workspaceFolderKey, clip.path, {
            left: clip.wsLeft,
            top: clip.wsTop,
            width: clip.wsWidth,
            height: clip.wsHeight,
            volume: clip.wsVolume,
          });
        }
      }
    } else if (state.workspaceSource === "theater") {
      const layouts = state.workspaceClips.map((c) => ({
        path: c.path,
        wsLeft: c.wsLeft,
        wsTop: c.wsTop,
        wsWidth: c.wsWidth,
        wsHeight: c.wsHeight,
        wsVolume: c.wsVolume,
      }));
      await api.post("/api/theater/layout", { layouts });
      // Also update local theater clips
      state.theaterClips.forEach((clip) => {
        const ws = state.workspaceClips.find((c) => c.path === clip.path);
        if (ws) {
          clip.wsLeft = ws.wsLeft;
          clip.wsTop = ws.wsTop;
          clip.wsWidth = ws.wsWidth;
          clip.wsHeight = ws.wsHeight;
          clip.wsVolume = ws.wsVolume;
        }
      });

      // Auto-save to loaded playlist if one is active
      if (state.loadedPlaylistName) {
        await api.post("/api/playlists", {
          name: state.loadedPlaylistName,
          clips: state.theaterClips,
        });
      }
    }
  } catch (err) {
    console.error("Save layout error:", err);
  }

  // Flash the save button green (always runs, even if API had an error)
  const btn = $("#ws-save-layout");
  btn.classList.add("ws-save-flash");
  setTimeout(() => {
    btn.classList.add("ws-save-fade");
    btn.classList.remove("ws-save-flash");
    setTimeout(() => btn.classList.remove("ws-save-fade"), 800);
  }, 600);

  const msg = state.loadedPlaylistName
    ? `Layout saved to "${state.loadedPlaylistName}"`
    : "Layout saved";
  toast(msg, "success");
}

// Global mouse handlers for drag & resize
document.addEventListener("mousemove", (e) => {
  if (dragState) {
    const { panel, startX, startY } = dragState;
    const canvas = dom.workspaceCanvas;
    const cw = canvas.clientWidth;
    const ch = canvas.clientHeight;
    const pw = panel.offsetWidth;
    const ph = panel.offsetHeight;
    const minVisible = 50; // Always keep 50px of panel visible

    let newX = e.clientX - startX;
    let newY = e.clientY - startY;

    // Clamp so panel can't go fully off-screen
    newX = Math.max(-(pw - minVisible), Math.min(cw - minVisible, newX));
    newY = Math.max(0, Math.min(ch - minVisible, newY)); // top clamped at 0 (never hide titlebar)

    panel.style.left = `${newX}px`;
    panel.style.top = `${newY}px`;
  }
  if (resizeState) {
    const { panel, corner, startX, startY, startW, startH, startLeft, startTop } = resizeState;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;

    if (corner === "br") {
      panel.style.width = `${Math.max(200, startW + dx)}px`;
      panel.style.height = `${Math.max(150, startH + dy)}px`;
    } else if (corner === "bl") {
      const newW = Math.max(200, startW - dx);
      panel.style.width = `${newW}px`;
      panel.style.left = `${startLeft + startW - newW}px`;
      panel.style.height = `${Math.max(150, startH + dy)}px`;
    } else if (corner === "tr") {
      panel.style.width = `${Math.max(200, startW + dx)}px`;
      const newH = Math.max(150, startH - dy);
      panel.style.height = `${newH}px`;
      panel.style.top = `${startTop + startH - newH}px`;
    } else if (corner === "tl") {
      const newW = Math.max(200, startW - dx);
      panel.style.width = `${newW}px`;
      panel.style.left = `${startLeft + startW - newW}px`;
      const newH = Math.max(150, startH - dy);
      panel.style.height = `${newH}px`;
      panel.style.top = `${startTop + startH - newH}px`;
    }
  }
});

document.addEventListener("mouseup", () => {
  dragState = null;
  resizeState = null;
});

// Workspace toolbar buttons
$("#btn-workspace").addEventListener("click", () => openWorkspace());
$("#ws-exit").addEventListener("click", closeWorkspace);

$("#ws-play-all").addEventListener("click", () => {
  $$(".ws-video").forEach((v) => v.play().catch(() => {}));
});

$("#ws-pause-all").addEventListener("click", () => {
  $$(".ws-video").forEach((v) => v.pause());
});

$("#ws-unmute-all").addEventListener("click", () => {
  $$(".ws-video").forEach((v) => { v.muted = false; });
  $$('[data-action="ws-toggle-mute"]').forEach((b) => b.classList.add("unmuted"));
  toast("All unmuted", "info");
});

$("#ws-mute-all").addEventListener("click", () => {
  $$(".ws-video").forEach((v) => { v.muted = true; });
  $$('[data-action="ws-toggle-mute"]').forEach((b) => b.classList.remove("unmuted"));
  toast("All muted", "info");
});

$("#ws-volume-all").addEventListener("click", () => {
  const labels = ["100%", "150%", "200%"];
  const btn = $("#ws-volume-all");
  const currentLabel = btn.textContent.trim();
  const currentIdx = labels.indexOf(currentLabel);
  const nextIdx = (currentIdx + 1) % labels.length;

  const gainLevels = [1.0, 1.5, 2.0];
  const nextLevel = gainLevels[nextIdx];
  $$(".ws-video").forEach((vid) => {
    if (!vid._gainNode) {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const source = ctx.createMediaElementSource(vid);
      const gain = ctx.createGain();
      source.connect(gain);
      gain.connect(ctx.destination);
      vid._gainNode = gain;
      vid._audioCtx = ctx;
    }
    vid._gainNode.gain.value = nextLevel;
  });

  // Sync all per-panel volume buttons
  $$('[data-action="ws-volume"]').forEach((b) => {
    b.textContent = labels[nextIdx];
  });

  btn.textContent = labels[nextIdx];
  toast(`All volumes: ${labels[nextIdx]}`, "info");
});

$("#ws-save-layout").addEventListener("click", saveWorkspaceLayout);
$("#ws-tile").addEventListener("click", () => {
  autoTileLayout();
});

$("#ws-toggle-fullscreen").addEventListener("click", () => {
  state.workspaceFullscreenToggling = true;
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  } else {
    document.documentElement.requestFullscreen().catch(() => {});
  }
  // Reset flag after fullscreen transition completes
  setTimeout(() => {
    state.workspaceFullscreenToggling = false;
  }, 300);
});

// Escape to exit workspace
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.workspaceOpen) {
    closeWorkspace();
  }
});

// Sync workspace state if user exits fullscreen via browser chrome (F11, etc.)
document.addEventListener("fullscreenchange", () => {
  if (!document.fullscreenElement && state.workspaceOpen && !state.workspaceFullscreenToggling) {
    closeWorkspace();
  }
});

// ── Playlists ────────────────────────────────────────────────
$("#btn-save-playlist").addEventListener("click", () => {
  if (state.theaterClips.length === 0) {
    toast(`Add clips to ${state.theaterName} first`, "error");
    return;
  }
  dom.modalOverlay.classList.remove("hidden");
  dom.playlistNameInput.value = "";
  dom.playlistNameInput.focus();
});

$("#modal-cancel").addEventListener("click", () => {
  dom.modalOverlay.classList.add("hidden");
});

$("#modal-save").addEventListener("click", async () => {
  const name = dom.playlistNameInput.value.trim();
  if (!name) {
    toast("Enter a name", "error");
    return;
  }
  // Capture workspace layout if open, so playlist remembers panel positions
  if (state.workspaceOpen) captureWorkspaceLayout();
  await api.post("/api/playlists", { name, clips: state.theaterClips });
  state.loadedPlaylistName = name;
  dom.modalOverlay.classList.add("hidden");
  toast(`Playlist "${name}" saved`, "success");
});

dom.playlistNameInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#modal-save").click();
  if (e.key === "Escape") $("#modal-cancel").click();
});

// ── Add URL Modal ────────────────────────────────────────────
$("#btn-add-url").addEventListener("click", () => {
  $("#url-modal-overlay").classList.remove("hidden");
  $("#url-input").value = "";
  $("#url-name-input").value = "";
  $("#url-input").focus();
});

$("#url-modal-cancel").addEventListener("click", () => {
  $("#url-modal-overlay").classList.add("hidden");
});

$("#url-modal-overlay").addEventListener("click", (e) => {
  if (e.target === $("#url-modal-overlay")) $("#url-modal-overlay").classList.add("hidden");
});

$("#url-modal-add").addEventListener("click", () => addUrlClip());

$("#url-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addUrlClip();
  if (e.key === "Escape") $("#url-modal-overlay").classList.add("hidden");
});

$("#url-name-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addUrlClip();
  if (e.key === "Escape") $("#url-modal-overlay").classList.add("hidden");
});

async function addUrlClip() {
  const url = $("#url-input").value.trim();
  if (!url) {
    toast("Paste a video URL", "error");
    return;
  }

  try { new URL(url); } catch {
    toast("Invalid URL", "error");
    return;
  }

  const rawName = $("#url-name-input").value.trim();

  // Always cache via yt-dlp (handles hotlink protection, DASH, referer, etc.)
  const addBtn = $("#url-modal-add");
  const progressArea = $("#url-download-progress");
  addBtn.disabled = true;
  addBtn.textContent = "Downloading...";
  if (progressArea) progressArea.classList.remove("hidden");

  try {
    const result = await api.post("/api/cache-external", { url, name: rawName });
    if (result.error) {
      toast(result.error, "error");
      return;
    }

    const clip = result.clip;
    const data = await api.post("/api/theater", clip);
    state.theaterClips = data.clips || [];
    $("#url-modal-overlay").classList.add("hidden");
    toast(`Cached "${clip.name}"`, "success");
    if (state.currentView === "theater") renderTheater();
  } catch (err) {
    const msg = err?.message || "Download failed";
    toast(msg, "error");
  } finally {
    addBtn.disabled = false;
    addBtn.textContent = "Add";
    if (progressArea) progressArea.classList.add("hidden");
  }
}

async function loadPlaylists() {
  const data = await api.get("/api/playlists");
  state.playlists = data.playlists || [];
  renderPlaylists();
}

function renderPlaylists() {
  dom.playlistsList.innerHTML = "";
  const has = state.playlists.length > 0;
  dom.playlistsEmpty.classList.toggle("hidden", has);
  dom.playlistsList.classList.toggle("hidden", !has);

  if (!has) return;

  state.playlists.forEach((pl) => {
    const card = document.createElement("div");
    card.className = "playlist-card";
    card.innerHTML = `
      <div class="playlist-info">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
        <div>
          <span class="playlist-name">${pl.name}</span>
          <span class="playlist-count">${pl.clips.length} clips</span>
        </div>
      </div>
      <div class="playlist-actions">
        <button class="cyber-btn small" data-action="load" data-tooltip="Load into ${state.theaterName}">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          Load
        </button>
        <button class="icon-btn-sm danger-btn" data-action="delete" data-tooltip="Delete Playlist">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        </button>
      </div>
    `;

    card.querySelector('[data-action="load"]').addEventListener("click", async () => {
      await api.post(`/api/playlists/${encodeURIComponent(pl.name)}/load`);
      state.loadedPlaylistName = pl.name;
      toast(`Loaded "${pl.name}"`, "success");
      switchView("theater");
    });

    card.querySelector('[data-action="delete"]').addEventListener("click", async () => {
      if (!confirm(`Delete playlist "${pl.name}"?`)) return;
      await api.del(`/api/playlists/${encodeURIComponent(pl.name)}`);
      toast(`Deleted "${pl.name}"`, "info");
      loadPlaylists();
    });

    dom.playlistsList.appendChild(card);
  });
}

// ── Search ───────────────────────────────────────────────────
let searchTimeout;
dom.searchInput.addEventListener("input", (e) => {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => {
    state.searchQuery = e.target.value.trim().toLowerCase();
    if (state.searchQuery && state.currentFolder) {
      const filtered = state.currentVideos.filter(
        (v) => v.name.toLowerCase().includes(state.searchQuery) || v.filename.toLowerCase().includes(state.searchQuery)
      );
      renderVideoGrid(filtered);
    } else if (state.currentFolder) {
      renderVideoGrid(state.currentVideos);
    }
  }, 250);
});

// ── Utility Functions ────────────────────────────────────────
function getVideoSrc(clip) {
  if (clip.url) return clip.url;
  return `/api/stream/${encodeURIComponent(clip.path)}`;
}


function isDirectVideoUrl(url) {
  const videoExts = [".mp4", ".webm", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".m4v"];
  try {
    const pathname = new URL(url).pathname.toLowerCase();
    return videoExts.some((ext) => pathname.endsWith(ext));
  } catch {
    return false;
  }
}

function formatSize(bytes) {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function formatTime(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function parseTime(str) {
  if (!str) return null;
  str = str.trim();
  const parts = str.split(":");
  if (parts.length === 1) {
    const n = parseFloat(parts[0]);
    return isNaN(n) ? null : n;
  }
  if (parts.length === 2) {
    const m = parseInt(parts[0]);
    const s = parseInt(parts[1]);
    return isNaN(m) || isNaN(s) ? null : m * 60 + s;
  }
  if (parts.length === 3) {
    const h = parseInt(parts[0]);
    const m = parseInt(parts[1]);
    const s = parseInt(parts[2]);
    return isNaN(h) || isNaN(m) || isNaN(s) ? null : h * 3600 + m * 60 + s;
  }
  return null;
}

// ── Tooltips ─────────────────────────────────────────────────
document.addEventListener("mouseover", (e) => {
  const target = e.target.closest("[data-tooltip]");
  if (!target) return;

  document.querySelectorAll(".tooltip-popup").forEach((t) => t.remove());

  const tip = document.createElement("div");
  tip.className = "tooltip-popup";
  tip.textContent = target.dataset.tooltip;
  document.body.appendChild(tip);

  const rect = target.getBoundingClientRect();
  tip.style.left = `${rect.left + rect.width / 2 - tip.offsetWidth / 2}px`;
  tip.style.top = `${rect.top - tip.offsetHeight - 8}px`;

  const tipRect = tip.getBoundingClientRect();
  if (tipRect.left < 4) tip.style.left = "4px";
  if (tipRect.right > window.innerWidth - 4) tip.style.left = `${window.innerWidth - tip.offsetWidth - 4}px`;
  if (tipRect.top < 4) {
    tip.style.top = `${rect.bottom + 8}px`;
  }

  requestAnimationFrame(() => tip.classList.add("show"));
});

document.addEventListener("mouseout", (e) => {
  const target = e.target.closest("[data-tooltip]");
  if (target) {
    document.querySelectorAll(".tooltip-popup").forEach((t) => t.remove());
  }
});

// ── Spacebar Play/Pause ─────────────────────────────────────
document.addEventListener("keydown", (e) => {
  // Don't trigger when typing in input fields
  if (e.target.matches("input, textarea")) return;

  if (e.code === "Space") {
    // Video popup modal - toggle play/pause
    const popup = document.querySelector(".video-popup-overlay");
    if (popup) {
      e.preventDefault();
      const video = popup.querySelector("video");
      if (video) video.paused ? video.play() : video.pause();
      return;
    }

    // Workspace mode - toggle all videos
    if (state.workspaceOpen) {
      e.preventDefault();
      const videos = $$(".ws-video");
      const anyPlaying = [...videos].some((v) => !v.paused);
      videos.forEach((v) => (anyPlaying ? v.pause() : v.play().catch(() => {})));
      return;
    }
  }
});

// ── Settings ────────────────────────────────────────────────────
$("#btn-settings").addEventListener("click", openSettings);
$("#settings-close").addEventListener("click", closeSettings);
$("#btn-add-source").addEventListener("click", addNewSource);

$("#settings-overlay").addEventListener("click", (e) => {
  if (e.target === $("#settings-overlay")) closeSettings();
});

// Close settings on Escape
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#settings-overlay").classList.contains("hidden")) {
    closeSettings();
  }
});

// Enter key in source path input triggers add
$("#source-path-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addNewSource();
});

function openSettings() {
  $("#settings-overlay").classList.remove("hidden");
  loadSourcesList();
}

function closeSettings() {
  $("#settings-overlay").classList.add("hidden");
}

async function loadSourcesList() {
  const data = await api.get("/api/sources");
  const list = $("#sources-list");
  list.innerHTML = "";

  if (data.sources.length === 0) {
    list.innerHTML = `<div class="source-empty">No sources configured yet. Add a folder below.</div>`;
    return;
  }

  data.sources.forEach((src) => {
    const item = document.createElement("div");
    item.className = "source-item";
    item.innerHTML = `
      <div class="source-info">
        <span class="source-name">${src.name}</span>
        <span class="source-path">${src.path}</span>
        ${!src.exists ? '<span class="source-missing">Path not found</span>' : `<span class="source-stats">${src.folders} folders, ${src.videos} videos</span>`}
      </div>
      <button class="icon-btn-sm danger-btn" data-action="remove-source" data-index="${src.index}" data-tooltip="Remove Source">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    `;
    item.querySelector('[data-action="remove-source"]').addEventListener("click", async (e) => {
      const index = e.currentTarget.dataset.index;
      if (!confirm(`Remove source "${src.name}"?\nThis only removes it from the app \u2014 no files are deleted.`)) return;
      await api.del(`/api/sources/${index}`);
      toast(`Removed "${src.name}"`, "info");
      loadSourcesList();
      await loadFolders();
      showFolderGrid();
    });

    list.appendChild(item);
  });
}

async function addNewSource() {
  const nameInput = $("#source-name-input");
  const pathInput = $("#source-path-input");
  const name = nameInput.value.trim();
  const path = pathInput.value.trim();

  if (!path) {
    toast("Enter a folder path", "error");
    pathInput.focus();
    return;
  }

  const result = await api.post("/api/sources", { name, path });
  if (result.error) {
    toast(result.error, "error");
    return;
  }
  toast(`Added "${name || path}"`, "success");
  nameInput.value = "";
  pathInput.value = "";
  loadSourcesList();
  await loadFolders();
  showFolderGrid();
}

// ── Folder Picker ───────────────────────────────────────────────
let folderPickerPath = "";

$("#btn-browse-folder").addEventListener("click", openFolderPicker);
$("#folder-picker-close").addEventListener("click", closeFolderPicker);
$("#folder-picker-cancel").addEventListener("click", closeFolderPicker);

$("#folder-picker-overlay").addEventListener("click", (e) => {
  if (e.target === $("#folder-picker-overlay")) closeFolderPicker();
});

$("#folder-picker-select").addEventListener("click", () => {
  if (folderPickerPath) {
    if (state._collectionPickerMode) {
      // Came from New Collection modal
      $("#collection-path-input").value = folderPickerPath;
      state._collectionPickerMode = false;
    } else {
      // Normal settings source picker
      $("#source-path-input").value = folderPickerPath;
    }
    closeFolderPicker();
  }
});

function openFolderPicker() {
  $("#folder-picker-overlay").classList.remove("hidden");
  folderPickerPath = "";
  browseTo("");
}

function closeFolderPicker() {
  $("#folder-picker-overlay").classList.add("hidden");
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#folder-picker-overlay").classList.contains("hidden")) {
    closeFolderPicker();
  }
});

async function browseTo(path) {
  const url = path
    ? `/api/browse-folders?path=${encodeURIComponent(path)}`
    : "/api/browse-folders";

  const data = await api.get(url);
  if (data.error && !data.dirs) {
    toast(data.error, "error");
    return;
  }

  folderPickerPath = data.path || "";
  const pathDisplay = $("#folder-picker-path");
  pathDisplay.textContent = folderPickerPath || "Select a location...";

  const list = $("#folder-picker-list");
  list.innerHTML = "";

  // Parent directory button
  if (data.parent !== null && data.parent !== undefined) {
    const parentItem = document.createElement("div");
    parentItem.className = "folder-picker-item parent-dir";
    parentItem.innerHTML = `
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
      <span>.. (up one level)</span>
    `;
    parentItem.addEventListener("click", () => browseTo(data.parent));
    list.appendChild(parentItem);
  }

  if (data.dirs.length === 0 && data.parent !== null) {
    const empty = document.createElement("div");
    empty.className = "folder-picker-empty";
    empty.textContent = "No subfolders";
    list.appendChild(empty);
    return;
  }

  data.dirs.forEach((dir) => {
    const item = document.createElement("div");
    item.className = "folder-picker-item";
    item.innerHTML = `
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
      <span>${dir.name}</span>
    `;
    item.addEventListener("click", () => browseTo(dir.path));
    list.appendChild(item);
  });
}

// ── Shutdown Server ──────────────────────────────────────────
$("#btn-shutdown").addEventListener("click", async () => {
  if (!confirm("Stop the server? The page will become unresponsive.")) return;
  try {
    await api.post("/api/shutdown", {});
    document.body.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#0a0a0f;color:#55556a;font-family:Inter,sans-serif;flex-direction:column;gap:12px;">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#ff3355" stroke-width="1.5"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
        <p style="font-size:16px;">Server stopped. You can close this tab.</p>
      </div>`;
  } catch (e) {
    // Server already shutting down
  }
});

// ── Branding ─────────────────────────────────────────────────
async function loadBranding() {
  try {
    const data = await api.get("/api/branding");
    state.siteName = data.siteName || "My Collection";
    state.theaterName = data.theaterName || "My Theater";
  } catch (e) {
    // Defaults already set in state
  }
  applyBranding();
}

function applyBranding() {
  const { siteName, theaterName } = state;

  // Page title
  document.title = siteName;

  // Logo text
  const logoEl = $("#brand-site-name");
  if (logoEl) logoEl.textContent = siteName.toUpperCase();

  // Theater nav tooltip
  const navBtn = $("#nav-theater-btn");
  if (navBtn) navBtn.dataset.tooltip = theaterName;

  // Theater heading
  const headingEl = $("#brand-theater-heading");
  if (headingEl) headingEl.textContent = theaterName;

  // Theater empty state
  const emptyEl = $("#brand-theater-empty");
  if (emptyEl) emptyEl.textContent = `No clips in ${theaterName.toLowerCase()} yet`;

  // Playlists hint
  const hintEl = $("#brand-playlists-hint");
  if (hintEl) hintEl.textContent = `Add clips to ${theaterName} and save them as a playlist`;

  // Static tooltips that reference theater name
  const addTheaterBtn = $("#btn-add-theater");
  if (addTheaterBtn) addTheaterBtn.dataset.tooltip = `Add to ${theaterName}`;

  // Settings inputs (prefill current values)
  const siteInput = $("#branding-site-name");
  const theaterInput = $("#branding-theater-name");
  if (siteInput) siteInput.value = siteName;
  if (theaterInput) theaterInput.value = theaterName;
}

$("#btn-save-branding").addEventListener("click", async () => {
  const siteName = $("#branding-site-name").value.trim();
  const theaterName = $("#branding-theater-name").value.trim();
  const data = await api.post("/api/branding", { siteName, theaterName });
  state.siteName = data.siteName;
  state.theaterName = data.theaterName;
  applyBranding();
  toast("Names updated", "success");
});

// ══════════════════════════════════════════════════════════════
// ── COLLECTIONS: Create, Download, Import, Delete ────────────
// ══════════════════════════════════════════════════════════════

// ── New Collection Modal ────────────────────────────────────
let collectionPickerPath = "";

function openNewCollectionModal() {
  $("#new-collection-overlay").classList.remove("hidden");
  $("#collection-name-input").value = "";
  $("#collection-path-input").value = "";
  collectionPickerPath = "";
  $("#collection-name-input").focus();
}

function closeNewCollectionModal() {
  $("#new-collection-overlay").classList.add("hidden");
}

$("#collection-modal-cancel").addEventListener("click", closeNewCollectionModal);
$("#new-collection-overlay").addEventListener("click", (e) => {
  if (e.target === $("#new-collection-overlay")) closeNewCollectionModal();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#new-collection-overlay").classList.contains("hidden")) {
    closeNewCollectionModal();
  }
});

// Browse button inside collection modal — reuse folder picker with a callback
$("#collection-browse-btn").addEventListener("click", () => {
  // Open the existing folder picker but set a flag so Select populates the collection path
  state._collectionPickerMode = true;
  openFolderPicker();
});

// Enter key in collection name
$("#collection-name-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") createCollection();
  if (e.key === "Escape") closeNewCollectionModal();
});

$("#collection-modal-create").addEventListener("click", createCollection);

async function createCollection() {
  const name = $("#collection-name-input").value.trim();
  const path = $("#collection-path-input").value.trim();

  if (!name) {
    toast("Enter a collection name", "error");
    $("#collection-name-input").focus();
    return;
  }
  if (!path) {
    toast("Choose a location first", "error");
    return;
  }

  try {
    const result = await api.post("/api/collections", { path, name });
    if (result.error) {
      toast(result.error, "error");
      return;
    }
    closeNewCollectionModal();
    toast(`Created "${name}"`, "success");
    await loadFolders();
    // Open the new collection
    openFolder(name, result.sourceIndex);
  } catch (err) {
    toast("Failed to create collection", "error");
  }
}

// ── Download URL to Folder ──────────────────────────────────
$("#btn-download-url-folder").addEventListener("click", () => {
  $("#folder-dl-modal-overlay").classList.remove("hidden");
  $("#folder-dl-url-input").value = "";
  $("#folder-dl-name-input").value = "";
  $("#folder-dl-url-input").focus();
});

$("#folder-dl-cancel").addEventListener("click", () => {
  $("#folder-dl-modal-overlay").classList.add("hidden");
});

$("#folder-dl-modal-overlay").addEventListener("click", (e) => {
  if (e.target === $("#folder-dl-modal-overlay")) {
    $("#folder-dl-modal-overlay").classList.add("hidden");
  }
});

$("#folder-dl-url-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") downloadToFolder();
  if (e.key === "Escape") $("#folder-dl-modal-overlay").classList.add("hidden");
});

$("#folder-dl-name-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") downloadToFolder();
  if (e.key === "Escape") $("#folder-dl-modal-overlay").classList.add("hidden");
});

$("#folder-dl-download").addEventListener("click", downloadToFolder);

async function downloadToFolder() {
  const url = $("#folder-dl-url-input").value.trim();
  if (!url) {
    toast("Paste a video URL", "error");
    return;
  }

  try { new URL(url); } catch {
    toast("Invalid URL", "error");
    return;
  }

  const name = $("#folder-dl-name-input").value.trim();
  const btn = $("#folder-dl-download");
  const progress = $("#folder-dl-progress");

  btn.disabled = true;
  btn.textContent = "Downloading...";
  progress.classList.remove("hidden");

  try {
    const result = await api.post("/api/folder-download", {
      url,
      folder: state.currentFolder,
      sourceIndex: state.currentSourceIndex,
      name: name || undefined,
    });

    if (result.error) {
      toast(result.error, "error");
      return;
    }

    $("#folder-dl-modal-overlay").classList.add("hidden");
    toast(`Downloaded "${result.name}"`, "success");
    // Refresh the current folder
    await openFolder(state.currentFolder, state.currentSourceIndex);
    await loadFolders(); // Update counts
  } catch (err) {
    toast("Download failed", "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Download";
    progress.classList.add("hidden");
  }
}

// ── Import Videos ───────────────────────────────────────────
let importPickerPath = "";
let importSelectedFiles = [];

$("#btn-import-video").addEventListener("click", openImportModal);
$("#import-modal-close").addEventListener("click", closeImportModal);
$("#import-modal-cancel").addEventListener("click", closeImportModal);

$("#import-modal-overlay").addEventListener("click", (e) => {
  if (e.target === $("#import-modal-overlay")) closeImportModal();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#import-modal-overlay").classList.contains("hidden")) {
    closeImportModal();
  }
});

$("#import-modal-import").addEventListener("click", importSelectedVideos);

function openImportModal() {
  $("#import-modal-overlay").classList.remove("hidden");
  importSelectedFiles = [];
  updateImportCount();
  importBrowseTo("");
}

function closeImportModal() {
  $("#import-modal-overlay").classList.add("hidden");
  importSelectedFiles = [];
}

function updateImportCount() {
  const count = importSelectedFiles.length;
  $("#import-selected-count").textContent = `${count} selected`;
  $("#import-modal-import").disabled = count === 0;
}

async function importBrowseTo(path) {
  const url = path
    ? `/api/browse-files?path=${encodeURIComponent(path)}`
    : "/api/browse-files";

  let data;
  try {
    data = await api.get(url);
  } catch {
    toast("Failed to browse", "error");
    return;
  }

  if (data.error && !data.dirs) {
    toast(data.error, "error");
    return;
  }

  importPickerPath = data.path || "";
  $("#import-picker-path").textContent = importPickerPath || "Select a location...";

  const list = $("#import-picker-list");
  list.innerHTML = "";

  // Parent directory
  if (data.parent !== null && data.parent !== undefined) {
    const parentItem = document.createElement("div");
    parentItem.className = "folder-picker-item parent-dir";
    parentItem.innerHTML = `
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
      <span>.. (up one level)</span>
    `;
    parentItem.addEventListener("click", () => importBrowseTo(data.parent));
    list.appendChild(parentItem);
  }

  // Directories
  if (data.dirs) {
    data.dirs.forEach((dir) => {
      const item = document.createElement("div");
      item.className = "folder-picker-item";
      item.innerHTML = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <span>${dir.name}</span>
      `;
      item.addEventListener("click", () => importBrowseTo(dir.path));
      list.appendChild(item);
    });
  }

  // Video files with checkboxes
  if (data.files && data.files.length > 0) {
    data.files.forEach((file) => {
      const item = document.createElement("div");
      item.className = "folder-picker-item import-file-item";
      const isChecked = importSelectedFiles.some((f) => f.path === file.path);
      item.innerHTML = `
        <input type="checkbox" class="import-checkbox" data-path="${file.path}" ${isChecked ? "checked" : ""} />
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        <span class="import-file-name">${file.name}</span>
        <span class="import-file-size">${formatSize(file.size)}</span>
      `;

      const checkbox = item.querySelector(".import-checkbox");
      item.addEventListener("click", (e) => {
        if (e.target !== checkbox) {
          checkbox.checked = !checkbox.checked;
        }
        if (checkbox.checked) {
          if (!importSelectedFiles.some((f) => f.path === file.path)) {
            importSelectedFiles.push(file);
          }
        } else {
          importSelectedFiles = importSelectedFiles.filter((f) => f.path !== file.path);
        }
        updateImportCount();
      });

      list.appendChild(item);
    });
  }

  // Empty state
  if ((!data.dirs || data.dirs.length === 0) && (!data.files || data.files.length === 0) && data.parent !== null) {
    const empty = document.createElement("div");
    empty.className = "folder-picker-empty";
    empty.textContent = "No folders or video files here";
    list.appendChild(empty);
  }
}

async function importSelectedVideos() {
  if (importSelectedFiles.length === 0) return;

  const btn = $("#import-modal-import");
  btn.disabled = true;
  btn.textContent = "Importing...";

  let successCount = 0;
  for (const file of importSelectedFiles) {
    try {
      const result = await api.post("/api/folder-import", {
        sourcePath: file.path,
        folder: state.currentFolder,
        sourceIndex: state.currentSourceIndex,
      });
      if (!result.error) successCount++;
    } catch {
      // Continue with next file
    }
  }

  btn.disabled = false;
  btn.textContent = "Import Selected";

  closeImportModal();
  toast(`Imported ${successCount} video${successCount !== 1 ? "s" : ""}`, "success");

  // Refresh current folder
  await openFolder(state.currentFolder, state.currentSourceIndex);
  await loadFolders();
}

// ── Delete Video ────────────────────────────────────────────
async function deleteVideo(video) {
  if (!confirm(`Delete "${video.name}"?\nThis permanently removes the file from disk.`)) return;

  try {
    const result = await api.delBody("/api/folder-video", { path: video.path });
    if (result.error) {
      toast(result.error, "error");
      return;
    }
    toast(`Deleted "${video.name}"`, "info");
    // Refresh
    await openFolder(state.currentFolder, state.currentSourceIndex);
    await loadFolders();
  } catch (err) {
    toast("Failed to delete video", "error");
  }
}

// ── Init ─────────────────────────────────────────────────────
async function init() {
  await loadBranding();
  await loadFolders();
  showFolderGrid();
}

init();
