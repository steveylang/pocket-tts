# Tailscale Reader: design

## Goal

A private, single-user web app on this machine (`vnic1`) reachable only over Tailscale. From an
iPhone, paste text or a web link and listen to it read aloud with pocket-tts. Success: tap Play on
a long article and hear audio within a few seconds, keep listening with the screen locked, and
control playback from the lock screen.

## Decisions (agreed)

- **Access:** `tailscale serve` with HTTPS on its own port (8444). The existing :8443 proxy to
  :8765 is left alone. The app binds to `127.0.0.1` only; tailnet membership is the access control.
- **Code location:** separate `reader/` folder that imports `TTSModel`. No edits to `pocket_tts/`,
  so upstream pulls never conflict.
- **Web pages:** read the main article text only, extracted with `trafilatura`. The extractor sits
  behind one function so defuddle can be added later.
- **Playback:** chunked finished WAV files played back-to-back in an `<audio>` element (not a raw
  streaming WAV, which iOS Safari handles poorly and drops when the screen locks).
- **v1 features:** voice picker, speed control (0.75x to 2x), lock-screen controls (Media Session),
  systemd auto-start.
- **Out of scope for v1:** download button, queue/history, accounts, defuddle, language switching.

## Structure

```
reader/
  app.py        FastAPI app: loads TTSModel, endpoints, generation lock, job store
  extract.py    URL -> clean text (trafilatura), plain-text cleanup, URL safety check
  chunking.py   sentences -> ~400 char chunks (reuses split_into_best_sentences)
  static/       index.html, app.js, styles (the phone page)
  tests/
```

## Endpoints

- `GET /` the page.
- `GET /voices` built-in voice names from `_ORIGINS_OF_PREDEFINED_VOICES`.
- `POST /prepare` body: text or URL, voice. Returns `{job, title, chunks: N}`.
  Errors: empty or near-empty extraction (clear message), unsafe URL, fetch failure.
- `GET /chunk/{job}/{n}.wav` returns a finished WAV for chunk n. Generates on first request,
  caches it, and starts chunk n+1 in the background.
- `GET /health`.

## Data flow

1. Page sends text or URL to `/prepare`. A URL is detected by `http(s)://` prefix.
2. Server fetches and extracts (URL case), cleans the text, splits into chunks, stores chunk texts
   in memory under a random job ID (1 hour expiry).
3. Page requests chunk 1, plays it, and prefetches chunk 2. On `ended` it swaps to the next chunk,
   always staying about one chunk ahead.
4. Speed is applied on the audio element with pitch preserved and stored in `localStorage`.
5. Media Session sets the title and wires play, pause, and previous/next chunk.

## Server details

- **Chunking:** sentence-aligned, about 400 characters (roughly 25 s of audio), never splitting a
  sentence. One constant, tunable after listening.
- **Concurrency:** one generation at a time (model is not thread-safe). A request that cannot get
  the lock for a different job gets a clear "busy" response rather than corrupting a run.
- **Cache:** generated WAVs in a temp folder, removed on job expiry and on restart. No database.
- **URL safety (SSRF):** only `http`/`https`; resolve the host and refuse loopback, private,
  link-local, and tailnet (100.64.0.0/10) addresses, re-checking after redirects; fetch timeout and
  response size cap.
- **Running:** systemd user service runs `uvicorn` on `127.0.0.1:8770` with `--language english`
  and the default voice. `tailscale serve --https=8444` proxies to it.

## Error handling (user-visible)

- Extraction fails or yields almost no text: page says so, nothing is spoken.
- Very long article: part count shown up front; chunks generated on demand.
- Server busy: clear message in the page.

## Testing

- Unit: chunking (boundaries, long sentences, short text), text cleanup, URL-safety check.
- Endpoint: `/prepare` and `/chunk` with the model stubbed (fast, no weights needed).
- Manual iPhone checklist: plays on Safari over the ts.net HTTPS URL; keeps playing with the screen
  locked; lock-screen play/pause/next work; speed changes keep pitch; gap between chunks acceptable;
  works when added to the Home Screen.

## Known risks

- A small gap or tone shift between chunks is likely. Verify on the phone early, then tune chunk
  size.
- iOS lock-screen and background behavior can only be confirmed on the real device.
- JavaScript-heavy or paywalled pages will not extract; this is reported, not worked around.
