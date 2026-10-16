/* Sound cues for the demos, exposed as window.SFX.
   Everything is synthesized live with the Web Audio API - no audio files, nothing
   licensed, nothing to attribute - and only ever plays in response to a click.
   A mute preference persists per visitor in localStorage. */
(() => {
  "use strict";
  const { el } = window.UI;
  const MUTE_KEY = "demo-muted";

  let audioCtx = null;
  let muted = false;
  try { muted = localStorage.getItem(MUTE_KEY) === "1"; } catch (_) { /* private mode */ }

  /** Play one enveloped oscillator tone; silently a no-op when muted or audio fails. */
  function tone(freq, ms, { type = "sine", gain = 0.25, at = 0, slide = 0 } = {}) {
    if (muted) return;
    try {
      audioCtx ||= new (window.AudioContext || window.webkitAudioContext)();
      // Browsers hand out suspended contexts outside (or sometimes even inside) a
      // gesture; resuming on every cue keeps the first click audible.
      if (audioCtx.state === "suspended") audioCtx.resume();
      const t0 = audioCtx.currentTime + at;
      const osc = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      osc.type = type;
      osc.frequency.setValueAtTime(freq, t0);
      if (slide) osc.frequency.exponentialRampToValueAtTime(Math.max(40, freq + slide), t0 + ms / 1000);
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(gain, t0 + 0.012);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + ms / 1000);
      osc.connect(g).connect(audioCtx.destination);
      osc.start(t0);
      osc.stop(t0 + ms / 1000 + 0.02);
    } catch (_) { /* no audio device - demos work silently */ }
  }

  /** The cue vocabulary the demos use. */
  const cues = {
    open: () => tone(520, 120, { type: "triangle", gain: 0.18 }),
    good: () => { tone(660, 120, { type: "triangle" }); tone(990, 170, { type: "triangle", at: 0.08 }); },
    bad: () => tone(200, 260, { type: "square", gain: 0.16, slide: -70 }),
    tick: () => tone(1250, 55, { type: "square", gain: 0.1 }),
    warn: () => tone(430, 200, { type: "sawtooth", gain: 0.14, slide: -60 }),
    alarm: () => { tone(880, 150, { type: "square", gain: 0.16 }); tone(880, 150, { type: "square", gain: 0.16, at: 0.2 }); },
  };

  /** The speaker toggle shown in every demo's header. */
  function muteButton() {
    const b = el("button", "demo__close demo__mute", muted ? "🔇" : "🔊");
    b.type = "button";
    const paint = () => {
      b.textContent = muted ? "🔇" : "🔊";
      b.setAttribute("aria-pressed", String(muted));
      b.setAttribute("aria-label", muted ? "Unmute demo sounds" : "Mute demo sounds");
    };
    paint();
    b.addEventListener("click", () => {
      muted = !muted;
      try { localStorage.setItem(MUTE_KEY, muted ? "1" : "0"); } catch (_) { /* ignore */ }
      paint();
      if (!muted) cues.open();
    });
    return b;
  }

  window.SFX = { ...cues, muteButton };
})();
