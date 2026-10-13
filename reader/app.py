"""Phone-friendly reader: paste text or a link, hear it read aloud one chunk at a time."""

import logging
import secrets
import shutil
import tempfile
import threading
import time
import wave
from collections.abc import AsyncIterator
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pocket_tts import TTSModel
from pocket_tts.utils.utils import _ORIGINS_OF_PREDEFINED_VOICES
from reader.chunking import chunk_text
from reader.extract import (
    Article,
    ExtractionError,
    clean_text,
    extract_article,
    fetch_page,
    title_from,
)

DEFAULT_VOICE = "alba"
JOB_TTL_SECONDS = 3600
# How long a chunk request waits for the (single) generator before saying "busy".
CHUNK_WAIT_SECONDS = 120
STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)


class SpeechModel(Protocol):
    """The part of TTSModel the reader uses (tests substitute a fake)."""

    @property
    def sample_rate(self) -> int: ...

    def _cached_get_state_for_audio_prompt(self, voice: str) -> object: ...

    def generate_audio(self, model_state: object, text_to_generate: str) -> torch.Tensor: ...


def write_wav(path: Path, audio: torch.Tensor, sample_rate: int) -> None:
    """Write mono 16-bit PCM atomically, so a half-written file is never served."""
    samples = audio.detach().cpu().float().reshape(-1).clamp(-1.0, 1.0).numpy()
    pcm = (samples * 32767).astype(np.int16)
    partial = path.with_suffix(".part")
    with wave.open(str(partial), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm.tobytes())
    partial.replace(path)


@dataclass
class Job:
    title: str
    voice: str
    chunks: list[str]
    created: float = field(default_factory=time.monotonic)
    futures: dict[int, "Future[Path]"] = field(default_factory=dict)


class ReaderService:
    """Holds the jobs and generates chunks, one at a time."""

    def __init__(self, model: SpeechModel, cache_dir: Path) -> None:
        self.model = model
        self.cache_dir = cache_dir
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()  # guards self.jobs and every job.futures
        # One worker: the model is not thread-safe, so generations queue up behind each other.
        self._executor = ThreadPoolExecutor(max_workers=1)

    def add_job(self, title: str, voice: str, chunks: list[str]) -> str:
        job_id = secrets.token_urlsafe(8)
        with self._lock:
            self._drop_expired()
            # Newest request wins: chunks of earlier readings that have not started are dropped,
            # so the new reading does not wait behind work nobody is listening to.
            for other in self.jobs.values():
                for n, future in list(other.futures.items()):
                    if future.cancel():
                        del other.futures[n]
            self.jobs[job_id] = Job(title, voice, chunks)
        return job_id

    def chunk_future(self, job_id: str, n: int) -> "Future[Path]":
        """Start (or find) generation of chunk n, and queue chunk n+1 behind it."""
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or not 0 <= n < len(job.chunks):
                raise KeyError((job_id, n))
            # Parts queued earlier but not started (the listener skipped past them) would delay
            # this one for nothing, so drop them.
            for other_n, other in list(job.futures.items()):
                if other_n not in (n, n + 1) and other.cancel():
                    del job.futures[other_n]
            future = self._submit(job_id, job, n)
            if n + 1 < len(job.chunks):
                self._submit(job_id, job, n + 1)
            return future

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _submit(self, job_id: str, job: Job, n: int) -> "Future[Path]":
        future = job.futures.get(n)
        if future is None:
            future = self._executor.submit(self._generate, job_id, job, n)
            job.futures[n] = future
        return future

    def _generate(self, job_id: str, job: Job, n: int) -> Path:
        path = self.cache_dir / job_id / f"{n}.wav"
        if path.exists():
            return path
        try:
            # generate_audio copies the voice state by default, so the cached state stays clean.
            state = self.model._cached_get_state_for_audio_prompt(job.voice)
            audio = self.model.generate_audio(state, job.chunks[n])
            path.parent.mkdir(parents=True, exist_ok=True)
            write_wav(path, audio, self.model.sample_rate)
        except Exception:
            with self._lock:
                job.futures.pop(n, None)  # forget the failure so a retry regenerates it
            raise
        return path

    def _drop_expired(self) -> None:
        """Remove old jobs and their files. Caller holds the lock."""
        now = time.monotonic()
        for job_id, job in list(self.jobs.items()):
            if now - job.created >= JOB_TTL_SECONDS:
                del self.jobs[job_id]
                shutil.rmtree(self.cache_dir / job_id, ignore_errors=True)


class PrepareRequest(BaseModel):
    input: str
    voice: str = DEFAULT_VOICE


def _looks_like_a_link(text: str) -> bool:
    return text.startswith(("http://", "https://")) and not any(c.isspace() for c in text)


def create_app(model: SpeechModel | None = None) -> FastAPI:
    """App factory. Run with: uvicorn reader.app:create_app --factory"""
    cache_dir = Path(tempfile.mkdtemp(prefix="pocket-tts-reader-"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loaded = model if model is not None else TTSModel.load_model()
        app.state.service = ReaderService(loaded, cache_dir)
        yield
        app.state.service.shutdown()
        shutil.rmtree(cache_dir, ignore_errors=True)

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/voices")
    def voices() -> dict[str, object]:
        return {"voices": list(_ORIGINS_OF_PREDEFINED_VOICES), "default": DEFAULT_VOICE}

    @app.post("/prepare")
    def prepare(request: PrepareRequest) -> dict[str, object]:
        if request.voice not in _ORIGINS_OF_PREDEFINED_VOICES:
            raise HTTPException(status_code=400, detail=f"Unknown voice: {request.voice}")
        text = request.input.strip()
        try:
            if _looks_like_a_link(text):
                article = extract_article(fetch_page(text))
            else:
                cleaned = clean_text(text)
                article = Article(title=title_from(cleaned), text=cleaned)
            chunks = chunk_text(article.text)
            if not chunks:
                raise ExtractionError("There is nothing readable in that text.")
        except ExtractionError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        job_id = app.state.service.add_job(article.title, request.voice, chunks)
        return {"job": job_id, "title": article.title, "chunks": len(chunks)}

    @app.get("/chunk/{job_id}/{n:int}.wav")
    def chunk(job_id: str, n: int) -> FileResponse:
        try:
            future = app.state.service.chunk_future(job_id, n)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="That reading has expired or does not exist."
            ) from None
        try:
            path = future.result(timeout=CHUNK_WAIT_SECONDS)
        except (FutureTimeout, CancelledError):
            raise HTTPException(
                status_code=503, detail="The reader is busy. Try again in a moment."
            ) from None
        except Exception:
            logger.exception("Generating chunk %d of job %s failed", n, job_id)
            raise HTTPException(status_code=500, detail="Could not generate that part.") from None
        return FileResponse(
            path, media_type="audio/wav", headers={"Cache-Control": "private, max-age=3600"}
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
