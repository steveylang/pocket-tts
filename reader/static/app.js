"use strict";

const $ = (id) => document.getElementById(id);
const audio = $("audio");

let job = null; // { id, title, chunks }
let index = 0;
// True from the tap on Play until the new reading is ready. While set, the unlock clip and the
// previous reading must not trigger "next part" or error handling.
let preparing = false;
let silentUrl = null;

function safeGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function safeSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* storage can be blocked; not essential */ }
}

function setStatus(text) {
  $("status").textContent = text;
}

// iOS only lets a page start audio from a tap, and the tap's permission is gone by the time the
// server has answered. Playing 0.1 s of silence inside the tap "unlocks" this audio element, so
// the real chunks can start later without another tap.
function silentWavUrl() {
  const rate = 8000;
  const samples = 800;
  const bytes = new Uint8Array(44 + samples * 2);
  const view = new DataView(bytes.buffer);
  const text = (offset, s) => [...s].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  text(0, "RIFF"); view.setUint32(4, 36 + samples * 2, true); text(8, "WAVE"); text(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  text(36, "data"); view.setUint32(40, samples * 2, true);
  return URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
}

function applySpeed() {
  const speed = parseFloat($("speed").value);
  $("speed-label").textContent = `${speed.toFixed(2)}x`;
  audio.defaultPlaybackRate = speed;
  audio.playbackRate = speed;
  audio.preservesPitch = true;
  audio.webkitPreservesPitch = true;
  safeSet("speed", String(speed));
}

function chunkUrl(n) {
  return `/chunk/${job.id}/${n}.wav`;
}

function playChunk(n) {
  if (!job || n < 0 || n >= job.chunks) return;
  index = n;
  audio.src = chunkUrl(n);
  $("retry").hidden = true;
  applySpeed();
  $("progress").textContent = `Part ${n + 1} of ${job.chunks}`;
  setStatus("");
  // The server generates the next part ahead by itself; asking for it here too would queue work
  // two parts ahead and slow down skipping.
  audio.play().catch(() => setStatus("Tap play to continue."));
}

function setupMediaSession() {
  if (!("mediaSession" in navigator)) return;
  navigator.mediaSession.metadata = new MediaMetadata({ title: job.title, artist: "Pocket Reader" });
  navigator.mediaSession.setActionHandler("play", () => audio.play());
  navigator.mediaSession.setActionHandler("pause", () => audio.pause());
  navigator.mediaSession.setActionHandler("previoustrack", () => playChunk(index - 1));
  navigator.mediaSession.setActionHandler("nexttrack", () => playChunk(index + 1));
}

async function start() {
  const input = $("input").value.trim();
  if (!input) {
    setStatus("Paste some text or a link first.");
    return;
  }
  preparing = true;
  job = null;
  index = 0;
  $("player").hidden = true;
  $("retry").hidden = true;
  silentUrl = silentUrl || silentWavUrl();
  audio.src = silentUrl;
  audio.play().catch(() => {});
  setStatus("Preparing…");
  $("play").disabled = true;
  try {
    const response = await fetch("/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input, voice: $("voice").value }),
    });
    // An unexpected server error may not be JSON; show its status text instead of a wrong message.
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      setStatus(body.detail || response.statusText || "Something went wrong.");
      return;
    }
    job = { id: body.job, title: body.title, chunks: body.chunks };
    $("title").textContent = job.title;
    $("player").hidden = false;
    setupMediaSession();
    setStatus("Generating the first part…");
    playChunk(0);
  } catch {
    setStatus("Could not reach the server.");
  } finally {
    preparing = false;
    $("play").disabled = false;
  }
}

async function loadVoices() {
  const response = await fetch("/voices");
  const { voices, default: fallback } = await response.json();
  for (const name of voices) {
    const option = document.createElement("option");
    option.value = option.textContent = name;
    $("voice").append(option);
  }
  const saved = safeGet("voice");
  $("voice").value = voices.includes(saved) ? saved : fallback;
}

$("play").addEventListener("click", start);
$("prev").addEventListener("click", () => playChunk(index - 1));
$("next").addEventListener("click", () => playChunk(index + 1));
$("voice").addEventListener("change", () => safeSet("voice", $("voice").value));
$("speed").addEventListener("input", applySpeed);
audio.addEventListener("ended", () => {
  if (preparing || !job) return;
  if (index + 1 < job.chunks) playChunk(index + 1);
  else setStatus("Finished.");
});
audio.addEventListener("loadedmetadata", applySpeed);
audio.addEventListener("error", () => {
  if (preparing || !job) return;
  setStatus("Could not load this part.");
  $("retry").hidden = false;
});
$("retry").addEventListener("click", () => playChunk(index));

const savedSpeed = parseFloat(safeGet("speed"));
if (savedSpeed >= 0.75 && savedSpeed <= 2) $("speed").value = String(savedSpeed);
applySpeed();
loadVoices().catch(() => setStatus("Could not load the voice list."));
