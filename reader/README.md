# Pocket Reader

A private page for listening to text or web articles on your phone, served over Tailscale only.
Design: `docs/superpowers/specs/2026-10-13-tailscale-reader-design.md`.

## Run it by hand

```bash
uv run --no-dev --group reader uvicorn reader.app:create_app --factory --host 127.0.0.1 --port 8770
```

The first start downloads the model weights, so it can take a while.

The service runs with `uv run --frozen` so the new upload dependencies (`pypdf`, `soundfile`) come
from the updated `uv.lock` at the next service restart.

## Run the tests

```bash
uv run --no-dev --group reader pytest reader/tests
```

`--no-dev` skips the dev group, whose `sphn` package needs a Rust build and fails on some
machines (it did on this aarch64 VM). The reader group carries its own test dependencies.

## Install as a service (this machine)

```bash
mkdir -p ~/.config/systemd/user
cp reader/pocket-tts-reader.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pocket-tts-reader
journalctl --user -u pocket-tts-reader -f      # wait for "Application startup complete"
curl -s http://127.0.0.1:8770/health
```

Linger is already enabled for this user, so the service starts at boot.

## Expose it on the tailnet

```bash
tailscale serve --bg --https=8444 http://127.0.0.1:8770   # may need sudo
tailscale serve status                                       # :8443 -> 8765 must still be listed
```

Then open `https://vnic1.tail6fc1ab.ts.net:8444` on the iPhone. Share > Add to Home Screen gives it
an app icon.

## Notes

- The server only listens on 127.0.0.1; Tailscale Serve is the only way in.
- One generation runs at a time. A new reading drops the not-yet-started parts of the old one.
- Pages that need JavaScript or a login cannot be read; the page says so.
- Documents: choose a `.txt`, `.md`, `.html` or `.pdf` (up to 20 MB). Markdown front matter is
  skipped. Scanned PDFs have no text and are refused (there is no OCR); multi-column PDFs and
  running headers or footers may read awkwardly.
- Custom voices are stored in `voices/` in the repo folder (ignored by git), or in the folder named
  by `READER_VOICES_DIR`. Add a clean 5 to 30 second `.wav`, `.mp3`, `.flac` or `.ogg` clip, or a
  `.safetensors` made with `pocket-tts export-voice` on the Mac (use the same model version as this
  server). iPhone Voice Memos are `.m4a`, which is not supported: convert to `.wav` first.
- Audio clips need the voice-cloning weights, which the default model here does not have (they
  need the terms accepted on Hugging Face and a login). Without them an audio upload returns 400
  "This model cannot make a voice from audio. Upload a .safetensors voice." A `.safetensors` voice
  always works. Verified against the real model: a `.safetensors` exported from the built-in alba
  voice uploads, reads aloud and deletes cleanly. Not verified: audio uploads (no cloning
  weights), a real Mac export, and states from a different model version.
- At most 20 custom voices. Deleting one mid-reading makes the remaining parts of that reading fail.
- Voice files from the Mimika Mac app (`<UUID>_kv.safetensors`) are converted on upload to this
  model's layout. They only sound right with the model they were made for, the original
  `english_2026-01`. Verified with a real Mimika file: on `english_2026-01` it reads normally; on
  `english_2026-04` and the current default `english_2026-09_2` the model never reaches the end of
  speech and the audio is near silent. A voice state from any model version other than the one
  the server runs is accepted at upload and only fails when it is used.
