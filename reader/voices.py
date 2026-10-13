"""Custom voices: uploaded audio or exported voice states, kept on disk across restarts."""

import json
import logging
import re
import secrets
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import torch
from safetensors import safe_open
from safetensors.torch import save_file

from pocket_tts.models.model_state import export_model_state

logger = logging.getLogger(__name__)

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


STATE_SUFFIX = ".safetensors"
AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".flac", ".ogg"})
UPLOAD_SUFFIXES = AUDIO_SUFFIXES | {STATE_SUFFIX}


class VoiceEncoder(Protocol):
    """The part of TTSModel that turns an upload into a voice state."""

    has_voice_cloning: bool

    def get_state_for_audio_prompt(
        self, audio_conditioning: Path, truncate: bool = False
    ) -> dict[str, dict[str, torch.Tensor]]: ...


_MIMIKA_FIRST_KEY = "kv_k_0"
_NOT_A_VOICE = (
    "That file is not a voice this reader can use. "
    "It must come from `pocket-tts export-voice` or from Mimika."
)


def is_mimika_state(path: Path) -> bool:
    """Whether a voice state uses the layout the Mimika Mac app writes (`kv_k_N` / `kv_v_N`)."""
    try:
        with safe_open(path, framework="pt") as source:
            return _MIMIKA_FIRST_KEY in source.keys()
    except Exception:
        return False  # not a readable safetensors file at all; the normal path reports that


def convert_mimika_state(source: Path, dest: Path) -> None:
    """Rewrite a Mimika voice file in the layout this model loads (`pocket-tts export-voice`).

    Mimika keeps each layer's keys and values in separate fp16 arrays padded to a fixed length,
    with the number of valid positions in the file's metadata. The model wants one fp32 array per
    layer with keys at index 0 and values at index 1, cut to the valid positions, plus the length.
    """
    try:
        with safe_open(source, framework="pt") as voice:
            info = json.loads((voice.metadata() or {})["info"])
            t_voice = info["T_voice"]
            if not isinstance(t_voice, int) or isinstance(t_voice, bool) or t_voice < 1:
                raise ValueError(f"bad T_voice {t_voice!r}")
            layers = sum(1 for key in voice.keys() if key.startswith("kv_k_"))
            if layers == 0:
                raise ValueError("no layers")
            tensors: dict[str, torch.Tensor] = {}
            for layer in range(layers):
                keys = voice.get_tensor(f"kv_k_{layer}")
                values = voice.get_tensor(f"kv_v_{layer}")
                if keys.ndim != 4 or keys.shape != values.shape or t_voice > keys.shape[1]:
                    raise ValueError(f"bad shape in layer {layer}")
                cache = torch.stack([keys[:, :t_voice], values[:, :t_voice]])
                prefix = f"transformer.layers.{layer}.self_attn"
                tensors[f"{prefix}/cache"] = cache.to(torch.float32).contiguous()
                tensors[f"{prefix}/offset"] = torch.tensor([t_voice], dtype=torch.long)
                tensors[f"{prefix}/pad"] = torch.tensor([0], dtype=torch.long)
        save_file(tensors, dest)
    except Exception as error:
        raise VoiceError("That Mimika voice file could not be converted.") from error


def encode_voice(model: VoiceEncoder, upload: Path, dest: Path) -> None:
    """Write a voice state file at `dest` from an uploaded clip or exported voice state.

    Must run on the model worker thread. `upload` keeps its (lower-case) extension, which the
    model uses to tell a voice state from audio.
    """
    if upload.suffix == STATE_SUFFIX:
        # Next to the upload; needs the .safetensors suffix for the model to load it, which
        # `dest` (a .part file) does not have.
        converted = upload.with_name("converted" + STATE_SUFFIX)
        try:
            state_file = upload
            if is_mimika_state(upload):
                convert_mimika_state(upload, converted)
                state_file = converted
            try:
                # Loading is the validation. A state from a different model can still load; that
                # shows up as an error when the voice is first used.
                state = model.get_state_for_audio_prompt(state_file)
                if not state:
                    raise ValueError("empty voice state")
            except Exception as error:
                raise VoiceError(_NOT_A_VOICE) from error
            shutil.copyfile(state_file, dest)
        finally:
            converted.unlink(missing_ok=True)
        return
    if not model.has_voice_cloning:
        raise VoiceError("This model cannot make a voice from audio. Upload a .safetensors voice.")
    try:
        # Only the first 30 seconds are used, as `pocket-tts export-voice` does.
        state = model.get_state_for_audio_prompt(upload, truncate=True)
        export_model_state(state, dest)
    except Exception as error:
        # The page only says it could not be read; the reason is for whoever reads the log.
        logger.warning("Could not read the audio upload %s", upload.name, exc_info=True)
        raise VoiceError("Could not read that audio file.") from error
