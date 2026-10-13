# Pocket Reader

A private page for listening to text or web articles on your phone, served over Tailscale only.
Design: `docs/superpowers/specs/2026-10-13-tailscale-reader-design.md`.

## Run it by hand

```bash
uv run --no-dev --group reader uvicorn reader.app:create_app --factory --host 127.0.0.1 --port 8770
```

The first start downloads the model weights, so it can take a while.

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
