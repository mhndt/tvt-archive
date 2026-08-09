from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL = (ROOT / "custom_components/tvt_archive/frontend/tvt-archive-panel.js").read_text(
    encoding="utf-8"
)
FLOW = (ROOT / "custom_components/tvt_archive/config_flow.py").read_text(encoding="utf-8")


class FrontendStaticTests(unittest.TestCase):
    def test_simple_archive_labels(self) -> None:
        self.assertIn('"Native TCP/9008"', PANEL)
        self.assertIn('"Recorded RTSP"', PANEL)
        self.assertNotIn("video + audio", PANEL.lower())
        self.assertNotIn("video and audio", FLOW.lower())

    def test_progress_is_real_job_data(self) -> None:
        self.assertIn("job.progress_percent", PANEL)
        self.assertIn("download-progress-bar", PANEL)
        self.assertIn("_downloadActionLabel()", PANEL)
        self.assertIn('return "Preparing"', PANEL)
        self.assertIn('return "Receiving"', PANEL)
        self.assertIn('return "Processing"', PANEL)
        self.assertNotIn("Preparing ${percent}%", PANEL)
        self.assertNotRegex(PANEL, re.compile(r"Preparing[^\n]*%"))
        self.assertIn("setTimeout(() => this._pollDownload(jobId), 1000)", PANEL)

    def test_progress_layout_has_mobile_rules(self) -> None:
        self.assertRegex(PANEL, re.compile(r"\.download-progress\{[^}]*grid-column:1/-1", re.S))
        self.assertIn("@media(max-width:560px)", PANEL)
        self.assertIn(".range button,.range .download-link{grid-column:1/-1;width:100%}", PANEL)
        self.assertIn("@media(max-width:370px)", PANEL)

    def test_save_link_is_explicit_and_mobile_safe(self) -> None:
        self.assertIn('id="save-download"', PANEL)
        self.assertIn('role="progressbar"', PANEL)
        self.assertNotIn("link.click()", PANEL)
        self.assertNotIn("_downloadAutoOpened", PANEL)

    def test_adaptive_archive_clock_avoids_short_camera_stalls(self) -> None:
        self.assertIn("_adaptiveTick()", PANEL)
        self.assertIn("ahead < 0.7", PANEL)
        self.assertIn("target = 0.45", PANEL)
        self.assertIn("target = 0.62", PANEL)
        self.assertIn("target = 0.78", PANEL)
        self.assertIn("target = 0.90", PANEL)
        self.assertIn("this.bufferSupplyRate", PANEL)
        self.assertIn("this.bufferSupplySamples", PANEL)
        self.assertIn("const sustainable =", PANEL)
        self.assertIn("target = 0.97", PANEL)
        self.assertIn("target = 1.02", PANEL)
        self.assertIn("setComplete(complete)", PANEL)

    def test_play_from_here_does_not_consume_the_first_fragment(self) -> None:
        self.assertIn("_primeRecordingPlayer()", PANEL)
        self.assertIn("controller.prime()", PANEL)
        self.assertIn('this._state("opening")', PANEL)
        self.assertIn('video.controls = false', PANEL)
        self.assertIn('video.autoplay = false', PANEL)
        self.assertNotIn('video.autoplay = true', PANEL)
        self.assertIn('this.startBufferSeconds - this.startBufferToleranceSeconds', PANEL)
        self.assertIn('ahead + 0.05 < startupThreshold', PANEL)
        prime = PANEL[PANEL.index("  prime() {"):PANEL.index("  async start() {")]
        self.assertNotIn("_ensureMediaSource()", prime)
        self.assertNotIn("this.video.play()", prime)

    def test_firefox_uses_local_hlsjs_and_keeps_one_signed_url(self) -> None:
        self.assertIn('const loadHlsLibrary = (url)', PANEL)
        self.assertIn('new Hls({', PANEL)
        self.assertIn('Hls.Events.BUFFER_APPENDED', PANEL)
        self.assertIn('Hls.Events.LEVEL_UPDATED', PANEL)
        self.assertIn('maxBufferHole: 0.5', PANEL)
        self.assertIn('const playlistUrl = previous?.playlist_url || fresh.playlist_url || null', PANEL)
        self.assertIn('const playerScriptUrl = previous?.player_script_url || fresh.player_script_url || null', PANEL)
        self.assertNotIn('const playlistUrl = fresh.playlist_url || previous?.playlist_url', PANEL)
        self.assertIn('setTimeout(() => this._pollPlaybackSession(sessionId), 1000)', PANEL)

    def test_player_keeps_loading_until_backend_completion(self) -> None:
        self.assertIn("this.serverComplete = false", PANEL)
        self.assertIn('this.hls.on(Hls.Events.LEVEL_UPDATED', PANEL)
        self.assertIn("setComplete(complete)", PANEL)
        self.assertIn("this.serverComplete = true", PANEL)
        self.assertIn('if (this.video.ended && !this.serverComplete)', PANEL)

    def test_recording_labels_are_compact(self) -> None:
        self.assertIn('["original", "Original"]', PANEL)
        self.assertNotIn("Original / High", PANEL)
        self.assertNotIn("Auto (", PANEL)
        self.assertIn('<option value="2">12-hour</option>', PANEL)
        self.assertIn('<option value="4">6-hour</option>', PANEL)
        self.assertNotIn("12-hour width", PANEL)
        self.assertNotIn("6-hour width", PANEL)

    def test_buffering_overlay_uses_secondary_text_color(self) -> None:
        self.assertRegex(PANEL, re.compile(r"\.player-state-content\{[^}]*color:var\(--secondary-text-color\)", re.S))

    def test_player_recovers_after_real_starvation(self) -> None:
        self.assertIn('_enterRebuffer(reason = "starvation")', PANEL)
        self.assertIn('this._enterRebuffer("low-buffer")', PANEL)
        self.assertIn('this.video.pause()', PANEL)
        self.assertIn('this.freezeTime = null', PANEL)
        self.assertIn('newAhead = Math.max(0, bufferedEnd - frozenAt)', PANEL)
        self.assertIn('target = Math.max(0.9, this.resumeBufferSeconds)', PANEL)
        self.assertIn('this.hls?.startLoad(this.freezeTime)', PANEL)
        self.assertIn('now + 0.08 < frozenAt', PANEL)
        self.assertIn('const result = this.video.play()', PANEL)
        self.assertNotIn('"Catching up"', PANEL)
        enter = PANEL[PANEL.index('  _enterRebuffer(reason = "starvation") {'):PANEL.index("  _maybeRecover() {")]
        self.assertIn('this.video.pause()', enter)
        recover = PANEL[PANEL.index("  _maybeRecover() {"):PANEL.index("  _adaptiveTick() {")]
        self.assertNotIn("this.resume();", recover)


    def test_player_nudges_decoder_when_playing_position_stops(self) -> None:
        self.assertIn("_watchPlaybackProgress(window, ahead, now)", PANEL)
        self.assertIn("now - this.playbackProgressAt < 2.5", PANEL)
        self.assertIn("current + 0.08", PANEL)
        self.assertIn("this.hls?.startLoad(target)", PANEL)
        self.assertIn("this._currentPlaybackTime() - 0.25", PANEL)

    def test_initial_spinner_does_not_return_during_starvation(self) -> None:
        self.assertIn("player-spinner", PANEL)
        self.assertIn('<span class="player-spinner"></span><span>${label}</span>', PANEL)
        self.assertIn('<span class="player-spinner"></span><span>Opening recording</span>', PANEL)
        self.assertIn("transientPlaybackWait", PANEL)
        self.assertIn("overlay.replaceChildren()", PANEL)

    def test_desktop_panel_fits_one_viewport_without_affecting_mobile(self) -> None:
        self.assertIn("@media(min-width:901px) and (min-height:720px)", PANEL)
        self.assertIn(":host{height:100dvh;overflow:hidden}", PANEL)
        self.assertIn("grid-template-rows:auto minmax(0,1fr) auto auto auto", PANEL)
        self.assertIn("@media(max-width:900px)", PANEL)

    def test_compact_header_and_edge_timeline_labels(self) -> None:
        self.assertIn("TVT Archive · v${VERSION}", PANEL)
        self.assertIn('class="subtle version-text"', PANEL)
        self.assertRegex(PANEL, re.compile(r"\.version-text\{[^}]*font-size:\.8rem", re.S))
        self.assertNotIn('<img src="/tvt_archive/logo.png', PANEL)
        self.assertIn(".tick:first-child span", PANEL)
        self.assertIn(".tick:last-child span", PANEL)

    def test_mobile_timeline_labels_do_not_collide(self) -> None:
        self.assertIn('class="timeline-inner zoom-${this._zoom}"', PANEL)
        self.assertIn('const mobileMinor = hour % 4 ? " mobile-minor" : "";', PANEL)
        self.assertIn(".timeline-inner.zoom-1 .tick.mobile-minor span{display:none}", PANEL)

    def test_internal_rebuffer_does_not_surface_native_controls(self) -> None:
        self.assertIn("this.initialControlsShown = false", PANEL)
        self.assertIn("if (!this.initialControlsShown)", PANEL)
        enter = PANEL[PANEL.index('  _enterRebuffer(reason = "starvation") {'):PANEL.index("  _maybeRecover() {")]
        self.assertIn("this.video.controls = false", enter)
        self.assertLess(enter.index("this.video.controls = false"), enter.index("this.video.pause()"))

    def test_firefox_startup_gate_allows_timestamp_alignment_shortfall(self) -> None:
        self.assertIn("this.startBufferToleranceSeconds = 0.45", PANEL)
        self.assertIn("const startupThreshold = Math.max(2.25, this.startBufferSeconds - this.startBufferToleranceSeconds)", PANEL)
        self.assertIn("ahead + 0.05 < startupThreshold", PANEL)

    def test_playback_preserves_audio_level_and_retries_transient_camera_failures(self) -> None:
        self.assertIn('quality, gain_db: 0', PANEL)
        self.assertIn('_recoverablePlaybackError(message)', PANEL)
        self.assertIn('retrying ${this._playbackRetryCount}/3', PANEL)
        self.assertIn('this._playRecording(true)', PANEL)
        self.assertIn('await this._stopPlaybackSession()', PANEL)

    def test_panel_is_recordings_only(self) -> None:
        self.assertNotIn("TVTLiveCameraPlayer", PANEL)
        self.assertNotIn("_selectedLiveProfile", PANEL)
        self.assertNotIn("_effectiveLiveEntity", PANEL)
        self.assertNotIn('id="go-live"', PANEL)
        self.assertNotIn('id="live"', PANEL)
        self.assertNotIn("Live now", PANEL)
        self.assertNotIn("Default live profile", PANEL)
        self.assertNotIn("manage_live_profiles", FLOW)
        self.assertNotIn("live_profile", FLOW)

    def test_error_renderer_does_not_fall_back_to_object_object(self) -> None:
        self.assertIn("JSON.stringify(error)", PANEL)
        self.assertNotIn('return String(error)', PANEL)


if __name__ == "__main__":
    unittest.main()
