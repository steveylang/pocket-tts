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

// FastAPI sends `detail` as a list for malformed requests; only a string is fit to show.
function errorText(body, response) {
  if (body && typeof body.detail === "string") return body.detail;
  return response.statusText || "Something went wrong.";
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

function fileForm(file) {
  const form = new FormData();
  form.append("file", file);
  form.append("voice", $("voice").value);
  return form;
}

async function start() {
  const file = $("file").files[0];
  const input = $("input").value.trim();
  if (!file && !input) {
    setStatus("Paste some text or a link, or choose a document first.");
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
    const response = file
      ? await fetch("/prepare-file", { method: "POST", body: fileForm(file) })
      : await fetch("/prepare", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ input, voice: $("voice").value }),
        });
    // An unexpected server error may not be JSON; show its status text instead of a wrong message.
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      setStatus(errorText(body, response));
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

function updateVoiceButtons() {
  const option = $("voice").selectedOptions[0];
  $("voice-delete").disabled = !(option && option.dataset.custom);
}

// `selectId` picks a voice after adding one; otherwise the last-used voice is restored.
async function loadVoices(selectId) {
  const response = await fetch("/voices");
  const { voices, custom, default: fallback } = await response.json();
  const select = $("voice");
  select.replaceChildren();
  for (const name of voices) select.append(new Option(name, name));
  if (custom.length) {
    const group = document.createElement("optgroup");
    group.label = "Uploaded";
    for (const { id, name } of custom) {
      const option = new Option(name, id);
      option.dataset.custom = "1";
      group.append(option);
    }
    select.append(group);
  }
  // A saved voice that was deleted is simply not in the list any more, so fall back.
  const wanted = selectId || safeGet("voice");
  select.value = [...select.options].some((o) => o.value === wanted) ? wanted : fallback;
  updateVoiceButtons();
}

async function addVoice() {
  const file = $("voice-file").files[0];
  const name = $("voice-name").value.trim();
  if (!file || !name) {
    setStatus("Choose a voice file and give it a name.");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  form.append("name", name);
  $("voice-add").disabled = true;
  setStatus("Preparing voice…");
  try {
    const response = await fetch("/voices", { method: "POST", body: form });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      setStatus(errorText(body, response));
      return;
    }
    safeSet("voice", body.id);
    await loadVoices(body.id);
    $("voice-file").value = "";
    $("voice-name").value = "";
    setStatus(`Added ${body.name}.`);
  } catch {
    setStatus("Could not reach the server.");
  } finally {
    $("voice-add").disabled = false;
  }
}

async function deleteVoice() {
  const option = $("voice").selectedOptions[0];
  if (!option || !option.dataset.custom) return;
  if (!confirm(`Delete the voice "${option.textContent}"?`)) return;
  try {
    const response = await fetch(`/voices/${encodeURIComponent(option.value)}`, {
      method: "DELETE",
    });
    // 404 means it was already gone; the refreshed list is the truth either way.
    if (!response.ok && response.status !== 404) {
      setStatus("Could not delete that voice.");
      return;
    }
    await loadVoices();
    setStatus("Voice deleted.");
  } catch {
    setStatus("Could not reach the server.");
  }
}

$("play").addEventListener("click", start);
// A document and pasted text are alternatives: choosing one clears the other.
$("file").addEventListener("change", () => {
  if ($("file").files[0]) $("input").value = "";
});
$("input").addEventListener("input", () => { $("file").value = ""; });
$("prev").addEventListener("click", () => playChunk(index - 1));
$("next").addEventListener("click", () => playChunk(index + 1));
$("voice").addEventListener("change", () => {
  safeSet("voice", $("voice").value);
  updateVoiceButtons();
});
$("voice-add").addEventListener("click", addVoice);
$("voice-delete").addEventListener("click", deleteVoice);
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
