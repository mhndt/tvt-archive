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

// Fallback for browsers without WebCodecs: the bridge turns the capture into an HLS event
// playlist and hls.js plays it. Playback runs at 1x; when the camera is slower than real
// time the video simply waits on its last frame until the next segment lands, the same
// way the frame player holds a frame, with no rate changes and no overlay once started.
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
    this.eventsBound = false;
    this.mediaRecoveryAttempts = 0;
    this.networkRecoveryTimer = null;
    this.controlsHideTimer = null;
    this.startBufferSeconds = Math.max(2, Math.min(12, Number(startBufferSeconds || 3)));
    this._onSeeking = () => this._state("seeking");
    this._onPlaying = () => { if (this.started) this._state("playing"); };
    this._onError = () => this._handleMediaError();
    this._onActivity = () => this._showControls();
    this._onBuffered = () => this._maybeStart();
  }

  _state(name) { this.onState?.(name); }

  _bindVideoEvents() {
    if (this.eventsBound) return;
    this.eventsBound = true;
    this.video.addEventListener("seeking", this._onSeeking);
    this.video.addEventListener("playing", this._onPlaying);
    this.video.addEventListener("error", this._onError);
    for (const type of ["pointerenter", "pointermove", "pointerdown"]) this.video.addEventListener(type, this._onActivity);
    for (const type of ["progress", "durationchange", "loadedmetadata"]) this.video.addEventListener(type, this._onBuffered);
  }

  _showControls() {
    if (this.destroyed || !this.started) return;
    this.video.controls = true;
    clearTimeout(this.controlsHideTimer);
    this.controlsHideTimer = setTimeout(() => { if (!this.video.paused && !this.video.seeking) this.video.controls = false; }, 2500);
  }

  prime() {
    // Runs inside the Play tap: load() here lifts the gesture requirement for later play() calls.
    if (this.destroyed) return;
    this._bindVideoEvents();
    try { this.video.load(); } catch (_) {}
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
    if (startBufferSeconds) this.startBufferSeconds = Math.max(2, Math.min(12, Number(startBufferSeconds)));
    this._bindVideoEvents();
    this._state("buffering");
    const Hls = await loadHlsLibrary(this.playerScriptUrl).catch(() => null);
    if (this.destroyed) return;
    if (!Hls?.isSupported()) {
      if (!this.video.canPlayType("application/vnd.apple.mpegurl")) throw new Error("This browser does not support recorded HLS playback.");
      this.video.src = this.url;
      this.video.load();
      return;
    }
    this.hls = new Hls({
      startPosition: 0,
      enableWorker: true,
      maxBufferLength: 30,
      maxMaxBufferLength: 120,
      backBufferLength: 180,
      manifestLoadingMaxRetry: 20,
      levelLoadingMaxRetry: 20,
      fragLoadingMaxRetry: 20,
      manifestLoadingRetryDelay: 250,
      levelLoadingRetryDelay: 250,
      fragLoadingRetryDelay: 250,
    });
    this.hls.on(Hls.Events.MEDIA_ATTACHED, () => { if (!this.destroyed) this.hls?.loadSource(this.url); });
    for (const event of [Hls.Events.MANIFEST_PARSED, Hls.Events.BUFFER_APPENDED, Hls.Events.FRAG_BUFFERED]) this.hls.on(event, this._onBuffered);
    this.hls.on(Hls.Events.ERROR, (_event, data) => this._handleHlsError(Hls, data));
    this.hls.attachMedia(this.video);
  }

  _bufferedAhead() {
    const ranges = this.video.buffered, now = Number(this.video.currentTime || 0);
    for (let i = 0; i < ranges.length; i += 1) {
      if (now <= ranges.end(i) + 0.35) return Math.max(0, ranges.end(i) - Math.max(now, ranges.start(i)));
    }
    return 0;
  }

  // Start once the first seconds are buffered; after that the browser paces itself.
  _maybeStart() {
    if (this.started || this.destroyed || !this.video.buffered.length) return;
    const ahead = this._bufferedAhead();
    if (ahead <= 0.05 || (ahead + 0.5 < this.startBufferSeconds && !this.serverComplete)) return;
    this.started = true;
    try { this.video.currentTime = this.video.buffered.start(0) + 0.03; } catch (_) {}
    this.onReady?.();
    const result = this.video.play();
    if (result?.then) result.then(() => this._state("playing")).catch((error) => { if (error?.name === "NotAllowedError") this.video.controls = true; });
    else this._state("playing");
  }

  _handleHlsError(Hls, data) {
    if (this.destroyed || !data?.fatal) return;
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
      clearTimeout(this.networkRecoveryTimer);
      this.networkRecoveryTimer = setTimeout(() => { if (!this.destroyed) this.hls?.startLoad(Math.max(0, Number(this.video.currentTime || 0))); }, 500);
      return;
    }
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR && this.mediaRecoveryAttempts < 3) {
      this.mediaRecoveryAttempts += 1;
      this.hls?.recoverMediaError();
      return;
    }
    this._fatal(`Recording playback failed${data.details ? `: ${data.details}` : ""}`);
  }

  _handleMediaError() {
    if (this.destroyed || this.hls || !this.video.error) return;
    const detail = {2: "network error", 3: "decode error", 4: "source not supported"}[this.video.error.code] || `code ${this.video.error.code}`;
    this._fatal(`Recording playback failed: ${detail}`);
  }

  setComplete(complete) {
    if (!complete || this.serverComplete) return;
    this.serverComplete = true;
    this._maybeStart();
  }

  _fatal(message) {
    if (this.destroyed) return;
    this.onError?.(message || "The recording player failed.");
    this.destroy();
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    clearTimeout(this.networkRecoveryTimer);
    clearTimeout(this.controlsHideTimer);
    if (this.eventsBound) {
      this.video.removeEventListener("seeking", this._onSeeking);
      this.video.removeEventListener("playing", this._onPlaying);
      this.video.removeEventListener("error", this._onError);
      for (const type of ["pointerenter", "pointermove", "pointerdown"]) this.video.removeEventListener(type, this._onActivity);
      for (const type of ["progress", "durationchange", "loadedmetadata"]) this.video.removeEventListener(type, this._onBuffered);
    }
    try { this.hls?.destroy(); } catch (_) {}
    this.hls = null;
    try { this.video.pause(); this.video.removeAttribute("src"); this.video.load(); } catch (_) {}
  }
}

const FRAME_PLAYER_SUPPORTED = typeof VideoDecoder === "function" && typeof EncodedVideoChunk === "function";
const REC_INFO = 0, REC_VIDEO = 1, REC_AUDIO = 2, REC_MARK = 3, REC_END = 4;
const DECODED_MAX = 8, PENDING_MAX = 600, PENDING_RESUME = 400, GAP_COLLAPSE_US = 3e6;
const ICONS = {
  play: "M8,5.14V19.14L19,12.14L8,5.14Z",
  pause: "M14,19H18V5H14M6,19H10V5H6V19Z",
  sound: "M14,3.23V5.29C16.89,6.15 19,8.83 19,12C19,15.17 16.89,17.84 14,18.7V20.77C18,19.86 21,16.28 21,12C21,7.72 18,4.14 14,3.23M16.5,12C16.5,10.23 15.5,8.71 14,7.97V16C15.5,15.29 16.5,13.76 16.5,12M3,9V15H7L12,20V4L7,9H3Z",
  muted: "M12,4L9.91,6.09L12,8.18M4.27,3L3,4.27L7.73,9H3V15H7L12,20V13.27L16.25,17.53C15.58,18.04 14.83,18.46 14,18.7V20.77C15.38,20.45 16.63,19.82 17.68,18.96L19.73,21L21,19.73L12,10.73M19,12C19,12.94 18.8,13.82 18.46,14.64L19.97,16.15C20.62,14.91 21,13.5 21,12C21,7.72 18,4.14 14,3.23V5.29C16.89,6.15 19,8.83 19,12M16.5,12C16.5,10.23 15.5,8.71 14,7.97V10.18L16.45,12.63C16.5,12.43 16.5,12.21 16.5,12Z",
  full: "M5,5H10V7H7V10H5V5M14,5H19V10H17V7H14V5M17,14H19V19H14V17H17V14M10,17V19H5V14H7V17H10Z",
  unfull: "M14,14H19V16H16V19H14V14M5,14H10V19H8V16H5V14M8,5H10V10H5V8H8V5M19,8V10H14V5H16V8H19Z",
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${ICONS[name]}"/></svg>`;
const ALAW = new Float32Array(256).map((_, i) => {
  const a = i ^ 0x55;
  let t = (a & 0x0f) << 4;
  const seg = (a & 0x70) >> 4;
  if (seg === 0) t += 8; else if (seg === 1) t += 0x108; else t = (t + 0x108) << (seg - 1);
  return ((a & 0x80) ? t : -t) / 32768;
});

const nalUnits = (data) => {
  const units = [];
  let start = -1;
  for (let i = 0; i + 2 < data.length; i++) {
    if (data[i] === 0 && data[i + 1] === 0 && data[i + 2] === 1) {
      const end = i > 0 && data[i - 1] === 0 ? i - 1 : i;
      if (start >= 0) units.push(data.subarray(start, end));
      start = i + 3;
      i += 2;
    }
  }
  if (start >= 0 && start < data.length) units.push(data.subarray(start));
  return units;
};

// Frames come over one long HTTP response as "TF" records. Video is decoded with WebCodecs
// and painted on a canvas by the rule every TVT player uses: a frame is shown when the
// wall clock has advanced as far as its timestamp, or at once if it arrived late. Nothing
// changes speed and nothing is dropped; when the camera falls behind the picture simply
// advances at the camera's pace and holds the last frame while waiting.
class TVTFramePlayer {
  constructor({onState, onError, onEnded, onProgress}) {
    this.onState = onState; this.onError = onError; this.onEnded = onEnded; this.onProgress = onProgress;
    this.element = document.createElement("div");
    this.element.className = "frame-player";
    this.element.innerHTML = `<canvas></canvas><div class="fp-wait"><span class="player-spinner"></span><span class="fp-wait-text">Opening recording</span></div>
      <div class="fp-bar"><button data-act="toggle" aria-label="Pause">${icon("pause")}</button><span class="fp-speed"></span><span class="fp-space"></span><button data-act="mute" aria-label="Mute">${icon("sound")}</button><input class="fp-volume" type="range" min="0" max="1" step="0.05" value="1" aria-label="Volume"><button data-act="full" aria-label="Full screen">${icon("full")}</button></div>`;
    this.canvas = this.element.querySelector("canvas");
    this.ctx = this.canvas.getContext("2d");
    this.frames = []; this.pending = []; this.audioQueue = []; this.arrivals = []; this.stepOnce = false;
    this.decoder = null; this.sps = null; this.pps = null; this.configured = false; this.needKey = true;
    this.paused = false; this.destroyed = false; this.ended = false; this.started = false;
    this.lastPts = null; this.lastWall = 0; this.rendered = 0; this.generation = 0; this.speed = null;
    this.audio = null; this.gain = null; this.muted = false; this.hasAudio = false; this.offsets = [];
    this.abort = null; this.roomResolve = null; this.barTimer = null; this.timer = 0;
    // Controls only while a mouse is over the player; a tap toggles them on touch screens.
    this.element.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-act]");
      if (button) { this._action(button.dataset.act); return; }
      if (!this.hoverable) this._showBar(this.element.classList.contains("show-bar") ? 0 : 2500);
    });
    this.hoverable = window.matchMedia("(hover: hover)").matches;
    this.element.addEventListener("pointerenter", (event) => { if (event.pointerType !== "touch") this._showBar(2500); });
    this.element.addEventListener("pointermove", (event) => { if (event.pointerType !== "touch") this._showBar(2500); });
    this.element.addEventListener("pointerleave", () => this._showBar(0));
    this.volume = 1;
    this.element.querySelector(".fp-volume").addEventListener("input", (event) => {
      this.volume = Number(event.target.value);
      this.muted = this.volume === 0;
      if (this.gain) this.gain.gain.value = this.volume;
      this._syncButtons();
    });
    this.element.addEventListener("dblclick", (event) => { if (!event.target.closest("button")) this._action("full"); });
    this._onFullscreen = () => this._syncButtons();
    document.addEventListener("fullscreenchange", this._onFullscreen);
  }

  prime() {
    // Called inside the Play tap so the audio context is allowed to start.
    if (this.audio || typeof AudioContext !== "function") return;
    try {
      this.audio = new AudioContext();
      this.gain = this.audio.createGain();
      this.gain.connect(this.audio.destination);
      this.audio.resume().catch(() => {});
    } catch (_) { this.audio = null; }
  }

  async start(url) {
    this.prime();
    this.started = true;
    this._state("opening");
    this.abort = new AbortController();
    this.timer = setTimeout(() => this._tick(), 0);
    try {
      const response = await fetch(url, {signal: this.abort.signal, cache: "no-store", credentials: "same-origin"});
      if (!response.ok || !response.body) throw new Error(`Stream request failed (${response.status})`);
      await this._read(response.body.getReader());
    } catch (error) {
      if (this.destroyed || this.abort?.signal.aborted) return;
      this.onError?.(error?.message || "The recording stream ended unexpectedly");
    }
  }

  async _read(reader) {
    let buffer = new Uint8Array(0);
    while (!this.destroyed) {
      const {value, done} = await reader.read();
      if (done) { if (!this.ended) this._end({status: "closed"}); return; }
      const joined = new Uint8Array(buffer.length + value.length);
      joined.set(buffer); joined.set(value, buffer.length);
      buffer = joined;
      let offset = 0;
      while (buffer.length - offset >= 16) {
        const view = new DataView(buffer.buffer, buffer.byteOffset + offset, 16);
        if (view.getUint8(0) !== 0x54 || view.getUint8(1) !== 0x46) throw new Error("Corrupt frame stream");
        const kind = view.getUint8(2), flags = view.getUint8(3);
        const pts = Number(view.getBigUint64(4, true)), length = view.getUint32(12, true);
        if (buffer.length - offset - 16 < length) break;
        this._record(kind, flags, pts, buffer.subarray(offset + 16, offset + 16 + length));
        offset += 16 + length;
      }
      buffer = buffer.subarray(offset);
      if (this.pending.length > PENDING_MAX) await new Promise((resolve) => { this.roomResolve = resolve; });
    }
  }

  _decodedCount() { return this.frames.length + (this.decoder?.decodeQueueSize || 0); }

  _record(kind, flags, pts, payload) {
    if (kind === REC_INFO) {
      try {
        const offset = JSON.parse(new TextDecoder().decode(payload)).utc_offset;
        if (Number.isFinite(offset)) { this.offsets.push({pts, offset}); this.offsets.sort((a, b) => a.pts - b.pts); }
      } catch (_) {}
      return;
    }
    if (kind === REC_VIDEO) { this._video(pts, Boolean(flags & 1), payload); return; }
    if (kind === REC_AUDIO) { this.hasAudio = true; this.audioQueue.push({pts, data: payload.slice()}); this._syncButtons(); return; }
    if (kind === REC_MARK) { this._flush(); try { this.generation = JSON.parse(new TextDecoder().decode(payload)).generation || 0; } catch (_) {} this._state("seeking"); return; }
    if (kind === REC_END) { let info = {}; try { info = JSON.parse(new TextDecoder().decode(payload)); } catch (_) {} this._end(info); }
  }

  _video(pts, keyframe, data) {
    const now = performance.now();
    this.arrivals.push([now, pts]);
    while (this.arrivals.length && now - this.arrivals[0][0] > 6000) this.arrivals.shift();
    const units = nalUnits(data);
    const slices = [];
    for (const unit of units) {
      const type = unit[0] & 0x1f;
      if (type === 7) this.sps = unit;
      else if (type === 8) this.pps = unit;
      else if (type !== 9) slices.push(unit);
    }
    if (keyframe && this.sps && this.pps) this._configure();
    if (!this.configured || !slices.length || (this.needKey && !keyframe)) return;
    this.needKey = false;
    const size = slices.reduce((sum, unit) => sum + 4 + unit.length, 0);
    const avcc = new Uint8Array(size);
    let offset = 0;
    for (const unit of slices) {
      new DataView(avcc.buffer).setUint32(offset, unit.length);
      avcc.set(unit, offset + 4);
      offset += 4 + unit.length;
    }
    this.pending.push(new EncodedVideoChunk({type: keyframe ? "key" : "delta", timestamp: pts, data: avcc}));
    this._feedDecoder();
  }

  _feedDecoder() {
    while (this.pending.length && this._decodedCount() < DECODED_MAX && this.decoder?.state === "configured") {
      const chunk = this.pending.shift();
      try { this.decoder.decode(chunk); } catch (_) { this.needKey = true; }
    }
    this._room();
  }

  _configure() {
    const sps = this.sps, pps = this.pps;
    const codec = `avc1.${[sps[1], sps[2], sps[3]].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
    const key = `${codec}:${sps.length}:${pps.length}`;
    if (this.configured && this.configKey === key) return;
    const description = new Uint8Array(11 + sps.length + pps.length);
    description.set([1, sps[1], sps[2], sps[3], 0xff, 0xe1, sps.length >> 8, sps.length & 0xff], 0);
    description.set(sps, 8);
    description.set([1, pps.length >> 8, pps.length & 0xff], 8 + sps.length);
    description.set(pps, 11 + sps.length);
    if (!this.decoder) {
      this.decoder = new VideoDecoder({
        output: (frame) => this._output(frame),
        error: (error) => { if (!this.destroyed) this.onError?.(error?.message || "Video decoding failed"); },
      });
    }
    try {
      this.decoder.configure({codec, description, optimizeForLatency: true});
      this.configured = true; this.configKey = key; this.needKey = true;
    } catch (error) {
      this.onError?.(`This browser cannot decode the camera's video (${codec})`);
    }
  }

  _output(frame) {
    if (this.destroyed) { frame.close(); return; }
    this.frames.push(frame);
  }

  _flush() {
    for (const frame of this.frames) frame.close();
    this.frames = []; this.pending = []; this.audioQueue = []; this.arrivals = [];
    this.stepOnce = this.paused;
    this.lastPts = null; this.rendered = 0; this.speed = null; this.needKey = true;
    try { if (this.decoder && this.decoder.state === "configured") this.decoder.reset(); } catch (_) {}
    if (this.decoder && this.decoder.state !== "closed" && this.sps && this.pps) { this.configured = false; this.configKey = null; this._configure(); }
    this._room();
  }

  _room() {
    if (this.roomResolve && this.pending.length < PENDING_RESUME) { const resolve = this.roomResolve; this.roomResolve = null; resolve(); }
  }

  _tick() {
    if (this.destroyed) return;
    // A timer, not requestAnimationFrame: the clock must keep running in a background tab.
    this.timer = setTimeout(() => this._tick(), 8);
    const now = performance.now();
    this._feedDecoder();
    if ((!this.paused || this.stepOnce) && this.frames.length) {
      const frame = this.frames[0];
      const gap = this.lastPts === null ? 0 : frame.timestamp - this.lastPts;
      const due = this.lastPts === null || gap < 0 || gap > GAP_COLLAPSE_US ? now : this.lastWall + gap / 1000;
      if (now >= due) {
        this.stepOnce = false;
        this.frames.shift();
        this._paint(frame);
        this.lastWall = now - due < 30 ? due : now;
        this.lastPts = frame.timestamp;
        frame.close();
        this.rendered += 1;
        if (this.rendered === 1) this._state("playing");
        this.onProgress?.(this);
      }
    }
    this._scheduleAudio(now);
    if (this.arrivals.length > 5) {
      const [w0, p0] = this.arrivals[0], [w1, p1] = this.arrivals[this.arrivals.length - 1];
      if (w1 - w0 > 4000) { this.speed = (p1 - p0) / 1000 / (w1 - w0); this._syncSpeed(); }
    }
  }

  _paint(frame) {
    const width = frame.displayWidth, height = frame.displayHeight;
    if (this.canvas.width !== width || this.canvas.height !== height) { this.canvas.width = width; this.canvas.height = height; }
    this.ctx.drawImage(frame, 0, 0, width, height);
  }

  _scheduleAudio(now) {
    if (!this.audio || this.paused || this.lastPts === null) return;
    while (this.audioQueue.length && this.audioQueue[0].pts <= this.lastPts + 600000) {
      const {pts, data} = this.audioQueue.shift();
      if (this.muted) continue;
      const wallMs = this.lastWall + (pts - this.lastPts) / 1000;
      let when = this.audio.currentTime + (wallMs - now) / 1000;
      if (when < this.audio.currentTime - 0.5) continue;
      if (when < this.audio.currentTime + 0.005) when = this.audio.currentTime + 0.005;
      const buffer = this.audio.createBuffer(1, data.length, 8000);
      const samples = buffer.getChannelData(0);
      for (let i = 0; i < data.length; i++) samples[i] = ALAW[data[i]];
      const source = this.audio.createBufferSource();
      source.buffer = buffer; source.connect(this.gain); source.start(when);
    }
  }

  // Timestamps are epoch microseconds; the camera's clock is epoch plus the UTC offset that
  // applied at that moment, which the bridge sends whenever it changes.
  get secondsOfDay() {
    if (this.lastPts === null) return null;
    let offset = this.offsets.length ? this.offsets[0].offset : -new Date().getTimezoneOffset() * 60;
    for (const entry of this.offsets) { if (entry.pts <= this.lastPts) offset = entry.offset; else break; }
    return Math.floor((this.lastPts / 1e6 + offset) % 86400 + 86400) % 86400;
  }
  get clock() {
    const s = this.secondsOfDay;
    if (s === null) return null;
    return [Math.floor(s / 3600), Math.floor((s % 3600) / 60), s % 60].map((n) => String(n).padStart(2, "0")).join(":");
  }

  _action(name) {
    if (name === "toggle") this.paused ? this.resume() : this.pause();
    else if (name === "mute") {
      this.muted = !this.muted;
      if (this.muted === false && this.volume === 0) this.volume = 1;
      if (this.gain) this.gain.gain.value = this.muted ? 0 : this.volume;
      const slider = this.element.querySelector(".fp-volume"); if (slider) slider.value = this.muted ? 0 : this.volume;
      this._syncButtons();
    }
    else if (name === "full") this.toggleFullscreen();
    this._showBar(2500);
  }

  pause() { if (this.paused || this.ended) return; this.paused = true; this.element.classList.add("paused"); this._syncButtons(); this._state("paused"); }
  resume() {
    if (!this.paused) return;
    this.paused = false; this.element.classList.remove("paused");
    this.lastWall = performance.now(); this.audio?.resume().catch(() => {});
    this._syncButtons(); this._state("playing");
  }

  toggleFullscreen() {
    if (document.fullscreenElement) { document.exitFullscreen?.(); return; }
    if (this.element.requestFullscreen) { this.element.requestFullscreen().catch(() => this._iosFullscreen()); return; }
    this._iosFullscreen();
  }

  _iosFullscreen() {
    // iPhone only lets <video> go full screen; mirror the canvas into one.
    try {
      if (!this.mirror) {
        this.mirror = document.createElement("video");
        this.mirror.playsInline = true; this.mirror.muted = true; this.mirror.setAttribute("playsinline", "");
        this.mirror.style.cssText = "position:absolute;width:1px;height:1px;opacity:0;pointer-events:none";
        this.mirror.srcObject = this.canvas.captureStream(30);
        this.element.append(this.mirror);
      }
      this.mirror.play().catch(() => {});
      this.mirror.webkitEnterFullscreen?.();
    } catch (_) {}
  }

  _showBar(ms) {
    clearTimeout(this.barTimer);
    if (!ms) { this.element.classList.remove("show-bar"); return; }
    this.element.classList.add("show-bar");
    this.barTimer = setTimeout(() => this.element.classList.remove("show-bar"), ms);
  }

  _syncButtons() {
    const set = (act, name, label) => { const b = this.element.querySelector(`button[data-act="${act}"]`); if (b) { b.innerHTML = icon(name); b.setAttribute("aria-label", label); } };
    set("toggle", this.paused ? "play" : "pause", this.paused ? "Play" : "Pause");
    set("mute", this.muted ? "muted" : "sound", this.muted ? "Unmute" : "Mute");
    set("full", document.fullscreenElement ? "unfull" : "full", document.fullscreenElement ? "Exit full screen" : "Full screen");
    const mute = this.element.querySelector('button[data-act="mute"]'); if (mute) mute.hidden = !this.hasAudio;
    const slider = this.element.querySelector(".fp-volume"); if (slider) slider.hidden = !this.hasAudio;
  }

  _syncSpeed() {
    const badge = this.element.querySelector(".fp-speed");
    if (badge) badge.textContent = this.speed !== null && this.speed < 0.9 ? `${this.speed.toFixed(1)}×` : "";
  }

  _state(name) {
    const wait = this.element.querySelector(".fp-wait");
    if (wait) {
      wait.classList.toggle("visible", name === "opening" || name === "seeking");
      const text = wait.querySelector(".fp-wait-text"); if (text) text.textContent = name === "seeking" ? "Seeking" : "Opening recording";
    }
    this.onState?.(name);
  }

  _end(info) {
    this.ended = true;
    this.pending = [];
    this._state("ended");
    this.onEnded?.(info || {});
  }

  destroy() {
    this.destroyed = true;
    clearTimeout(this.timer);
    clearTimeout(this.barTimer);
    document.removeEventListener("fullscreenchange", this._onFullscreen);
    try { this.abort?.abort(); } catch (_) {}
    for (const frame of this.frames) frame.close();
    this.frames = [];
    try { this.decoder?.close(); } catch (_) {}
    try { this.audio?.close(); } catch (_) {}
    this.element.remove();
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
    this._mode = "recording";
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
    this._stream = null;
    this._framePlayer = null;
    this._streamPollTimer = null;
    this._progressTimer = null;
    this._reportedRendered = -1;
    this._slowHint = false;
    this._recordingPlayer = null;
    this._recordingController = null;
    this._pollTimer = null;
    this._sessionPollTimer = null;
    this._playbackRetryTimer = null;
    this._playbackRetryCount = 0;
    this._playbackRetryPending = false;
    this._statusTimer = null;
    this._narrow = false;
    if (!customElements.get("ha-button")) {
      customElements.whenDefined("ha-button").then(() => { if (this.isConnected && !this._isPlaying()) this._render(); });
    }
  }

  set hass(value) {
    this._hass = value;
    this._syncMenuButton();
    if (!this._loaded && value) {
      this._loaded = true;
      this._bootstrap();
    }
  }
  get hass() { return this._hass; }
  set narrow(value) {
    const changed = this._narrow !== Boolean(value);
    this._narrow = Boolean(value);
    if (changed && !this._isPlaying()) this._render();
    else this._syncMenuButton();
  }
  set panel(value) { this._panel = value; }
  _isPlaying() { return Boolean(this._playbackSession?.playlist_url || this._stream); }
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
    clearTimeout(this._streamPollTimer);
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
      if (["low","balanced","data_saver"].includes(value.recordingQuality || value.quality)) this._recordingQuality = "low";
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
      const date = params.get("date");
      const time = params.get("time");
      if (camera) this._cameraId = camera;
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
        date:this._date, cameraId:this._cameraId, recordingQuality:this._recordingQuality,
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
  }

  async _loadTimeline(refresh = false) {
    if (!this._cameraId) return;
    const showLoading = !this._busy;
    if (showLoading) {
      this._message = "Loading timeline…";
      if (!this._isPlaying()) this._render(); else this._updateStatusLine();
    }
    try {
      this._timeline = await this._api("GET", `tvt_archive/${this._entryId}/cameras/${this._cameraId}/timeline?date=${encodeURIComponent(this._date)}${refresh ? "&refresh=1" : ""}`);
      this._error = "";
    } catch (error) {
      this._error = errorText(error);
    }
    if (showLoading) this._message = "";
    if (!this._isPlaying()) this._render(); else this._updateStatusLine();
  }

  async _loadStatus(refresh = false) {
    if (!this._cameraId) return;
    try {
      this._status = await this._api("GET", `tvt_archive/${this._entryId}/cameras/${this._cameraId}/status${refresh ? "?refresh=1" : ""}`);
      this._error = "";
    } catch (error) {
      this._error = errorText(error);
    }
    if (this._isPlaying()) this._renderStatusOnly(); else this._render();
    clearTimeout(this._statusTimer);
    this._statusTimer = setTimeout(() => this._loadStatus(), 120000);
  }

  _effectiveRecordingQuality() {
    return this._recordingQuality === "low" ? "low" : "original";
  }

  _timelineHtml() {
    if (!this._timeline) return `<div class="empty">Loading timeline…</div>`;
    const width = this._zoom * 100;
    let html = `<div class="timeline-inner zoom-${this._zoom}" style="width:${width}%">`;
    for (let hour = 0; hour <= 24; hour += 2) {
      const mobileMinor = hour % 4 ? " mobile-minor" : "";
      html += `<div class="tick${mobileMinor}" style="left:${(hour / 24) * 100}%"><span>${pad(hour)}:00</span></div>`;
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
    const mode = this._stream?.video || this._playbackSession?.video || status.encoder?.name || "";
    return {
      recording: status.timeline_today?.recording_now ? "Running" : status.online === false ? "Offline" : "Not active",
      today: status.timeline_today?.recorded_hours == null ? "—" : historyText(status.timeline_today.recorded_hours),
      history: historyText(status.availability?.available_history_hours),
      video: {software:"Software", copy:"Off", vaapi:"VAAPI"}[mode] || "—",
      oldest: status.availability?.earliest ? new Date(status.availability.earliest).toLocaleString() : "—",
      latest: status.availability?.latest ? new Date(status.availability.latest).toLocaleString() : "—",
    };
  }

  _keyframeHint() {
    const gop = Number(this._status?.gop_seconds || 0);
    const mode = this._playbackSession?.video;
    if (!mode || mode === "copy" || gop <= 2) return "";
    return `Camera keyframes every ${Math.round(gop)} s. Set the camera's I-frame interval to 2 s or less to play without re-encoding.`;
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
    const qualities = [
      ["original", "Original"],
      ["low", "Low (480p)"],
    ].map(([value, label]) => `<option value="${value}" ${value === this._recordingQuality ? "selected" : ""}>${label}</option>`).join("");
    const qualityLabel = "Recording quality";
    const btn = (id, label, extra = "", appearance = "plain") => customElements.get("ha-button")
      ? `<ha-button id="${id}" appearance="${appearance}" ${extra}>${label}</ha-button>`
      : `<button id="${id}" class="${appearance}" ${extra}>${label}</button>`;
    const menu = customElements.get("ha-menu-button") ? `<ha-menu-button id="menu"></ha-menu-button>` : "";

    this.shadowRoot.innerHTML = `<style>
      :host{display:block;min-height:100%;background:var(--primary-background-color);color:var(--primary-text-color);box-sizing:border-box}
      *{box-sizing:border-box;min-width:0}.page{max-width:1600px;margin:0 auto;padding:18px;display:grid;gap:14px}
      .toolbar{display:flex;align-items:center;height:56px;gap:4px;margin:-6px 0 -4px -8px}.title{font-size:20px;font-weight:400;line-height:1}.subtle{color:var(--secondary-text-color);font-size:.9rem}

      .controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;max-width:100%}.controls label{flex:1 1 150px}
      label{display:grid;gap:4px;color:var(--secondary-text-color);font-size:.78rem;min-width:0}label>span{white-space:nowrap}
      input,select{font:inherit;color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:10px;padding:9px 11px;min-width:0;max-width:100%;width:100%}input[type="time"],input[type="date"]{display:block;-webkit-appearance:none;appearance:none}
      button,.download-link{cursor:pointer;font:inherit;font-weight:500;font-size:14px;color:var(--primary-color);background:none;border:0;border-radius:var(--ha-button-border-radius,var(--ha-border-radius-pill,4px));padding:0 var(--ha-space-4,12px);height:var(--ha-button-height,var(--button-height,36px));white-space:nowrap;display:inline-flex;align-items:center;justify-content:center;text-decoration:none;transition:background-color .15s}button:hover,.download-link:hover{background:rgba(var(--rgb-primary-color,3,169,244),.08)}button:active,.download-link:active{background:rgba(var(--rgb-primary-color,3,169,244),.18)}button.filled{background:var(--ha-color-fill-primary-normal-resting,rgba(var(--rgb-primary-color,3,169,244),.16));color:var(--ha-color-on-primary-normal,var(--primary-color))}button.filled:hover{background:var(--ha-color-fill-primary-normal-hover,rgba(var(--rgb-primary-color,3,169,244),.28))}button.filled:active{background:var(--ha-color-fill-primary-normal-active,rgba(var(--rgb-primary-color,3,169,244),.16))}button.outlined{border:1px solid var(--ha-color-border-primary-loud,var(--primary-color));color:var(--ha-color-on-primary-normal,var(--primary-color))}button.outlined:hover{background:var(--ha-color-fill-primary-quiet-hover,rgba(var(--rgb-primary-color,3,169,244),.08))}button.outlined:active{background:var(--ha-color-fill-primary-quiet-active,rgba(var(--rgb-primary-color,3,169,244),.14))}button:disabled{cursor:default;color:var(--ha-color-on-disabled-normal,var(--disabled-text-color,#9b9b9b));background:none;border-color:var(--ha-color-on-disabled-quiet,var(--disabled-color,rgba(128,128,128,.3)))}button.filled:disabled{background:var(--ha-color-fill-disabled-normal-resting,var(--disabled-color,rgba(128,128,128,.2)))}
      .shell{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:14px}.card{background:var(--ha-card-background,var(--card-background-color));border-radius:var(--ha-card-border-radius,12px);border:var(--ha-card-border-width,1px) solid var(--ha-card-border-color,var(--divider-color));box-shadow:var(--ha-card-box-shadow,none);overflow:hidden;min-width:0}
      .player-head{padding:11px 14px;display:flex;justify-content:space-between;align-items:center;gap:10px;border-bottom:1px solid var(--divider-color)}
      .player{background:#000;min-height:min(62vh,640px);display:grid;place-items:center;position:relative;overflow:hidden}
      .player video,.player .frame-player{width:100%;height:min(62vh,640px);object-fit:contain;background:#000;display:block;grid-area:1/1}.player-state{display:none;grid-area:1/1;align-self:stretch;justify-self:stretch;z-index:2;align-items:center;justify-content:center;pointer-events:none;color:#fff;font-size:.92rem}.player-state.visible{display:flex}.player-state-content{display:flex;align-items:center;gap:10px;padding:10px 13px;border-radius:10px;background:rgba(0,0,0,.58);color:#fff}.player-spinner{width:18px;height:18px;border-radius:50%;border:3px solid rgba(255,255,255,.25);border-top-color:#fff;animation:tvt-spin .8s linear infinite}@keyframes tvt-spin{to{transform:rotate(360deg)}}
      .frame-player{position:relative;width:100%;background:#000;display:grid;grid-area:1/1;overflow:hidden;cursor:default}.frame-player canvas{width:100%;height:100%;object-fit:contain;display:block;grid-area:1/1;background:#000}.frame-player:fullscreen{width:100vw;height:100vh}
      .fp-wait{display:none;grid-area:1/1;place-self:center;align-items:center;gap:10px;padding:10px 13px;border-radius:10px;background:rgba(0,0,0,.58);color:#fff;font-size:.92rem;pointer-events:none}.fp-wait.visible{display:flex}
      .fp-bar{position:absolute;left:0;right:0;bottom:0;display:flex;align-items:center;gap:4px;padding:4px 6px;background:linear-gradient(transparent,rgba(0,0,0,.65));color:#fff;opacity:0;transition:opacity .2s;pointer-events:none}.frame-player.show-bar .fp-bar,.frame-player.paused .fp-bar{opacity:1;pointer-events:auto}
      .fp-bar button{color:#fff;width:40px;height:40px;padding:0;display:grid;place-items:center;border-radius:50%}.fp-bar button:hover{background:rgba(255,255,255,.15)}.fp-bar button:active{background:rgba(255,255,255,.3)}.fp-bar svg{width:26px;height:26px;fill:currentColor}.fp-speed{font-size:.8rem;opacity:.85;margin-left:6px}.fp-space{flex:1}.fp-volume{display:none;width:80px;height:auto;margin:0 4px;padding:0;border:0;background:none;border-radius:0;accent-color:#fff}@media(hover:hover) and (pointer:fine){.fp-volume:not([hidden]){display:block}}
      .empty{padding:32px;text-align:center;color:#bdbdbd}.sidebar{padding:14px;display:grid;gap:10px;align-content:start}.stat{padding:10px 12px;background:var(--secondary-background-color);border-radius:10px}.stat span{color:var(--secondary-text-color);font-size:.8rem}.stat b{display:block;margin-top:3px;overflow-wrap:anywhere}
      .timeline-card{padding:12px}.timeline-title{display:flex;justify-content:space-between;gap:10px;margin-bottom:8px}.timeline{height:112px;overflow-x:auto;overflow-y:hidden;position:relative;background:var(--secondary-background-color);border-radius:10px;cursor:crosshair}.timeline-inner{height:100%;position:relative;min-width:100%}.segment{position:absolute;top:38px;height:42px;border-radius:6px;background:var(--success-color,#43a047)}.tick{position:absolute;top:0;bottom:0;width:1px;background:var(--divider-color)}.tick span{position:absolute;left:0;top:7px;transform:translateX(-50%);font-size:.7rem;color:var(--secondary-text-color);white-space:nowrap}.tick:first-child span{left:6px;transform:none}.tick:last-child span{left:auto;right:6px;transform:none}.marker{position:absolute;top:28px;bottom:12px;width:2px;background:var(--error-color,#e53935)}.marker:after{content:"";position:absolute;top:-5px;left:-4px;width:10px;height:10px;border-radius:50%;background:inherit}
      .lower{display:grid;grid-template-columns:1fr 1fr;gap:14px}.box{padding:13px;min-width:0;overflow:hidden}.selection-controls{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:end}.selection-controls>*{min-width:0;max-width:100%}.range{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) auto;gap:8px;align-items:end}.range>*{min-width:0;max-width:100%}.download-progress{display:none;grid-column:1/-1;align-items:center;gap:9px;font-size:.78rem;color:var(--secondary-text-color)}.download-progress.visible{display:flex}.download-track{height:5px;flex:1;overflow:hidden;border-radius:999px;background:var(--divider-color)}.download-bar{height:100%;width:0;background:var(--primary-color);transition:width .25s ease}.download-percent{min-width:34px;text-align:right;font-variant-numeric:tabular-nums}.statusline{min-height:22px;color:var(--secondary-text-color)}.error{color:var(--error-color)}
      @media(min-width:901px) and (min-height:720px){
        :host{height:100dvh;overflow:hidden}.page{height:100%;overflow:hidden;padding:12px 18px;gap:10px;grid-template-rows:auto auto minmax(0,1fr) auto auto auto}
        .shell{min-height:0;gap:10px}.shell>.card:first-child{display:grid;grid-template-rows:auto minmax(0,1fr);min-height:0}.player{height:100%;min-height:0}.player video,.player .frame-player{height:100%;min-height:0;max-height:none}
        .sidebar{min-height:0;overflow:hidden;padding:10px;gap:7px}.stat{padding:7px 10px}.timeline-card{padding:9px 10px}.timeline-title{margin-bottom:6px}.timeline{height:92px}.segment{top:31px;height:35px}.marker{top:23px;bottom:10px}
        .lower{gap:10px}.box{padding:10px}.statusline{min-height:18px;font-size:.8rem}
      }
      @media(max-width:900px){.shell{grid-template-columns:1fr}.sidebar{grid-template-columns:repeat(2,minmax(0,1fr))}.lower{grid-template-columns:1fr}.player,.player video,.player .frame-player{min-height:42vh;height:42vh}.page{padding:10px}}
      @media(max-width:560px){
        .controls{display:grid;grid-template-columns:1fr 1fr;gap:10px}.controls label{width:100%}.controls button,.controls ha-button{grid-column:1/-1;justify-self:end}
        .player-head{display:grid;grid-template-columns:1fr auto;align-items:center}.player-head>b{grid-column:1/-1}.player-head>.subtle{grid-column:1/-1}.sidebar{grid-template-columns:1fr 1fr}.timeline-title{align-items:flex-start}.timeline-title span{max-width:68%}.timeline-inner.zoom-1 .tick.mobile-minor span{display:none}
        .selection-controls{grid-template-columns:1fr}.selection-controls label{grid-column:auto;width:100%;overflow:hidden}.selection-controls input{width:100%;max-width:100%}.selection-controls button,.selection-controls ha-button{width:100%}
        .range{grid-template-columns:1fr 1fr}.range button,.range ha-button,.range .download-link{grid-column:1/-1;width:100%}.box{padding:11px}.page{overflow-x:hidden}
      }
      @media(max-width:370px){.sidebar{grid-template-columns:1fr}.selection-controls{grid-template-columns:1fr}.selection-controls label{grid-column:auto}.range{grid-template-columns:1fr}.range button,.range ha-button,.range .download-link{grid-column:auto}.download-progress{grid-column:1}}
    </style><div class="page">
      <div class="toolbar">${menu}<div class="title">Recordings</div></div>
      <div class="controls"><label><span>Camera</span><select id="camera">${cameras}</select></label><label><span>Date</span><input id="date" type="date" value="${$esc(this._date)}"></label><label><span>${qualityLabel}</span><select id="quality">${qualities}</select></label><label><span>Timeline</span><select id="zoom"><option value="1">24 hours</option><option value="2">12-hour</option><option value="4">6-hour</option></select></label>${btn("refresh", "Refresh")}</div>
      <div class="shell"><div class="card"><div class="player-head"><b>${$esc(this._camera?.name || "Camera")}</b><span class="subtle">${$esc(this._date)} ${$esc(selected)}</span></div><div id="player" class="player"></div></div>
        <div class="card sidebar">
          <div class="stat"><span>Recording</span><b id="stat-recording">${$esc(values.recording)}</b></div><div class="stat"><span>Recorded today</span><b id="stat-today">${$esc(values.today)}</b></div>
          <div class="stat"><span>Available history</span><b id="stat-history">${$esc(values.history)}</b></div>
<div class="stat"><span>Encoding</span><b id="stat-video">${$esc(values.video)}</b></div>
          <div class="stat"><span>Oldest recording</span><b id="stat-oldest">${$esc(values.oldest)}</b></div><div class="stat"><span>Latest recording</span><b id="stat-latest">${$esc(values.latest)}</b></div>
        </div>
      </div>
      <div class="card timeline-card"><div class="timeline-title"><span>Click a recorded section to select a time</span><b>${selected}</b></div><div id="timeline" class="timeline">${this._timelineHtml()}</div></div>
      <div class="lower"><div class="card box"><div class="selection-controls"><label><span>Selected time</span><input id="selected" type="time" step="1" value="${selected}"></label>${btn("play", "Play from here", "", "filled")}</div></div>
        <div class="card box"><div class="range"><label><span>Download start</span><input id="range-start" type="time" step="1" value="${$esc(this._rangeStart)}"></label><label><span>Download end</span><input id="range-end" type="time" step="1" value="${$esc(this._rangeEnd)}"></label>${this._downloadUrl ? `<a id="save-download" class="download-link" href="${$esc(this._downloadUrl)}" download="${$esc(this._downloadFilename || "recording.mp4")}">Save file</a>` : btn("download", this._busy ? this._downloadActionLabel() : "Download original", this._busy ? "disabled" : "", "outlined")}<div id="download-progress" class="download-progress ${this._busy || this._downloadPercent === 100 ? "visible" : ""}"><div class="download-track" role="progressbar" aria-label="Export progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.max(0, Math.min(100, Math.round(this._downloadPercent || 0)))}"><div id="download-progress-bar" class="download-bar" style="width:${Math.max(0, Math.min(100, Math.round(this._downloadPercent || 0)))}%"></div></div><span id="download-percent" class="download-percent">${Math.max(0, Math.min(100, Math.round(this._downloadPercent || 0)))}%</span></div></div></div></div>
      <div id="statusline" class="statusline ${this._error ? "error" : ""}">${$esc(this._error || this._message || "")}</div>
    </div>`;
    this._bind();
    this._syncMenuButton();
    this._renderPlayer();
  }

  _syncMenuButton() {
    const menu = this.shadowRoot.getElementById("menu");
    if (menu) { menu.hass = this._hass; menu.narrow = this._narrow; }
  }

  _bind() {
    const get = (id) => this.shadowRoot.getElementById(id);
    const zoom = get("zoom"); if (zoom) zoom.value = String(this._zoom);
    get("camera")?.addEventListener("change", async (event) => {
      await this._stopPlaybackSession();
      this._resetDownloadResult();
      this._cameraId = event.target.value;
      this._camera = this._cameras.find((camera) => camera.id === this._cameraId);
      this._timeline = null; this._status = null; this._mode = "recording"; this._persistState();
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
      const wasPlaying = this._isPlaying();
      if (wasPlaying) this._selectedSec = this._playedSecondsOfDay();
      this._recordingQuality = event.target.value;
      this._persistState();
      if (!wasPlaying) { this._render(); return; }
      await this._stopPlaybackSession();
      this._playRecording();
    });
    get("zoom")?.addEventListener("change", (event) => { this._zoom = Number(event.target.value); this._persistState(); if (!this._isPlaying()) this._render(); });
    get("refresh")?.addEventListener("click", async () => {
      this._message = "Refreshing…"; this._updateStatusLine();
      await this._loadCameras();
      await Promise.all([this._loadTimeline(true), this._loadStatus(true)]);
      this._persistState();
    });
    get("play")?.addEventListener("click", () => this._playRecording());
    get("selected")?.addEventListener("change", async (event) => {
      const value = event.currentTarget.value;
      this._resetDownloadResult();
      this._selectTime(clockToSec(value));
      if (this._canSeek()) { this._updateSelection(); this._seekStream(this._selectedSec); return; }
      await this._stopPlaybackSession();
      this._render();
    });
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
      this._resetDownloadResult();
      const rect = inner.getBoundingClientRect();
      this._selectTime(Math.max(0, Math.min(86399, ((event.clientX - rect.left) / rect.width) * 86400)));
      this._mode = "recording";
      if (this._canSeek()) { this._updateSelection(); this._seekStream(this._selectedSec); return; }
      await this._stopPlaybackSession();
      this._render();
    });
  }

  _selectTime(seconds) {
    this._selectedSec = seconds;
    this._rangeStart = secToClock(seconds);
    this._rangeEnd = secToClock(Math.min(86399, seconds + 300));
    this._persistState();
  }

  _canSeek() {
    return Boolean(this._stream && this._framePlayer && !this._framePlayer.ended);
  }

  _playedSecondsOfDay() {
    const streamed = this._framePlayer?.secondsOfDay;
    if (streamed != null) return streamed;
    return Math.min(86399, this._selectedSec + Math.floor(this._currentPlaybackTime()));
  }

  _updateSelection() {
    const clock = secToClock(this._selectedSec);
    const set = (selector, value) => { const node = this.shadowRoot.querySelector(selector); if (node) node[selector.startsWith("#") ? "value" : "textContent"] = value; };
    set("#selected", clock); set("#range-start", this._rangeStart); set("#range-end", this._rangeEnd);
    set(".timeline-title b", clock); set(".player-head .subtle", `${this._date} ${clock}`);
    this._moveMarker(this._selectedSec);
  }

  _moveMarker(seconds) {
    const marker = this.shadowRoot.querySelector(".marker");
    if (marker) marker.style.left = `${(seconds / 86400) * 100}%`;
  }

  async _renderPlayer() {
    const host = this.shadowRoot.getElementById("player"); if (!host) return;
    if (this._framePlayer) { host.replaceChildren(this._framePlayer.element); return; }
    if (!this._playbackSession?.playlist_url) {
      if (this._recordingController && this._recordingPlayer?.isConnected) return;
      host.innerHTML = `<div class="empty">Select a recorded time and press <b>Play from here</b>.</div>`;
      return;
    }
    this._attachHlsPlayer(host, this._playbackSession.playlist_url);
  }

  _setRecordingPlayerState(state, detail = {}) {
    const overlay = this.shadowRoot.getElementById("player-state");
    if (!overlay) return;
    const alreadyStarted = Boolean(this._recordingController?.started);
    if (state === "playing" || state === "seeking" || alreadyStarted) {
      overlay.className = "player-state";
      overlay.replaceChildren();
      return;
    }
    overlay.className = "player-state visible";
    overlay.innerHTML = `<div class="player-state-content"><span class="player-spinner"></span><span>Opening recording</span></div>`;
  }

  _createRecordingPlayer(host, url = null) {
    this._recordingController?.destroy();
    const video = this._recordingPlayer || document.createElement("video");
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
    clearTimeout(this._streamPollTimer);
    clearInterval(this._progressTimer);
    const stream = this._stream;
    this._stream = null;
    this._slowHint = false;
    this._framePlayer?.destroy();
    this._framePlayer = null;
    if (stream?.id && this._entryId) {
      this._api("DELETE", `tvt_archive/${this._entryId}/streams/${stream.id}`).catch(() => {});
    }
    const current = this._playbackSession;
    this._playbackSession = null;
    this._recordingController?.destroy();
    this._recordingController = null;
    if (current?.id && this._entryId) {
      this._api("DELETE", `tvt_archive/${this._entryId}/sessions/${current.id}`).catch(() => {});
    }
    if (render) this._render();
  }

  async _playRecording(automaticRetry = false) {
    clearTimeout(this._playbackRetryTimer);
    if (!automaticRetry) { this._playbackRetryCount = 0; this._playbackRetryPending = false; }
    if (FRAME_PLAYER_SUPPORTED) { this._playStream(); return; }
    this._stopPlaybackSession();
    this._mode = "recording";
    this._persistState();
    this._error = "";
    const quality = this._effectiveRecordingQuality();
    const duration = Math.max(5, Math.min(900, 86400 - Math.floor(this._selectedSec)));
    this._message = `Opening ${quality.replaceAll("_", " ")} recording`;
    this._render();
    this._primeRecordingPlayer();
    try {
      const session = await this._api("POST", `tvt_archive/${this._entryId}/cameras/${this._cameraId}/sessions`, {
        start: isoFor(this._date, secToClock(this._selectedSec)), duration, quality, gain_db: 0,
      });
      this._playbackSession = session;
      if (session.player_script_url) loadHlsLibrary(session.player_script_url).catch(() => {});
      await this._pollPlaybackSession(session.id);
    } catch (error) {
      this._handlePlaybackFailure(errorText(error));
    }
  }

  async _playStream() {
    if (this._canSeek()) { this._seekStream(this._selectedSec); return; }
    this._stopPlaybackSession();
    this._mode = "recording";
    this._persistState();
    this._error = "";
    this._message = "Opening recording";
    this._render();
    const player = new TVTFramePlayer({
      onState: (state) => { if (state === "playing") { this._playbackRetryCount = 0; this._playbackRetryPending = false; } },
      onError: (message) => this._handlePlaybackFailure(message),
      onEnded: (error) => this._streamEnded(error),
      onProgress: (current) => this._streamProgress(current),
    });
    this._framePlayer = player;
    this._reportedRendered = -1;
    this.shadowRoot.getElementById("player")?.replaceChildren(player.element);
    player.prime();
    try {
      const session = await this._api("POST", `tvt_archive/${this._entryId}/cameras/${this._cameraId}/streams`, {
        start: isoFor(this._date, secToClock(this._selectedSec)), quality: this._effectiveRecordingQuality(),
      });
      if (this._framePlayer !== player) return;
      this._stream = session;
      player.start(session.frames_url);
      this._progressTimer = setInterval(() => this._reportProgress(), 2000);
      this._pollStream(session.id);
    } catch (error) {
      this._handlePlaybackFailure(errorText(error));
    }
  }

  async _seekStream(seconds) {
    const stream = this._stream;
    if (!stream) return;
    this._error = ""; this._message = ""; this._updateStatusLine();
    try {
      await this._api("POST", `tvt_archive/${this._entryId}/streams/${stream.id}`, {seek: secToClock(Math.floor(seconds))});
    } catch (error) {
      this._handlePlaybackFailure(errorText(error));
    }
  }

  async _pollStream(id) {
    try {
      const fresh = await this._api("GET", `tvt_archive/${this._entryId}/streams/${id}`);
      if (this._stream?.id !== id) return;
      this._stream = {...fresh, frames_url: this._stream.frames_url};
      if (fresh.status === "error") throw new Error(fresh.error || "Playback failed");
      this._renderStatusOnly();
      if (fresh.frames_ready || fresh.complete) { this._message = ""; this._updateStatusLine(); return; }
      this._message = fresh.phase || "";
      this._updateStatusLine();
      clearTimeout(this._streamPollTimer);
      this._streamPollTimer = setTimeout(() => this._pollStream(id), 1000);
    } catch (error) {
      this._handlePlaybackFailure(errorText(error));
    }
  }

  _reportProgress() {
    const player = this._framePlayer, stream = this._stream;
    if (!player || !stream || player.rendered === this._reportedRendered) return;
    this._reportedRendered = player.rendered;
    this._api("POST", `tvt_archive/${this._entryId}/streams/${stream.id}`, {rendered: player.rendered, generation: player.generation}).catch(() => {});
  }

  _streamProgress(player) {
    const seconds = player.secondsOfDay;
    if (seconds != null && player.rendered % 5 === 0) {
      this._moveMarker(seconds);
      const head = this.shadowRoot.querySelector(".player-head .subtle");
      if (head) head.textContent = `${this._date} ${player.clock}`;
    }
    const slow = player.speed !== null && player.speed < 0.85;
    if (slow !== this._slowHint) {
      this._slowHint = slow;
      this._message = slow ? "Slow camera link" : "";
      this._updateStatusLine();
    }
  }

  _streamEnded(info) {
    clearInterval(this._progressTimer);
    if (info.error) { this._handlePlaybackFailure(info.error); return; }
    if (info.status === "complete") this._message = "End of the recording";
    else if (info.status === "closed") this._message = "The recording stream closed";
    else this._message = info.phase || "Playback stopped";
    this._updateStatusLine();
  }

  async _pollPlaybackSession(sessionId) {
    try {
      const previous = this._playbackSession;
      const alreadyAttached = Boolean(this._recordingController?.url || previous?.playlist_url);
      const fresh = await this._api("GET", `tvt_archive/${this._entryId}/sessions/${sessionId}`);
      if (this._playbackSession?.id !== sessionId) return;
      // Keep the first signed URLs; every status response carries new ones and swapping recreates the player.
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
        this._message = this._keyframeHint();
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
    return /(no route to host|network is unreachable|temporarily unreachable|connection refused|timed out|timeout|network ?error|fragloaderror|levelloaderror|manifestloaderror|camera archive capture failed|stream ended unexpectedly|stream request failed|failed to fetch|load failed)/i.test(String(message || ""));
  }

  async _handlePlaybackFailure(message) {
    const text = String(message || "The recording could not be opened.");
    if (this._playbackRetryPending) return;
    if (this._mode === "recording" && this._recoverablePlaybackError(text) && this._playbackRetryCount < 3) {
      const resume = this._framePlayer?.secondsOfDay;
      const played = Math.max(0, this._currentPlaybackTime() - 0.25);
      if (resume != null) this._selectedSec = resume;
      else if (played > 0) this._selectedSec = Math.min(86399, this._selectedSec + played);
      this._rangeStart = secToClock(this._selectedSec);
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
    clearInterval(this._progressTimer);
    this._stream = null;
    this._framePlayer?.destroy();
    this._framePlayer = null;
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
      if (this._isPlaying()) this._updateDownloadUi(); else this._render();
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
      if (this._isPlaying()) this._updateDownloadUi(); else this._render();
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
        if (this._isPlaying()) this._updateDownloadUi(); else this._render();
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
      if (this._isPlaying()) this._updateDownloadUi(); else this._render();
      clearTimeout(this._statusTimer);
      this._statusTimer = setTimeout(() => this._loadStatus(), 1000);
    }
  }

}

if (!customElements.get("tvt-archive-panel")) customElements.define("tvt-archive-panel", TVTArchivePanel);
