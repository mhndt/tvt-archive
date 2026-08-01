const VERSION = "0.8.1";
const $esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pad = (n) => String(n).padStart(2, "0");
const localDate = (d = new Date()) => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
const secToClock = (seconds) => {
  const s = Math.max(0, Math.min(86399, Math.round(seconds)));
  return `${pad(Math.floor(s/3600))}:${pad(Math.floor((s%3600)/60))}:${pad(s%60)}`;
};
const clockToSec = (value) => {
  const p = String(value || "00:00:00").split(":").map(Number);
  return (p[0] || 0) * 3600 + (p[1] || 0) * 60 + (p[2] || 0);
};
const isoFor = (date, time) => `${date}T${time.length === 5 ? `${time}:00` : time}`;
const historyText = (hours) => {
  if (!Number.isFinite(Number(hours))) return "—";
  const minutes = Math.max(0, Math.round(Number(hours) * 60));
  const days = Math.floor(minutes / 1440), hrs = Math.floor((minutes % 1440) / 60), mins = minutes % 60;
  return days ? `${days}d ${hrs}h` : hrs ? `${hrs}h ${mins}m` : `${mins}m`;
};
const diskText = (value) => ({normal:"Normal",read_only:"Read-only",formatting:"Formatting",unformatted:"Unformatted",error:"Error",no_sd_card:"No SD card",unknown:"Unknown"})[value] || "Unavailable";

const errorText = (error) => {
  if (typeof error === "string") return error;
  if (error?.message && typeof error.message === "string") return error.message;
  const candidates = [error?.error, error?.body, error?.data, error?.response];
  for (const value of candidates) {
    if (typeof value === "string") {
      try {
        const parsed = JSON.parse(value);
        if (typeof parsed?.error === "string") return parsed.error;
        if (typeof parsed?.message === "string") return parsed.message;
      } catch (_) {}
      return value;
    }
    if (value && typeof value === "object") {
      if (typeof value.error === "string") return value.error;
      if (typeof value.message === "string") return value.message;
    }
  }
  try {
    const encoded = JSON.stringify(error);
    if (encoded && encoded !== "{}") return encoded;
  } catch (_) {}
  return "Unexpected error";
};


let hlsLibraryPromise = null;
const loadHlsLibrary = (url) => {
  if (window.Hls?.isSupported) return Promise.resolve(window.Hls);
  if (!url) return Promise.reject(new Error("The local HLS player URL is missing."));
  if (hlsLibraryPromise) return hlsLibraryPromise;
  hlsLibraryPromise = new Promise((resolve, reject) => {
    const existing = document.getElementById("tvt-archive-hlsjs");
    const finish = () => window.Hls?.isSupported ? resolve(window.Hls) : reject(new Error("The local HLS player could not be loaded."));
    if (existing) {
      if (window.Hls) { finish(); return; }
      existing.addEventListener("load", finish, {once:true});
      existing.addEventListener("error", () => reject(new Error("The local HLS player could not be loaded.")), {once:true});
      return;
    }
    const script = document.createElement("script");
    script.id = "tvt-archive-hlsjs";
    script.src = url;
    script.async = true;
    script.crossOrigin = "anonymous";
    script.addEventListener("load", finish, {once:true});
    script.addEventListener("error", () => reject(new Error("The local HLS player could not be loaded.")), {once:true});
    document.head.appendChild(script);
  }).catch((error) => {
    hlsLibraryPromise = null;
    document.getElementById("tvt-archive-hlsjs")?.remove();
    throw error;
  });
  return hlsLibraryPromise;
};

class TVTFmp4Player {
  constructor(video, url, playerScriptUrl, mimeType, startBufferSeconds, onReady, onError, onState) {
    this.video = video;
    this.url = url || null;
    this.playerScriptUrl = playerScriptUrl || null;
    this.mimeType = mimeType || 'video/mp4; codecs="avc1.640029, mp4a.40.2"';
    this.onReady = onReady;
    this.onError = onError;
    this.onState = onState;
    this.hls = null;
    this.destroyed = false;
    this.started = false;
    this.serverComplete = false;
    this.rebuffering = false;
    this.videoEventsBound = false;
    this.rateTimer = null;
    this.adaptiveRate = 1;
    this.mediaRecoveryAttempts = 0;
    this.networkRecoveryTimer = null;
    this.startBufferSeconds = Math.max(2, Math.min(12, Number(startBufferSeconds || 3)));
    // Firefox's decoded range can be a few hundred milliseconds shorter than
    // the playlist duration because of fMP4 timestamp and audio alignment.
    this.startBufferToleranceSeconds = 0.45;
    this.resumeBufferSeconds = 0.6;
    this._boundSeeking = () => {
      this.rebuffering = false;
      this._setPlaybackRate(1);
      this._state("seeking");
    };
    this._boundSeeked = () => {
      if (!this.video.paused) this._state("playing");
      this._adaptiveTick();
    };
    this._boundWaiting = () => this._handleWaiting();
    this._boundStalled = () => this._handleWaiting();
    this._boundEnded = () => {
      if (!this.serverComplete && !this.destroyed) this._enterRebuffer();
    };
    this._boundPlaying = () => {
      if (this.started && !this.rebuffering) this._state("playing");
    };
    this._boundProgress = () => this._bufferChanged();
  }

  _state(name, detail = {}) { this.onState?.(name, detail); }

  _bindVideoEvents() {
    if (this.videoEventsBound) return;
    this.videoEventsBound = true;
    this.video.addEventListener("seeking", this._boundSeeking);
    this.video.addEventListener("seeked", this._boundSeeked);
    this.video.addEventListener("waiting", this._boundWaiting);
    this.video.addEventListener("stalled", this._boundStalled);
    this.video.addEventListener("ended", this._boundEnded);
    this.video.addEventListener("playing", this._boundPlaying);
    this.video.addEventListener("progress", this._boundProgress);
    this.video.addEventListener("durationchange", this._boundProgress);
    this.video.addEventListener("loadedmetadata", this._boundProgress);
  }

  prime() {
    if (this.destroyed) return;
    this._bindVideoEvents();
    this._state("opening");
  }

  async start() {
    this.prime();
    await this.load(this.url, this.playerScriptUrl, this.mimeType, this.startBufferSeconds);
  }

  async load(url, playerScriptUrl, mimeType, startBufferSeconds) {
    if (this.destroyed) return;
    this.url = url;
    this.playerScriptUrl = playerScriptUrl || this.playerScriptUrl;
    if (mimeType) this.mimeType = mimeType;
    if (startBufferSeconds) {
      this.startBufferSeconds = Math.max(2, Math.min(12, Number(startBufferSeconds)));
      this.resumeBufferSeconds = 0.6;
    }
    this._bindVideoEvents();
    this._state("buffering", {ahead:0, target:this.startBufferSeconds});

    if (this.video.canPlayType("application/vnd.apple.mpegurl")) {
      this.video.src = this.url;
      this.video.load();
      return;
    }

    const Hls = await loadHlsLibrary(this.playerScriptUrl);
    if (this.destroyed) return;
    if (!Hls.isSupported()) throw new Error("This browser does not support recorded HLS playback.");
    this.hls = new Hls({
      autoStartLoad: true,
      startPosition: 0,
      lowLatencyMode: false,
      enableWorker: false,
      startFragPrefetch: true,
      maxBufferHole: 0.5,
      maxBufferLength: 30,
      maxMaxBufferLength: 120,
      backBufferLength: 180,
      liveBackBufferLength: 180,
      liveDurationInfinity: false,
      highBufferWatchdogPeriod: 1,
      nudgeOffset: 0.1,
      nudgeMaxRetry: 5,
      manifestLoadingTimeOut: 10000,
      manifestLoadingMaxRetry: 20,
      manifestLoadingRetryDelay: 250,
      levelLoadingTimeOut: 10000,
      levelLoadingMaxRetry: 20,
      levelLoadingRetryDelay: 250,
      fragLoadingTimeOut: 20000,
      fragLoadingMaxRetry: 20,
      fragLoadingRetryDelay: 250,
    });
    this.hls.on(Hls.Events.MEDIA_ATTACHED, () => {
      if (!this.destroyed) this.hls?.loadSource(this.url);
    });
    this.hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (this.destroyed) return;
      this._state("buffering", {ahead:this._bufferedAhead(), target:this.startBufferSeconds});
      this._bufferChanged();
    });
    const buffered = () => this._bufferChanged();
    this.hls.on(Hls.Events.BUFFER_APPENDED, buffered);
    this.hls.on(Hls.Events.FRAG_BUFFERED, buffered);
    this.hls.on(Hls.Events.LEVEL_UPDATED, buffered);
    this.hls.on(Hls.Events.ERROR, (_event, data) => this._handleHlsError(Hls, data));
    this.hls.attachMedia(this.video);
  }

  _handleHlsError(Hls, data) {
    if (this.destroyed || !data) return;
    if (!data.fatal) {
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR && this.started && this._bufferedAhead() < 0.15) this._enterRebuffer();
      return;
    }
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
      clearTimeout(this.networkRecoveryTimer);
      this._state(this.started ? "rebuffering" : "buffering", {
        ahead:this._bufferedAhead(),
        target:this.started ? this.resumeBufferSeconds : this.startBufferSeconds,
      });
      this.networkRecoveryTimer = setTimeout(() => {
        if (!this.destroyed) this.hls?.startLoad(Math.max(0, Number(this.video.currentTime || 0)));
      }, 500);
      return;
    }
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR && this.mediaRecoveryAttempts < 3) {
      this.mediaRecoveryAttempts += 1;
      this.hls?.recoverMediaError();
      return;
    }
    this._fatal(`Recording playback failed${data.details ? `: ${data.details}` : ""}`);
  }

  _bufferChanged() {
    if (this.destroyed) return;
    this._maybeStart();
    this._maybeRecover();
  }

  _bufferWindow() {
    const ranges = this.video.buffered;
    if (!ranges.length) return null;
    const now = Number(this.video.currentTime || 0);
    let index = -1;
    for (let i = 0; i < ranges.length; i += 1) {
      if (now >= ranges.start(i) - 0.35 && now <= ranges.end(i) + 0.35) { index = i; break; }
      if (now < ranges.start(i)) { index = i; break; }
    }
    if (index < 0) return null;
    const start = ranges.start(index);
    let end = ranges.end(index);
    for (let i = index + 1; i < ranges.length; i += 1) {
      if (ranges.start(i) - end > 0.5) break;
      end = Math.max(end, ranges.end(i));
    }
    const position = Math.max(start, now);
    return {start, end, ahead:Math.max(0, end - position)};
  }

  _bufferedAhead() { return this._bufferWindow()?.ahead || 0; }

  _nextPlayableTime() {
    const ranges = this.video.buffered;
    if (!ranges.length) return null;
    const now = Number(this.video.currentTime || 0);
    for (let i = 0; i < ranges.length; i += 1) {
      if (now < ranges.end(i) - 0.05) return Math.max(now, ranges.start(i) + 0.02);
    }
    return null;
  }

  _maybeStart() {
    if (this.started || this.destroyed || !this.video.buffered.length) return;
    const window = this._bufferWindow();
    const ahead = window?.ahead || 0;
    this._state("buffering", {ahead, target:this.startBufferSeconds});
    const startupThreshold = Math.max(2.25, this.startBufferSeconds - this.startBufferToleranceSeconds);
    if (ahead + 0.05 < startupThreshold && !this.serverComplete) return;
    if (!window || ahead <= 0.05) return;
    this.started = true;
    this.rebuffering = false;
    try { this.video.currentTime = window.start + 0.03; } catch (_) {}
    this.video.controls = true;
    this._startAdaptiveClock();
    this.onReady?.();
    this.resume();
  }

  resume() {
    if (this.destroyed || !this.started) return;
    const next = this._nextPlayableTime();
    if (next != null && (this.video.ended || this._bufferedAhead() < 0.05)) {
      try { this.video.currentTime = next; } catch (_) {}
    }
    const result = this.video.play();
    if (result?.then) {
      result.then(() => this._state("playing")).catch(() => {
        this.video.controls = true;
        this._state("play-required");
      });
    }
  }

  _setPlaybackRate(rate) {
    const normalized = Math.max(0.65, Math.min(1.03, Number(rate || 1)));
    if (Math.abs(normalized - this.adaptiveRate) < 0.005) return;
    this.adaptiveRate = normalized;
    try {
      this.video.defaultPlaybackRate = normalized;
      this.video.playbackRate = normalized;
    } catch (_) {}
  }

  _handleWaiting() {
    if (this.destroyed || !this.started || this.serverComplete || this.video.seeking) return;
    this._enterRebuffer();
  }

  _enterRebuffer() {
    if (this.destroyed || this.rebuffering || this.serverComplete) return;
    this.rebuffering = true;
    this._setPlaybackRate(0.65);
    // Do not deliberately pause for another three seconds. The browser is
    // already starved; resume as soon as the next short fragment arrives.
    this.hls?.startLoad(Math.max(0, Number(this.video.currentTime || 0)));
    this._state("rebuffering", {ahead:this._bufferedAhead(), target:this.resumeBufferSeconds});
  }

  _maybeRecover() {
    if (!this.rebuffering || this.destroyed) return;
    const ahead = this._bufferedAhead();
    this._state("rebuffering", {ahead, target:this.resumeBufferSeconds});
    if (ahead + 0.03 < this.resumeBufferSeconds && !this.serverComplete) return;
    if (ahead <= 0.05) return;
    this.rebuffering = false;
    this.resume();
  }

  _adaptiveTick() {
    if (this.destroyed || !this.started) return;
    if (this.rebuffering) { this._maybeRecover(); return; }
    if (this.video.seeking || !this.video.buffered.length) {
      this._setPlaybackRate(1);
      return;
    }
    if (this.video.ended && !this.serverComplete) {
      this._enterRebuffer();
      return;
    }
    if (this.video.paused) return;
    const ahead = this._bufferedAhead();
    let target = 1;
    if (!this.serverComplete) {
      // NVMS-like clock control: slow down before starvation instead of
      // repeatedly consuming the whole local buffer at a rigid 1.00x.
      if (ahead < 0.7) target = 0.65;
      else if (ahead < 1.3) target = 0.74;
      else if (ahead < 2.2) target = 0.84;
      else if (ahead < 3.5) target = 0.92;
      else if (ahead < 5.5) target = 0.97;
      else if (ahead > 10) target = 1.02;
    }
    this._setPlaybackRate(target);
  }

  _startAdaptiveClock() {
    if (this.rateTimer || this.destroyed) return;
    this.rateTimer = setInterval(() => this._adaptiveTick(), 400);
    this._adaptiveTick();
  }

  setComplete(complete) {
    if (!complete || this.serverComplete) return;
    this.serverComplete = true;
    this._setPlaybackRate(1);
    this._maybeStart();
    this._maybeRecover();
  }

  _fatal(message) {
    if (this.destroyed) return;
    this.onError?.(message || "The recording player failed.");
    this.destroy();
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    clearInterval(this.rateTimer);
    clearTimeout(this.networkRecoveryTimer);
    if (this.videoEventsBound) {
      this.video.removeEventListener("seeking", this._boundSeeking);
      this.video.removeEventListener("seeked", this._boundSeeked);
      this.video.removeEventListener("waiting", this._boundWaiting);
      this.video.removeEventListener("stalled", this._boundStalled);
      this.video.removeEventListener("ended", this._boundEnded);
      this.video.removeEventListener("playing", this._boundPlaying);
      this.video.removeEventListener("progress", this._boundProgress);
      this.video.removeEventListener("durationchange", this._boundProgress);
      this.video.removeEventListener("loadedmetadata", this._boundProgress);
    }
    this._setPlaybackRate(1);
    try { this.hls?.destroy(); } catch (_) {}
    this.hls = null;
    try { this.video.pause(); this.video.removeAttribute("src"); this.video.load(); } catch (_) {}
  }
}
class TVTArchivePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._hass = null;
    this._loaded = false;
    this._entryId = null;
    this._cameras = [];
    this._cameraId = null;
    this._camera = null;
    this._date = localDate();
    const now = new Date();
    this._selectedSec = now.getHours() * 3600 + now.getMinutes() * 60;
    this._rangeStart = secToClock(this._selectedSec);
    this._rangeEnd = secToClock(Math.min(86399, this._selectedSec + 300));
    this._zoom = 1;
    this._recordingQuality = "original";
    this._liveProfileId = null;
    this._mode = "live";
    this._timeline = null;
    this._status = null;
    this._busy = false;
    this._downloadJobId = null;
    this._downloadUrl = null;
    this._downloadFilename = null;
    this._downloadPercent = 0;
    this._downloadPhase = "";
    this._message = "";
    this._error = "";
    this._playbackSession = null;
    this._recordingPlayer = null;
    this._recordingController = null;
    this._pollTimer = null;
    this._sessionPollTimer = null;
    this._playbackRetryTimer = null;
    this._playbackRetryCount = 0;
    this._playbackRetryPending = false;
    this._statusTimer = null;
    this._narrow = false;
  }

  set hass(value) {
    this._hass = value;
    if (!this._loaded && value) {
      this._loaded = true;
      this._bootstrap();
    } else if (value) {
      this._refreshLivePlayer();
    }
  }
  get hass() { return this._hass; }
  set narrow(value) {
    const changed = this._narrow !== Boolean(value);
    this._narrow = Boolean(value);
    if (changed && !this._playbackSession?.playlist_url) this._render();
  }
  set panel(value) { this._panel = value; }
  set route(value) { this._route = value; }

  connectedCallback() {
    this._render();
    if (this._loaded && this._downloadJobId) {
      clearTimeout(this._pollTimer);
      this._busy = true;
      this._pollDownload(this._downloadJobId);
    }
  }
  disconnectedCallback() {
    clearTimeout(this._pollTimer);
    clearTimeout(this._sessionPollTimer);
    clearTimeout(this._playbackRetryTimer);
    clearTimeout(this._statusTimer);
    this._stopPlaybackSession(false);
  }

  async _api(method, path, data) {
    if (!this._hass) throw new Error("Home Assistant is not ready");
    return this._hass.callApi(method, path.replace(/^\/api\//, ""), data);
  }

  _stateKey() { return `tvt_archive_panel:${this._entryId || "default"}`; }

  _restoreState() {
    try {
      const value = JSON.parse(sessionStorage.getItem(this._stateKey()) || "null");
      if (!value || typeof value !== "object") return;
      if (/^\d{4}-\d{2}-\d{2}$/.test(value.date || "")) this._date = value.date;
      if (typeof value.cameraId === "string") this._cameraId = value.cameraId;
      if (["live","recording"].includes(value.mode)) this._mode = value.mode;
      if (["original","balanced","data_saver"].includes(value.recordingQuality || value.quality)) this._recordingQuality = value.recordingQuality || value.quality;
      else if ((value.recordingQuality || value.quality) === "auto") this._recordingQuality = "original";
      if (typeof value.liveProfileId === "string") this._liveProfileId = value.liveProfileId;
      if ([1,2,4].includes(Number(value.zoom))) this._zoom = Number(value.zoom);
      if (Number.isFinite(Number(value.selectedSec))) this._selectedSec = Math.max(0, Math.min(86399, Number(value.selectedSec)));
      if (/^\d{2}:\d{2}:\d{2}$/.test(value.rangeStart || "")) this._rangeStart = value.rangeStart;
      if (/^\d{2}:\d{2}:\d{2}$/.test(value.rangeEnd || "")) this._rangeEnd = value.rangeEnd;
      if (typeof value.downloadJobId === "string" && /^[a-f0-9]{32}$/.test(value.downloadJobId)) this._downloadJobId = value.downloadJobId;
      if (Number.isFinite(Number(value.downloadPercent))) this._downloadPercent = Math.max(0, Math.min(100, Math.round(Number(value.downloadPercent))));
    } catch (_) {}
  }

  _applyNavigationParams() {
    try {
      const params = new URL(window.location.href).searchParams;
      const camera = params.get("camera");
      const mode = params.get("mode");
      const date = params.get("date");
      const time = params.get("time");
      if (camera) this._cameraId = camera;
      if (["live","recording"].includes(mode)) this._mode = mode;
      if (/^\d{4}-\d{2}-\d{2}$/.test(date || "")) this._date = date;
      if (/^\d{2}:\d{2}(?::\d{2})?$/.test(time || "")) {
        this._selectedSec = clockToSec(time);
        this._rangeStart = secToClock(this._selectedSec);
        this._rangeEnd = secToClock(Math.min(86399, this._selectedSec + 300));
      }
    } catch (_) {}
  }

  _persistState() {
    try {
      sessionStorage.setItem(this._stateKey(), JSON.stringify({
        date:this._date, cameraId:this._cameraId, mode:this._mode, recordingQuality:this._recordingQuality, liveProfileId:this._liveProfileId,
        zoom:this._zoom, selectedSec:this._selectedSec, rangeStart:this._rangeStart, rangeEnd:this._rangeEnd,
        downloadJobId:this._downloadJobId, downloadPercent:this._downloadPercent,
      }));
    } catch (_) {}
  }

  async _bootstrap() {
    try {
      const entries = await this._api("GET", "tvt_archive/entries");
      if (!entries.entries?.length) throw new Error("No TVT Archive integration is configured");
      this._entryId = this._panel?.config?.entry_id || entries.entries[0].entry_id;
      this._restoreState();
      this._applyNavigationParams();
      await this._loadCameras();
      this._persistState();
      if (!this._cameras.length) throw new Error("No cameras are configured. Open Settings → Devices & services → TVT Archive → Configure.");
      await Promise.all([this._loadTimeline(), this._loadStatus()]);
      if (this._downloadJobId) {
        this._busy = true;
        this._render();
        this._pollDownload(this._downloadJobId);
      }
    } catch (error) {
      this._error = errorText(error);
      this._render();
    }
  }

  async _loadCameras() {
    const payload = await this._api("GET", `tvt_archive/${this._entryId}/cameras`);
    this._cameras = payload.cameras || [];
    if (!this._cameraId || !this._cameras.some((camera) => camera.id === this._cameraId)) {
      this._cameraId = this._cameras[0]?.id || null;
    }
    this._camera = this._cameras.find((camera) => camera.id === this._cameraId) || null;
    this._ensureLiveProfileSelection();
  }

  async _loadTimeline(refresh = false) {
    if (!this._cameraId) return;
    const showLoading = !this._busy;
    if (showLoading) {
      this._message = "Loading timeline…";
      if (!this._playbackSession?.playlist_url) this._render(); else this._updateStatusLine();
    }
    try {
      this._timeline = await this._api("GET", `tvt_archive/${this._entryId}/cameras/${this._cameraId}/timeline?date=${encodeURIComponent(this._date)}${refresh ? "&refresh=1" : ""}`);
      this._error = "";
    } catch (error) {
      this._error = errorText(error);
    }
    if (showLoading) this._message = "";
    if (!this._playbackSession?.playlist_url) this._render(); else this._updateStatusLine();
  }

  async _loadStatus(refresh = false) {
    if (!this._cameraId) return;
    try {
      this._status = await this._api("GET", `tvt_archive/${this._entryId}/cameras/${this._cameraId}/status${refresh ? "?refresh=1" : ""}`);
      this._error = "";
    } catch (error) {
      this._error = errorText(error);
    }
    if (this._playbackSession?.playlist_url) this._renderStatusOnly(); else this._render();
    clearTimeout(this._statusTimer);
    this._statusTimer = setTimeout(() => this._loadStatus(), 120000);
  }

  _effectiveRecordingQuality() {
    return ["original", "balanced", "data_saver"].includes(this._recordingQuality)
      ? this._recordingQuality
      : "original";
  }

  _liveProfiles() {
    return Array.isArray(this._camera?.live_profiles)
      ? this._camera.live_profiles.filter((profile) => profile?.entity_id)
      : [];
  }

  _ensureLiveProfileSelection() {
    const profiles = this._liveProfiles();
    if (profiles.some((profile) => profile.id === this._liveProfileId)) return;
    this._liveProfileId = profiles.find((profile) => profile.default)?.id || profiles[0]?.id || null;
  }

  _selectedLiveProfile() {
    this._ensureLiveProfileSelection();
    const profiles = this._liveProfiles();
    return profiles.find((profile) => profile.id === this._liveProfileId)
      || profiles.find((profile) => profile.default)
      || profiles[0]
      || null;
  }

  _effectiveLiveEntity() {
    const profile = this._selectedLiveProfile();
    if (!profile) return null;
    return profile.entity_id;
  }

  _timelineHtml() {
    if (!this._timeline) return `<div class="empty">Loading timeline…</div>`;
    const width = this._zoom * 100;
    let html = `<div class="timeline-inner" style="width:${width}%">`;
    for (let hour = 0; hour <= 24; hour += 2) {
      html += `<div class="tick" style="left:${(hour / 24) * 100}%"><span>${pad(hour)}:00</span></div>`;
    }
    for (const range of this._timeline.merged_ranges || []) {
      const startDate = new Date(range.start), stopDate = new Date(range.stop);
      const start = startDate.getHours() * 3600 + startDate.getMinutes() * 60 + startDate.getSeconds();
      let stop = stopDate.getHours() * 3600 + stopDate.getMinutes() * 60 + stopDate.getSeconds();
      if (range.stop.startsWith(`${this._date}T00:00:00`) && range.start !== range.stop) stop = 86400;
      html += `<div class="segment" style="left:${(start/86400)*100}%;width:${Math.max(.08,((stop-start)/86400)*100)}%"></div>`;
    }
    html += `<div class="marker" style="left:${(this._selectedSec/86400)*100}%"></div></div>`;
    return html;
  }

  _statusValues() {
    const status = this._status || {};
    return {
      recording: status.timeline_today?.recording_now ? "Running" : status.online === false ? "Offline" : "Not active",
      today: status.timeline_today?.recorded_hours == null ? "—" : historyText(status.timeline_today.recorded_hours),
      history: historyText(status.availability?.available_history_hours),
      archive: this._camera?.archive_backend === "rtsp" ? "Recorded RTSP" : "Native TCP/9008",
      liveProfile: this._selectedLiveProfile()?.name || "Not configured",
      accelerator: (this._playbackSession?.accelerator_used || status.accelerator?.selected || "—").replaceAll("_", " "),
      oldest: status.availability?.earliest ? new Date(status.availability.earliest).toLocaleString() : "—",
      latest: status.availability?.latest ? new Date(status.availability.latest).toLocaleString() : "—",
    };
  }

  _renderStatusOnly() {
    const values = this._statusValues();
    for (const [key, value] of Object.entries(values)) {
      const target = this.shadowRoot.getElementById(`stat-${key}`);
      if (target) target.textContent = value;
    }
    this._updateStatusLine();
  }

  _updateStatusLine() {
    const target = this.shadowRoot.getElementById("statusline");
    if (!target) return;
    target.classList.toggle("error", Boolean(this._error));
    target.textContent = this._error || this._message || "";
  }

  _downloadActionLabel() {
    const phase = String(this._downloadPhase || "").toLowerCase();
    if (phase.includes("receiv")) return "Receiving";
    if (phase.includes("browser file") || phase.includes("process") || phase.includes("validat")) return "Processing";
    return "Preparing";
  }

  _updateDownloadUi() {
    const percent = Math.max(0, Math.min(100, Math.round(Number(this._downloadPercent || 0))));
    const button = this.shadowRoot.getElementById("download");
    const progress = this.shadowRoot.getElementById("download-progress");
    const bar = this.shadowRoot.getElementById("download-progress-bar");
    const label = this.shadowRoot.getElementById("download-percent");
    if (button) {
      button.disabled = this._busy;
      button.textContent = this._busy ? this._downloadActionLabel() : "Download original";
    }
    if (progress) progress.classList.toggle("visible", this._busy || percent === 100);
    if (bar) {
      bar.style.width = `${percent}%`;
      bar.parentElement?.setAttribute("aria-valuenow", String(percent));
    }
    if (label) label.textContent = `${percent}%`;
    this._updateStatusLine();
  }

  _resetDownloadResult() {
    if (this._busy) return;
    this._downloadJobId = null;
    this._downloadUrl = null;
    this._downloadFilename = null;
    this._downloadPercent = 0;
    this._downloadPhase = "";
    this._persistState();
  }

  _render() {
    const selected = secToClock(this._selectedSec);
    const effectiveQuality = this._effectiveRecordingQuality();
    const values = this._statusValues();
    const cameras = this._cameras.map((camera) => `<option value="${$esc(camera.id)}" ${camera.id === this._cameraId ? "selected" : ""}>${$esc(camera.name || camera.id)}</option>`).join("");
    this._ensureLiveProfileSelection();
    const liveProfiles = this._liveProfiles();
    const qualities = this._mode === "recording"
      ? [
          ["original", "Original"],
          ["balanced", "Balanced (720p)"],
          ["data_saver", "Data Saver (480p)"],
        ].map(([value, label]) => `<option value="${value}" ${value === this._recordingQuality ? "selected" : ""}>${label}</option>`).join("")
      : (liveProfiles.length
          ? liveProfiles.map((profile) => `<option value="${$esc(profile.id)}" ${profile.id === this._liveProfileId ? "selected" : ""}>${$esc(profile.name)}</option>`).join("")
          : `<option value="">No live profiles</option>`);
    const qualityLabel = this._mode === "recording" ? "Recording quality" : "Live profile";

    this.shadowRoot.innerHTML = `<style>
      :host{display:block;min-height:100%;background:var(--primary-background-color);color:var(--primary-text-color);box-sizing:border-box}
      *{box-sizing:border-box;min-width:0}.page{max-width:1600px;margin:0 auto;padding:18px;display:grid;gap:14px}
      .top{display:flex;align-items:end;justify-content:space-between;gap:12px;flex-wrap:wrap}.heading{display:flex;align-items:center;gap:12px}
      .heading h1{font-size:1.65rem;margin:0}.subtle{color:var(--secondary-text-color);font-size:.9rem}.version-text{font-size:.8rem;line-height:1.1;margin-top:2px}
      .controls{display:flex;gap:8px;align-items:end;flex-wrap:wrap;max-width:100%}label{display:grid;gap:4px;color:var(--secondary-text-color);font-size:.78rem;min-width:0}
      input,select,button{font:inherit;color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:10px;padding:9px 11px;min-width:0;max-width:100%}
      input[type="time"],input[type="date"]{width:100%;min-width:0;max-width:100%;display:block;-webkit-appearance:none;appearance:none}
      button{cursor:pointer;background:var(--primary-color);color:var(--text-primary-color,#fff);border:0;font-weight:600}button.secondary{background:var(--secondary-background-color);color:var(--primary-text-color)}button:disabled{opacity:.55;cursor:default}
      .shell{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:14px}.card{background:var(--card-background-color);border-radius:14px;box-shadow:var(--ha-card-box-shadow,0 2px 2px rgba(0,0,0,.15));overflow:hidden;min-width:0}
      .player-head{padding:11px 14px;display:flex;justify-content:space-between;align-items:center;gap:10px;border-bottom:1px solid var(--divider-color)}
      .mode{display:flex;gap:7px}.mode button.active{background:var(--primary-color);color:#fff}.player{background:#000;min-height:min(62vh,640px);display:grid;place-items:center;position:relative;overflow:hidden}
      .player video{width:100%;height:min(62vh,640px);object-fit:contain;background:#000;display:block}.live-host{width:100%;min-height:min(62vh,640px)}.player-state{display:none;grid-area:1/1;z-index:2;align-items:center;justify-content:center;pointer-events:none;color:var(--secondary-text-color);font-size:.92rem}.player-state.visible{display:flex}.player-state.action{pointer-events:auto}.player-state-content{display:flex;align-items:center;gap:10px;padding:10px 13px;border-radius:10px;background:rgba(0,0,0,.58);color:var(--secondary-text-color)}.player-spinner{width:18px;height:18px;border-radius:50%;border:3px solid rgba(255,255,255,.25);border-top-color:var(--secondary-text-color);animation:tvt-spin .8s linear infinite}@keyframes tvt-spin{to{transform:rotate(360deg)}}
      .empty{padding:32px;text-align:center;color:var(--secondary-text-color)}.sidebar{padding:14px;display:grid;gap:10px;align-content:start}.stat{padding:10px 12px;background:var(--secondary-background-color);border-radius:10px}.stat span{color:var(--secondary-text-color);font-size:.8rem}.stat b{display:block;margin-top:3px;overflow-wrap:anywhere}
      .timeline-card{padding:12px}.timeline-title{display:flex;justify-content:space-between;gap:10px;margin-bottom:8px}.timeline{height:112px;overflow-x:auto;overflow-y:hidden;position:relative;background:var(--secondary-background-color);border-radius:10px;cursor:crosshair}.timeline-inner{height:100%;position:relative;min-width:100%}.segment{position:absolute;top:38px;height:42px;border-radius:6px;background:var(--success-color,#43a047)}.tick{position:absolute;top:0;bottom:0;width:1px;background:var(--divider-color)}.tick span{position:absolute;left:0;top:7px;transform:translateX(-50%);font-size:.7rem;color:var(--secondary-text-color);white-space:nowrap}.tick:first-child span{left:6px;transform:none}.tick:last-child span{left:auto;right:6px;transform:none}.marker{position:absolute;top:28px;bottom:12px;width:2px;background:var(--error-color,#e53935)}.marker:after{content:"";position:absolute;top:-5px;left:-4px;width:10px;height:10px;border-radius:50%;background:inherit}
      .lower{display:grid;grid-template-columns:1fr 1fr;gap:14px}.box{padding:13px;min-width:0;overflow:hidden}.selection-controls{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:8px;align-items:end}.selection-controls>*{min-width:0;max-width:100%}.range{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) auto;gap:8px;align-items:end}.range>*{min-width:0;max-width:100%}.download-link{display:inline-flex;align-items:center;justify-content:center;padding:10px 14px;border-radius:8px;background:var(--primary-color);color:var(--text-primary-color,#fff);text-decoration:none;font-weight:600}.download-progress{display:none;grid-column:1/-1;align-items:center;gap:9px;font-size:.78rem;color:var(--secondary-text-color)}.download-progress.visible{display:flex}.download-track{height:5px;flex:1;overflow:hidden;border-radius:999px;background:var(--divider-color)}.download-bar{height:100%;width:0;background:var(--primary-color);transition:width .25s ease}.download-percent{min-width:34px;text-align:right;font-variant-numeric:tabular-nums}.statusline{min-height:22px;color:var(--secondary-text-color)}.error{color:var(--error-color)}
      @media(min-width:901px) and (min-height:720px){
        :host{height:100dvh;overflow:hidden}.page{height:100%;overflow:hidden;padding:12px 18px;gap:10px;grid-template-rows:auto minmax(0,1fr) auto auto auto}
        .shell{min-height:0;gap:10px}.shell>.card:first-child{display:grid;grid-template-rows:auto minmax(0,1fr);min-height:0}.player{height:100%;min-height:0}.player video,.live-host{height:100%;min-height:0;max-height:none}
        .sidebar{min-height:0;overflow:hidden;padding:10px;gap:7px}.stat{padding:7px 10px}.timeline-card{padding:9px 10px}.timeline-title{margin-bottom:6px}.timeline{height:92px}.segment{top:31px;height:35px}.marker{top:23px;bottom:10px}
        .lower{gap:10px}.box{padding:10px}.statusline{min-height:18px;font-size:.8rem}
      }
      @media(max-width:900px){.shell{grid-template-columns:1fr}.sidebar{grid-template-columns:repeat(2,minmax(0,1fr))}.lower{grid-template-columns:1fr}.player,.player video,.live-host{min-height:42vh;height:42vh}.page{padding:10px}}
      @media(max-width:560px){
        .top{display:block}.heading{margin-bottom:16px}.controls{width:100%;display:grid;grid-template-columns:1fr;gap:10px}.controls label,.controls input,.controls select,.controls button{width:100%}
        .player-head{display:grid;grid-template-columns:1fr auto;align-items:center}.player-head>b{grid-column:1/-1}.player-head>.subtle{grid-column:1/-1}.sidebar{grid-template-columns:1fr 1fr}.timeline-title{align-items:flex-start}.timeline-title span{max-width:68%}
        .selection-controls{grid-template-columns:1fr 1fr}.selection-controls label{grid-column:1/-1;width:100%;overflow:hidden}.selection-controls input{width:100%;max-width:100%}.selection-controls button{width:100%}
        .range{grid-template-columns:1fr 1fr}.range button,.range .download-link{grid-column:1/-1;width:100%}.box{padding:11px}.page{overflow-x:hidden}
      }
      @media(max-width:370px){.sidebar{grid-template-columns:1fr}.selection-controls{grid-template-columns:1fr}.selection-controls label{grid-column:auto}.range{grid-template-columns:1fr}.range button,.range .download-link{grid-column:auto}.download-progress{grid-column:1}}
    </style><div class="page">
      <div class="top"><div class="heading"><div><h1>Recordings</h1><div class="subtle version-text">TVT Archive · v${VERSION}</div></div></div>
        <div class="controls"><label>Camera<select id="camera">${cameras}</select></label><label>Date<input id="date" type="date" value="${$esc(this._date)}"></label><label>${qualityLabel}<select id="quality">${qualities}</select></label><label>Timeline<select id="zoom"><option value="1">24 hours</option><option value="2">12-hour</option><option value="4">6-hour</option></select></label><button id="refresh" class="secondary">Refresh</button></div>
      </div>
      <div class="shell"><div class="card"><div class="player-head"><div class="mode"><button id="live" class="secondary ${this._mode === "live" ? "active" : ""}">● Live</button><button id="recording" class="secondary ${this._mode === "recording" ? "active" : ""}">Recording</button></div><b>${$esc(this._camera?.name || "Camera")}</b><span class="subtle">${this._mode === "recording" ? `${this._date} ${selected}` : `Live now${this._selectedLiveProfile()?.name ? ` · ${this._selectedLiveProfile().name}` : ""}`}</span></div><div id="player" class="player"></div></div>
        <div class="card sidebar">
          <div class="stat"><span>Recording</span><b id="stat-recording">${$esc(values.recording)}</b></div><div class="stat"><span>Recorded today</span><b id="stat-today">${$esc(values.today)}</b></div>
          <div class="stat"><span>Available history</span><b id="stat-history">${$esc(values.history)}</b></div><div class="stat"><span>Archive media</span><b id="stat-archive">${$esc(values.archive)}</b></div>
          <div class="stat"><span>Default live profile</span><b id="stat-liveProfile">${$esc(values.liveProfile)}</b></div><div class="stat"><span>Playback accelerator</span><b id="stat-accelerator">${$esc(values.accelerator)}</b></div>
          <div class="stat"><span>Oldest recording</span><b id="stat-oldest">${$esc(values.oldest)}</b></div><div class="stat"><span>Latest recording</span><b id="stat-latest">${$esc(values.latest)}</b></div>
        </div>
      </div>
      <div class="card timeline-card"><div class="timeline-title"><span>Click a recorded section to select a time</span><b>${selected}</b></div><div id="timeline" class="timeline">${this._timelineHtml()}</div></div>
      <div class="lower"><div class="card box"><div class="selection-controls"><label>Selected time<input id="selected" type="time" step="1" value="${selected}"></label><button id="play">Play from here</button><button id="go-live" class="secondary">Go live</button></div></div>
        <div class="card box"><div class="range"><label>Download start<input id="range-start" type="time" step="1" value="${$esc(this._rangeStart)}"></label><label>Download end<input id="range-end" type="time" step="1" value="${$esc(this._rangeEnd)}"></label>${this._downloadUrl ? `<a id="save-download" class="download-link" href="${$esc(this._downloadUrl)}" download="${$esc(this._downloadFilename || "recording.mp4")}">Save file</a>` : `<button id="download" ${this._busy ? "disabled" : ""}>${this._busy ? this._downloadActionLabel() : "Download original"}</button>`}<div id="download-progress" class="download-progress ${this._busy || this._downloadPercent === 100 ? "visible" : ""}"><div class="download-track" role="progressbar" aria-label="Export progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.max(0, Math.min(100, Math.round(this._downloadPercent || 0)))}"><div id="download-progress-bar" class="download-bar" style="width:${Math.max(0, Math.min(100, Math.round(this._downloadPercent || 0)))}%"></div></div><span id="download-percent" class="download-percent">${Math.max(0, Math.min(100, Math.round(this._downloadPercent || 0)))}%</span></div></div></div></div>
      <div id="statusline" class="statusline ${this._error ? "error" : ""}">${$esc(this._error || this._message || "")}</div>
    </div>`;
    this._bind();
    this._renderPlayer();
  }

  _bind() {
    const get = (id) => this.shadowRoot.getElementById(id);
    const zoom = get("zoom"); if (zoom) zoom.value = String(this._zoom);
    get("camera")?.addEventListener("change", async (event) => {
      await this._stopPlaybackSession();
      this._resetDownloadResult();
      this._cameraId = event.target.value;
      this._camera = this._cameras.find((camera) => camera.id === this._cameraId);
      this._liveProfileId = null; this._ensureLiveProfileSelection();
      this._timeline = null; this._status = null; this._mode = "live"; this._persistState();
      await Promise.all([this._loadTimeline(), this._loadStatus()]);
    });
    get("date")?.addEventListener("change", async (event) => {
      const nextDate = event.currentTarget.value;
      if (!/^\d{4}-\d{2}-\d{2}$/.test(nextDate)) return;
      this._resetDownloadResult();
      this._date = nextDate; this._timeline = null; this._persistState();
      await this._stopPlaybackSession();
      await this._loadTimeline();
    });
    get("quality")?.addEventListener("change", async (event) => {
      if (this._mode === "recording" && this._playbackSession?.playlist_url) {
        this._selectedSec = Math.min(86399, this._selectedSec + Math.floor(this._currentPlaybackTime()));
      }
      if (this._mode === "recording") this._recordingQuality = event.target.value;
      else this._liveProfileId = event.target.value || null;
      this._persistState();
      this._mode === "recording" ? this._playRecording() : this._render();
    });
    get("zoom")?.addEventListener("change", (event) => { this._zoom = Number(event.target.value); this._persistState(); if (!this._playbackSession?.playlist_url) this._render(); });
    get("refresh")?.addEventListener("click", async () => {
      this._message = "Refreshing…"; this._updateStatusLine();
      await this._loadCameras();
      await Promise.all([this._loadTimeline(true), this._loadStatus(true)]);
      this._persistState();
    });
    get("live")?.addEventListener("click", () => this._goLive());
    get("go-live")?.addEventListener("click", () => this._goLive());
    get("recording")?.addEventListener("click", () => { this._mode = "recording"; this._persistState(); this._render(); });
    get("play")?.addEventListener("click", () => this._playRecording());
    get("selected")?.addEventListener("change", async (event) => { const value=event.currentTarget.value; this._resetDownloadResult(); this._selectedSec = clockToSec(value); this._rangeStart = value.length===5 ? `${value}:00` : value; this._rangeEnd = secToClock(Math.min(86399, this._selectedSec + 300)); this._persistState(); await this._stopPlaybackSession(); this._render(); });
    get("range-start")?.addEventListener("change", (event) => { this._rangeStart = event.currentTarget.value; this._resetDownloadResult(); this._render(); });
    get("range-end")?.addEventListener("change", (event) => { this._rangeEnd = event.currentTarget.value; this._resetDownloadResult(); this._render(); });
    get("download")?.addEventListener("click", () => this._download());
    get("save-download")?.addEventListener("click", () => {
      this._message = "Download started";
      this._updateStatusLine();
    });
    const timeline = get("timeline");
    timeline?.addEventListener("click", async (event) => {
      const inner = timeline.querySelector(".timeline-inner"); if (!inner) return;
      await this._stopPlaybackSession();
      this._resetDownloadResult();
      const rect = inner.getBoundingClientRect();
      this._selectedSec = Math.max(0, Math.min(86399, ((event.clientX - rect.left) / rect.width) * 86400));
      this._rangeStart = secToClock(this._selectedSec); this._rangeEnd = secToClock(Math.min(86399, this._selectedSec + 300)); this._mode = "recording"; this._persistState(); this._render();
    });
  }

  _refreshLivePlayer() {
    if (this._mode !== "live" || !this.shadowRoot.getElementById("player")) return;
    const card = this.shadowRoot.querySelector("hui-picture-entity-card");
    if (card) card.hass = this._hass;
  }

  _renderPlayer() {
    const host = this.shadowRoot.getElementById("player"); if (!host) return;
    if (this._mode === "recording") {
      if (!this._playbackSession?.playlist_url) {
        if (this._recordingController && this._recordingPlayer?.isConnected) return;
        host.innerHTML = `<div class="empty">Select a recorded time and press <b>Play from here</b>.</div>`;
        return;
      }
      this._attachHlsPlayer(host, this._playbackSession.playlist_url);
      return;
    }
    const entity = this._effectiveLiveEntity();
    if (!entity || !this._hass?.states?.[entity]) {
      host.innerHTML = `<div class="empty">No live camera entity is associated with this archive camera.<br>Open Settings → Devices & services → TVT Archive → Configure → Manage live profiles.</div>`;
      return;
    }
    const card = document.createElement("hui-picture-entity-card");
    card.className = "live-host";
    try {
      card.setConfig({type:"picture-entity", entity, camera_view:"live", show_name:false, show_state:false});
      card.hass = this._hass; host.replaceChildren(card);
    } catch (error) {
      host.innerHTML = `<div class="empty">Could not render live entity ${$esc(entity)}: ${$esc(errorText(error))}</div>`;
    }
  }

  _setRecordingPlayerState(state, detail = {}) {
    const overlay = this.shadowRoot.getElementById("player-state");
    if (!overlay) return;
    const content = overlay.querySelector(".player-state-content");
    if (state === "playing" || state === "seeking") {
      overlay.className = "player-state";
      overlay.replaceChildren();
      return;
    }
    overlay.className = `player-state visible${state === "play-required" ? " action" : ""}`;
    if (state === "play-required") {
      overlay.innerHTML = `<div class="player-state-content"><button id="resume-recording">Play recording</button></div>`;
      overlay.querySelector("#resume-recording")?.addEventListener("click", () => this._recordingController?.resume());
      return;
    }
    const label = state === "rebuffering" ? "Catching up" : state === "opening" ? "Opening recording" : "Buffering recording";
    const ahead = Number(detail.ahead || 0), target = Number(detail.target || 0);
    const suffix = target > 0 ? ` · ${Math.min(target, ahead).toFixed(1)}/${target.toFixed(0)}s` : "";
    overlay.innerHTML = `<div class="player-state-content"><span class="player-spinner"></span><span>${label}${suffix}</span></div>`;
    if (content) content.textContent = label;
  }

  _createRecordingPlayer(host, url = null) {
    this._recordingController?.destroy();
    const video = document.createElement("video");
    video.controls = false; video.autoplay = false; video.playsInline = true;
    video.preload = "auto";
    const overlay = document.createElement("div");
    overlay.id = "player-state";
    overlay.className = "player-state visible";
    overlay.innerHTML = `<div class="player-state-content"><span class="player-spinner"></span><span>Opening recording</span></div>`;
    host.replaceChildren(video, overlay);
    this._recordingPlayer = video;
    const controller = new TVTFmp4Player(
      video, url, this._playbackSession?.player_script_url, this._playbackSession?.mime_type, this._playbackSession?.start_buffer_seconds,
      () => { this._message = ""; this._playbackRetryCount = 0; this._playbackRetryPending = false; this._updateStatusLine(); },
      (message) => this._handlePlaybackFailure(message || "The recording could not be opened."),
      (state, detail) => this._setRecordingPlayerState(state, detail),
    );
    this._recordingController = controller;
    return controller;
  }

  _primeRecordingPlayer() {
    const host = this.shadowRoot.getElementById("player");
    if (!host) return;
    const controller = this._createRecordingPlayer(host);
    controller.prime();
  }

  async _attachHlsPlayer(host, url) {
    if (this._recordingController && this._recordingPlayer?.isConnected) {
      if (this._recordingController.url === url) return;
      if (!this._recordingController.url) {
        try { await this._recordingController.load(url, this._playbackSession?.player_script_url, this._playbackSession?.mime_type, this._playbackSession?.start_buffer_seconds); }
        catch (error) { if (this._recordingController) this._handlePlaybackFailure(errorText(error)); }
        return;
      }
    }
    const controller = this._createRecordingPlayer(host, url);
    try { await controller.start(); }
    catch (error) { if (this._recordingController === controller) this._handlePlaybackFailure(errorText(error)); }
  }

  _currentPlaybackTime() {
    return Number(this._recordingPlayer?.currentTime || 0);
  }

  async _stopPlaybackSession(render = false) {
    clearTimeout(this._sessionPollTimer);
    const current = this._playbackSession;
    this._playbackSession = null;
    this._recordingController?.destroy();
    this._recordingController = null;
    this._recordingPlayer = null;
    if (current?.id && this._entryId) {
      this._api("DELETE", `tvt_archive/${this._entryId}/sessions/${current.id}`).catch(() => {});
    }
    if (render) this._render();
  }

  async _goLive() { clearTimeout(this._playbackRetryTimer); await this._stopPlaybackSession(); this._mode = "live"; this._persistState(); this._render(); }

  async _playRecording(automaticRetry = false) {
    clearTimeout(this._playbackRetryTimer);
    if (!automaticRetry) { this._playbackRetryCount = 0; this._playbackRetryPending = false; }
    await this._stopPlaybackSession();
    this._mode = "recording";
    this._persistState();
    this._error = "";
    const quality = this._effectiveRecordingQuality();
    const duration = Math.max(5, Math.min(900, 86400 - Math.floor(this._selectedSec)));
    this._message = `Opening ${quality.replaceAll("_", " ")} recording`;
    this._render();
    // Create the visible shell immediately, then attach one stable signed playlist
    // to the local hls.js player when the backend announces it. The same player
    // remains attached while the event playlist grows.
    this._primeRecordingPlayer();
    try {
      const session = await this._api("POST", `tvt_archive/${this._entryId}/cameras/${this._cameraId}/sessions`, {
        start: isoFor(this._date, secToClock(this._selectedSec)), duration, quality, gain_db: 0,
      });
      this._playbackSession = session;
      if (session.player_script_url && !this._recordingPlayer?.canPlayType("application/vnd.apple.mpegurl")) {
        loadHlsLibrary(session.player_script_url).catch(() => {});
      }
      await this._pollPlaybackSession(session.id);
    } catch (error) {
      this._handlePlaybackFailure(errorText(error));
    }
  }

  async _pollPlaybackSession(sessionId) {
    try {
      const previous = this._playbackSession;
      const alreadyAttached = Boolean(this._recordingController?.url || previous?.playlist_url);
      const fresh = await this._api("GET", `tvt_archive/${this._entryId}/sessions/${sessionId}`);
      if (this._playbackSession?.id !== sessionId) return;
      // Playlist readiness is supposed to be sticky, but retain the first signed
      // URL locally as well so a transient metadata/proxy read can never tear
      // down a player that is already receiving segments.
      // Keep the first signed media URLs for the lifetime of this player. The HA
      // proxy signs each status response, so preferring the fresh URL recreated
      // Firefox's media element every second and caused the 1.4s/opening loop.
      const playlistUrl = previous?.playlist_url || fresh.playlist_url || null;
      const playerScriptUrl = previous?.player_script_url || fresh.player_script_url || null;
      const session = {
        ...fresh,
        ...(playlistUrl ? {playlist_url:playlistUrl, playlist_ready:true} : {}),
        ...(playerScriptUrl ? {player_script_url:playerScriptUrl} : {}),
      };
      this._playbackSession = session;
      this._message = `${session.phase || session.status}${session.elapsed_seconds ? ` · ${session.elapsed_seconds}s` : ""}`;
      if (session.status === "error") throw new Error(session.error || "Playback session failed");
      if (session.playlist_ready && session.playlist_url) {
        this._message = "";
        const preparedPlayer = Boolean(this._recordingController && this._recordingPlayer?.isConnected);
        if (!alreadyAttached && !preparedPlayer) this._render();
        else this._renderStatusOnly();
        const host = this.shadowRoot.getElementById("player");
        if (host) await this._attachHlsPlayer(host, session.playlist_url);
        this._recordingController?.setComplete(Boolean(session.complete));
        if (!session.complete && session.status !== "stopped") {
          clearTimeout(this._sessionPollTimer);
          this._sessionPollTimer = setTimeout(() => this._pollPlaybackSession(sessionId), 1000);
        }
        return;
      }
      this._updateStatusLine();
      clearTimeout(this._sessionPollTimer);
      this._sessionPollTimer = setTimeout(() => this._pollPlaybackSession(sessionId), 500);
    } catch (error) {
      this._handlePlaybackFailure(errorText(error));
    }
  }

  _recoverablePlaybackError(message) {
    return /(no route to host|network is unreachable|temporarily unreachable|connection refused|timed out|timeout|networkerror|fragloaderror|levelloaderror|manifestloaderror|camera archive capture failed)/i.test(String(message || ""));
  }

  async _handlePlaybackFailure(message) {
    const text = String(message || "The recording could not be opened.");
    if (this._playbackRetryPending) return;
    if (this._mode === "recording" && this._recoverablePlaybackError(text) && this._playbackRetryCount < 3) {
      const played = Math.max(0, Math.floor(this._currentPlaybackTime() - 1));
      if (played > 0) {
        this._selectedSec = Math.min(86399, this._selectedSec + played);
        this._rangeStart = secToClock(this._selectedSec);
      }
      this._playbackRetryCount += 1;
      this._playbackRetryPending = true;
      this._message = `Camera connection interrupted · retrying ${this._playbackRetryCount}/3`;
      this._error = "";
      await this._stopPlaybackSession(false);
      this._render();
      clearTimeout(this._playbackRetryTimer);
      this._playbackRetryTimer = setTimeout(() => { this._playbackRetryPending = false; this._playRecording(true); }, 1200);
      return;
    }
    this._message = "";
    this._error = text;
    this._playbackSession = null;
    this._recordingController?.destroy();
    this._recordingController = null;
    this._recordingPlayer = null;
    this._render();
  }

  async _download() {
    try {
      const start = this._rangeStart, end = this._rangeEnd;
      const duration = clockToSec(end) - clockToSec(start);
      if (duration <= 0) throw new Error("Download end must be after its start");
      if (duration > 3600) throw new Error("Downloads are limited to one hour per request");
      this._busy = true;
      this._downloadJobId = null;
      this._downloadUrl = null;
      this._downloadFilename = null;
      this._downloadPercent = 0;
      this._downloadPhase = "Preparing";
      this._error = "";
      this._message = this._downloadPhase;
      this._persistState();
      if (this._playbackSession?.playlist_url) this._updateDownloadUi(); else this._render();
      const job = await this._api("POST", `tvt_archive/${this._entryId}/cameras/${this._cameraId}/jobs`, {start: isoFor(this._date, start), duration, gain_db: 0, quality: "original", kind: "download"});
      this._downloadJobId = job.id;
      this._persistState();
      await this._pollDownload(job.id);
    } catch (error) {
      this._busy = false;
      this._downloadJobId = null;
      this._downloadPercent = 0;
      this._downloadPhase = "";
      this._downloadUrl = null;
      this._downloadFilename = null;
      this._message = "";
      this._error = errorText(error);
      this._persistState();
      if (this._playbackSession?.playlist_url) this._updateDownloadUi(); else this._render();
    }
  }

  async _pollDownload(jobId) {
    try {
      const job = await this._api("GET", `tvt_archive/${this._entryId}/jobs/${jobId}`);
      if (this._downloadJobId !== jobId) return;
      this._downloadPercent = Math.max(0, Math.min(100, Math.round(Number(job.progress_percent ?? Number(job.progress || 0) * 100))));
      this._downloadPhase = job.phase || job.status || "Preparing";
      this._message = this._downloadPhase;
      if (job.status === "error") throw new Error(job.error || "Download preparation failed");
      if (job.ready) {
        this._busy = false;
        this._downloadPercent = 100;
        this._downloadUrl = job.download_url;
        this._downloadFilename = job.filename || "recording.mp4";
        this._message = "Download ready";
        this._persistState();
        if (this._playbackSession?.playlist_url) this._updateDownloadUi(); else this._render();
        clearTimeout(this._statusTimer);
        this._statusTimer = setTimeout(() => this._loadStatus(), 1000);
        return;
      }
      this._busy = true;
      this._persistState();
      this._updateDownloadUi();
      clearTimeout(this._pollTimer);
      this._pollTimer = setTimeout(() => this._pollDownload(jobId), 1000);
    } catch (error) {
      this._busy = false;
      this._downloadJobId = null;
      this._downloadPercent = 0;
      this._downloadPhase = "";
      this._downloadUrl = null;
      this._downloadFilename = null;
      this._message = "";
      this._error = errorText(error);
      this._persistState();
      if (this._playbackSession?.playlist_url) this._updateDownloadUi(); else this._render();
      clearTimeout(this._statusTimer);
      this._statusTimer = setTimeout(() => this._loadStatus(), 1000);
    }
  }

}

if (!customElements.get("tvt-archive-panel")) customElements.define("tvt-archive-panel", TVTArchivePanel);
