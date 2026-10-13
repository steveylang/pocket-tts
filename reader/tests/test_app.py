import io
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import torch
from fastapi.testclient import TestClient

from reader import app as reader_app
from reader.app import ReaderService, write_wav
from reader.extract import UnsafeURL
from reader.tests.conftest import FakeModel
from reader.tests.samples import ARTICLE_HTML

LONG_TEXT = " ".join(f"This is sentence number {i} of the test article." for i in range(60))


def prepare(client: TestClient, text: str = LONG_TEXT, voice: str = "alba") -> httpx.Response:
    return client.post("/prepare", json={"input": text, "voice": voice})


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def test_health(client: TestClient):
    assert client.get("/health").json() == {"status": "healthy"}


def test_voices_lists_the_builtin_voices(client: TestClient):
    body = client.get("/voices").json()
    assert body["default"] == "alba"
    assert "alba" in body["voices"]


def test_prepare_text_returns_a_job_and_the_chunk_count(client: TestClient):
    response = prepare(client)
    assert response.status_code == 200
    body = response.json()
    assert body["chunks"] > 1
    assert body["job"]
    assert body["title"].startswith("This is sentence number 0")


def test_chunk_is_a_wav_and_the_next_chunk_is_generated_ahead(client: TestClient, model: FakeModel):
    job = prepare(client).json()["job"]
    response = client.get(f"/chunk/{job}/0.wav")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(response.content)) as wav:
        assert wav.getframerate() == 24000
        assert wav.getnframes() == 2400
    wait_until(lambda: len(model.texts) == 2)
    assert model.texts[0] != model.texts[1]


def test_the_selected_voice_is_used(client: TestClient, model: FakeModel):
    job = prepare(client, voice="marius").json()["job"]
    client.get(f"/chunk/{job}/0.wav")
    assert model.voices[0] == "marius"


def test_range_requests_get_partial_content_for_ios(client: TestClient):
    job = prepare(client).json()["job"]
    response = client.get(f"/chunk/{job}/0.wav", headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert response.headers["content-range"].startswith("bytes 0-99/")
    assert len(response.content) == 100


def test_unknown_job_and_out_of_range_chunk_are_404(client: TestClient):
    job = prepare(client).json()["job"]
    assert client.get("/chunk/nope/0.wav").status_code == 404
    assert client.get(f"/chunk/{job}/9999.wav").status_code == 404


def test_expired_jobs_are_gone(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    first = prepare(client).json()["job"]
    monkeypatch.setattr(reader_app, "JOB_TTL_SECONDS", 0)
    prepare(client)
    assert client.get(f"/chunk/{first}/0.wav").status_code == 404


def test_a_failed_generation_is_a_500_and_can_be_retried(client: TestClient, model: FakeModel):
    job = prepare(client).json()["job"]
    model.fail_next = True
    assert client.get(f"/chunk/{job}/0.wav").status_code == 500
    assert client.get(f"/chunk/{job}/0.wav").status_code == 200


def test_unknown_voice_is_rejected(client: TestClient):
    response = prepare(client, voice="nobody")
    assert response.status_code == 400
    assert "nobody" in response.json()["detail"]


@pytest.mark.parametrize("text", ["", "   ", "🙂 --- ***"])
def test_text_with_nothing_readable_is_rejected(client: TestClient, text: str):
    response = prepare(client, text)
    assert response.status_code == 400
    assert "nothing readable" in response.json()["detail"].lower()


def test_a_url_is_fetched_and_its_article_is_read(
    client: TestClient, model: FakeModel, monkeypatch: pytest.MonkeyPatch
):
    fetched: list[str] = []

    def fake_fetch(url: str) -> bytes:
        fetched.append(url)
        return ARTICLE_HTML

    monkeypatch.setattr(reader_app, "fetch_page", fake_fetch)
    body = prepare(client, "  https://example.com/post  ").json()
    assert fetched == ["https://example.com/post"]
    assert body["title"] == "Notes on Growing Tomatoes"
    job = body["job"]
    client.get(f"/chunk/{job}/0.wav")
    assert "Tomatoes need at least six hours" in model.texts[0]
    assert "Log in" not in model.texts[0]


def test_an_unsafe_url_is_a_400_with_the_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    def refuse(url: str) -> bytes:
        raise UnsafeURL("That link points to a private or internal address, so it was not read.")

    monkeypatch.setattr(reader_app, "fetch_page", refuse)
    response = prepare(client, "http://127.0.0.1/")
    assert response.status_code == 400
    assert "private or internal" in response.json()["detail"]


def test_text_that_merely_mentions_a_link_is_read_as_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    def fail(url: str) -> bytes:
        raise AssertionError("must not fetch")

    monkeypatch.setattr(reader_app, "fetch_page", fail)
    assert prepare(client, "Please read https://example.com/x today.").status_code == 200


def test_write_wav_roundtrip(tmp_path: Path):
    path = tmp_path / "out.wav"
    write_wav(path, torch.tensor([0.0, 0.5, -0.5, 2.0]), 16000)
    with wave.open(str(path)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        frames = wav.readframes(4)
    samples = [int.from_bytes(frames[i : i + 2], "little", signed=True) for i in range(0, 8, 2)]
    assert samples == [0, 16383, -16383, 32767]  # 2.0 is clipped to full scale


def test_a_malformed_link_is_a_400_not_a_crash(client: TestClient):
    response = prepare(client, "http://example.com:99999/")
    assert response.status_code == 400
    assert "not valid" in response.json()["detail"]


def test_skipping_ahead_drops_the_old_pending_parts(tmp_path: Path):
    release = threading.Event()

    class BlockingModel(FakeModel):
        def generate_audio(
            self, model_state: dict[str, str], text_to_generate: str
        ) -> torch.Tensor:
            release.wait(5)
            return super().generate_audio(model_state, text_to_generate)

    service = ReaderService(BlockingModel(), tmp_path)
    try:
        job_id = service.add_job("t", "alba", [f"Part {i}." for i in range(10)])
        service.chunk_future(job_id, 0)
        futures = service.jobs[job_id].futures
        wait_until(lambda: futures[0].running())
        service.chunk_future(job_id, 5)
        # Part 0 is already running and cannot stop; the never-started part 1 must be dropped.
        assert set(futures) == {0, 5, 6}
    finally:
        release.set()
        service.shutdown()
