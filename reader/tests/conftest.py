from collections.abc import Iterator
from pathlib import Path

import pytest
import torch
from fastapi.testclient import TestClient

from pocket_tts.models.model_state import _import_model_state
from reader.app import create_app


class FakeModel:
    """Stand-in for TTSModel: 0.1 s of silence per chunk, recording what it was asked for."""

    sample_rate = 24000
    has_voice_cloning = True

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.voices: list[str | Path] = []
        self.prompts: list[tuple[str, bool]] = []
        self.fail_next = False

    def _cached_get_state_for_audio_prompt(self, voice: str | Path) -> dict[str, str | Path]:
        self.voices.append(voice)
        return {"voice": voice}

    def get_state_for_audio_prompt(
        self, audio_conditioning: Path, truncate: bool = False
    ) -> dict[str, dict[str, torch.Tensor]]:
        self.prompts.append((audio_conditioning.name, truncate))
        if audio_conditioning.suffix == ".safetensors":
            # The real model reads the file; a damaged one raises, which is what we rely on.
            return _import_model_state(audio_conditioning, torch.device("cpu"))
        return {"fake": {"voice": torch.zeros(1)}}

    def generate_audio(self, model_state: dict[str, str | Path], text_to_generate: str) -> torch.Tensor:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("boom")
        self.texts.append(text_to_generate)
        return torch.zeros(2400)


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def client(model: FakeModel) -> Iterator[TestClient]:
    with TestClient(create_app(model)) as test_client:
        yield test_client
