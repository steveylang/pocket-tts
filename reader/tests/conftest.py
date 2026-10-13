from collections.abc import Iterator

import pytest
import torch
from fastapi.testclient import TestClient

from reader.app import create_app


class FakeModel:
    """Stand-in for TTSModel: 0.1 s of silence per chunk, recording what it was asked for."""

    sample_rate = 24000

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.voices: list[str] = []
        self.fail_next = False

    def _cached_get_state_for_audio_prompt(self, voice: str) -> dict[str, str]:
        self.voices.append(voice)
        return {"voice": voice}

    def generate_audio(self, model_state: dict[str, str], text_to_generate: str) -> torch.Tensor:
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
