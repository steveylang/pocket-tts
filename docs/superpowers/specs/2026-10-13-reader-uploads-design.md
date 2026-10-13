# Reader uploads: documents and custom voices

Date: 2026-10-13. Builds on `2026-10-13-tailscale-reader-design.md`.

## Goal

From the phone page, (1) read an uploaded document aloud, and (2) add, use and delete custom voices,
made from audio clips or from `.safetensors` voices exported on a Mac. Single user on a tailnet.

## Build order

Documents first (smaller, no model involvement), then voices. Each is test-first with the existing
fake model. The two parts share no code except the upload size handling.

## Part 1: documents

**Endpoint.** `POST /prepare-file`, multipart: `file`, `voice` (default `alba`). Returns the same
`{job, title, chunks}` as `/prepare`. After extraction the path is identical to `/prepare`:
`chunk_text`, `add_job`, then `/chunk/...` as today.

**Extraction** (new `reader/documents.py`, pure functions, no FastAPI):

| Extension | Method |
|-----------|--------|
| `.txt`, `.md` | decode UTF-8 (fall back to latin-1), `clean_text` |
| `.html`, `.htm` | `extract_article` (existing) |
| `.pdf` | `pypdf` page text joined with blank lines, `clean_text` |

Anything else: 400 "That file type can't be read. Use .txt, .md, .html or .pdf."

**Title.** HTML: the article title. Others: first paragraph truncated as `_title_from` does today.

**Limits and errors** (all 400, message shown on the page):
- Size cap 20 MB, checked by reading at most the cap plus one byte (the framework has already spooled the upload to disk; this bounds what is loaded and processed).
- PDF with no extractable text: "This PDF has no text, it may be scanned images. There is no OCR."
- Encrypted PDF: "This PDF is password protected."
- Corrupt file: "Could not read that file."
- Empty result after cleaning: existing "There is nothing readable in that text."

**Dependency.** `pypdf` added to the `reader` group in `pyproject.toml`. `python-multipart` is already a main dependency, so nothing to add for uploads.

**Extraction details.** `.md` also drops a leading `---` front matter block; `.pdf` rejoins words hyphenated across line breaks before a lower-case letter.

**Known quality limits (documented in README, not fixed):** multi-column PDFs and running
headers/footers can read awkwardly; no OCR.

## Part 2: custom voices

**Storage.** `voices/` under the repo working directory (path overridable by `READER_VOICES_DIR`
for tests), gitignored. Per voice: `<id>.safetensors` and `<id>.json` (`{"name": "..."}`).
`<id>` is a random token (`secrets.token_urlsafe(8)`), never the name.

**Why unique ids.** `_cached_get_state_for_audio_prompt` is an `lru_cache(maxsize=2)` keyed on its
argument. A reused name after delete would return the old voice. The voice key passed to the model is
the `Path` of the id-named file, which is unique per upload. Delete also calls `cache_clear()` on that
method so deleted voices are not held in memory.

**Endpoints.**
- `GET /voices`: `{"voices": [built-in names], "custom": [{"id","name"}], "default": "alba"}`.
  Existing keys unchanged, so the current page and tests keep working.
- `POST /voices`, multipart: `file`, `name` (1 to 40 chars after trim). Returns `{"id","name"}`.
  - `.safetensors`: saved to a temp file, validated by loading it as a voice state on the worker (a state from a different model version can still load and may only fail when first used),
    then moved into place. A load failure returns 400 "That file is not a voice this reader can use
    (it must be exported from the same model)."
  - Audio (`.wav`, `.mp3`, `.flac`, `.ogg`; not `.m4a`, which libsndfile cannot read, and `soundfile` is added to the `reader` dependency group): decoded and encoded on the worker with
    `get_state_for_audio_prompt(path, truncate=True)` (first 30 seconds), then saved with
    `export_model_state`. The audio itself is not kept.
  - If the model reports no voice-cloning support, audio uploads return 400 with that reason;
    `.safetensors` uploads still work.
- `DELETE /voices/{id}`: removes both files, clears the cache, 204. Unknown id: 404.
- `/prepare` and `/prepare-file` accept `voice` as either a built-in name or a custom id. A custom id
  resolves to its file path; unknown values are still a 400.

**Concurrency.** Conversion and validation run on the existing single-worker executor, so they queue
behind generation instead of racing the non-thread-safe model. The upload request waits for its
result (timeout reuses the chunk wait of 120 s, then 503 "busy").

**Limits.** 25 MB upload cap. At most 20 custom voices; the 21st returns 400 asking to delete one.

**Jobs and delete.** A job stores the resolved voice key when created. Deleting a voice mid-reading
makes later chunks fail with the existing "Could not generate that part" 500; acceptable for one user.

## Page changes

- A "Choose file" button next to the text box, accepting `.txt,.md,.html,.htm,.pdf`. After picking,
  choosing a file clears the paste box, the browser's file control shows the name, and Play sends it to `/prepare-file`.
  Choosing a file clears pasted text and vice versa.
- Voice dropdown gets an "Uploaded" group listing custom voices.
- An "Add voice" section: file input with NO `accept` filter (so iOS does not grey out `.safetensors`; the server validates the type), name field, Add button. Status line
  shows "Preparing voice..." then "Added <name>".
- A "Delete voice" button, enabled only when a custom voice is selected, with a confirm prompt.
- Last-used voice persists in localStorage as today; a stale custom id falls back to the default.

## Testing

- `documents.py`: unit tests per type, including scanned-PDF, encrypted, corrupt, bad encoding.
  PDFs are generated in the test with a small writer so no binary fixtures are committed.
- App tests with the fake model: `/prepare-file` per type, size cap, bad type; voices upload
  (safetensors and audio), list, use in `/prepare`, delete, name validation, count cap, same-name
  re-upload after delete yields a different id.
- Manual, against the real model: upload a real clip and a Mac-exported `.safetensors`, confirm both
  read aloud in the new voice; upload an incompatible `.safetensors` and confirm the error.

**Real-model findings (Task 8, 2026-10-13).** The default model on this server has no voice-cloning
weights (they need the Hugging Face terms accepted and a login). An audio upload returns 400 "This
model cannot make a voice from audio. Upload a .safetensors voice."; `pocket-tts export-voice` on an
audio file fails for the same reason. A `.safetensors` exported from the built-in alba voice
uploaded, read aloud (valid non-silent WAV) and deleted correctly, and malformed files returned 400
with no 500s and no stray files. Not verified: audio cloning, a real Mac export, a state from a
different model version, and the empty-audio path on a cloning model.

## Out of scope

OCR, rename of voices, uploading several documents at once, a library of past documents, any
authentication (the tailnet is the boundary, as before).
