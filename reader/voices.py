"""Custom voices: uploaded audio or exported voice states, kept on disk across restarts."""

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAX_VOICES = 20
MAX_NAME_CHARS = 40
# token_urlsafe output only. Checked before an id is ever turned into a path.
_ID = re.compile(r"[A-Za-z0-9_-]+")


class VoiceError(Exception):
    """Something the user can fix; the message is shown on the page."""


@dataclass(frozen=True)
class Voice:
    id: str
    name: str


class VoiceStore:
    """Voices live in `<directory>/<id>.safetensors` (the state) and `<id>.json` (the name).

    Each upload gets a fresh random id, so a deleted voice's name can be reused without the
    model's voice cache ever returning the old one.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def prepare_name(self, name: str) -> str:
        """Clean a name and check there is room, so a bad request fails before any work is done."""
        name = " ".join(name.split())
        if not name:
            raise VoiceError("Give the voice a name.")
        if len(name) > MAX_NAME_CHARS:
            raise VoiceError(f"A voice name can have at most {MAX_NAME_CHARS} characters.")
        if len(self.list_voices()) >= MAX_VOICES:
            raise VoiceError(f"There are already {MAX_VOICES} uploaded voices. Delete one first.")
        return name

    def add(self, name: str, make_state: Callable[[Path], None]) -> Voice:
        """Create a voice; `make_state(path)` must write the voice state file at `path`."""
        name = self.prepare_name(name)
        self.directory.mkdir(parents=True, exist_ok=True)
        voice_id = secrets.token_urlsafe(8)
        partial = self.directory / f"{voice_id}.part"
        try:
            make_state(partial)
            partial.replace(self._state(voice_id))
        finally:
            partial.unlink(missing_ok=True)
        # The name file is written last: a voice only exists once both files are there.
        self._meta(voice_id).write_text(json.dumps({"name": name}), encoding="utf-8")
        return Voice(voice_id, name)

    def list_voices(self) -> list[Voice]:
        found = []
        for meta in self.directory.glob("*.json"):
            if not _ID.fullmatch(meta.stem) or not self._state(meta.stem).exists():
                continue
            try:
                name = json.loads(meta.read_text(encoding="utf-8"))["name"]
            except (OSError, ValueError, KeyError, TypeError):
                continue  # a damaged name file hides that one voice, not the whole list
            found.append(Voice(meta.stem, str(name)))
        return sorted(found, key=lambda voice: voice.name.casefold())

    def state_path(self, voice_id: str) -> Path | None:
        if not _ID.fullmatch(voice_id):
            return None
        state = self._state(voice_id)
        return state if state.exists() and self._meta(voice_id).exists() else None

    def delete(self, voice_id: str) -> bool:
        if self.state_path(voice_id) is None:
            return False
        self._meta(voice_id).unlink(missing_ok=True)
        self._state(voice_id).unlink(missing_ok=True)
        return True

    def _state(self, voice_id: str) -> Path:
        return self.directory / f"{voice_id}.safetensors"

    def _meta(self, voice_id: str) -> Path:
        return self.directory / f"{voice_id}.json"
