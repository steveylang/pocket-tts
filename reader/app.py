"""Phone-friendly reader: paste text or a link, hear it read aloud one chunk at a time."""

import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
import wave
from collections.abc import AsyncIterator, Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

import numpy as np
import torch
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pocket_tts import TTSModel
from pocket_tts.utils.utils import _ORIGINS_OF_PREDEFINED_VOICES
from reader.chunking import chunk_text
from reader.documents import MAX_DOCUMENT_BYTES, extract_document
from reader.extract import (
    Article,
    ExtractionError,
    clean_text,
    extract_article,
    fetch_page,
    title_from,
)
from reader.voices import UPLOAD_SUFFIXES, Voice, VoiceError, VoiceStore, encode_voice

DEFAULT_VOICE = "alba"
JOB_TTL_SECONDS = 3600
# How long a chunk request waits for the (single) generator before saying "busy".
CHUNK_WAIT_SECONDS = 120
MAX_VOICE_UPLOAD_BYTES = 25_000_000
VOICE_TYPE_MESSAGE = "That file type can't be used. Use .wav, .mp3, .flac, .ogg or .safetensors."
STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SpeechModel(Protocol):
    """The part of TTSModel the reader uses (tests substitute a fake)."""

    has_voice_cloning: bool

    @property
    def sample_rate(self) -> int: ...

    def _cached_get_state_for_audio_prompt(self, voice: str | Path) -> object: ...

    def get_state_for_audio_prompt(
        self, audio_conditioning: Path, truncate: bool = False
    ) -> object: ...

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
    voice: str | Path
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

    def add_job(self, title: str, voice: str | Path, chunks: list[str]) -> str:
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

    def run_on_worker(self, work: Callable[[], T]) -> "Future[T]":
        """Run model work on the single worker, queued behind any generation in progress."""
        return self._executor.submit(work)

    def forget_cached_voices(self) -> None:
        """Release the voice states the model caches, so a deleted voice is not kept in memory."""
        clear = getattr(self.model._cached_get_state_for_audio_prompt, "cache_clear", None)
        if clear is not None:
            clear()

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


def _read_upload(file: UploadFile, limit: int) -> bytes:
    """Read an uploaded file, refusing anything over `limit` bytes."""
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status_code=400, detail=f"That file is larger than {limit // 1_000_000} MB."
        )
    return data


def create_app(model: SpeechModel | None = None, voices_dir: Path | None = None) -> FastAPI:
    """App factory. Run with: uvicorn reader.app:create_app --factory"""
    cache_dir = Path(tempfile.mkdtemp(prefix="pocket-tts-reader-"))
    # Relative to the service's working directory (the repo) unless READER_VOICES_DIR says otherwise.
    store = VoiceStore(voices_dir or Path(os.environ.get("READER_VOICES_DIR") or "voices"))

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
        return {
            "voices": list(_ORIGINS_OF_PREDEFINED_VOICES),
            "custom": [{"id": voice.id, "name": voice.name} for voice in store.list_voices()],
            "default": DEFAULT_VOICE,
        }

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # A voice upload with no usable file (browsers send a nameless part as plain text) is the
        # same "can't be used" 400 as a wrong type; every other validation failure stays a 422.
        is_voice_upload = request.method == "POST" and request.url.path == "/voices"
        if is_voice_upload and any(tuple(e["loc"]) == ("body", "file") for e in error.errors()):
            return JSONResponse(status_code=400, content={"detail": VOICE_TYPE_MESSAGE})
        return await request_validation_exception_handler(request, error)

    @app.post("/voices")
    def add_voice(file: UploadFile | None = File(None), name: str = Form(...)) -> dict[str, str]:
        # A missing file (or one with no filename) gets the same 400 via the handler below.
        upload_name = Path(file.filename or "").name.lower() if file else ""
        suffix = Path(upload_name).suffix
        if file is None or suffix not in UPLOAD_SUFFIXES:
            raise HTTPException(status_code=400, detail=VOICE_TYPE_MESSAGE)
        try:
            name = store.prepare_name(name)  # fail on a bad name before reading the upload
        except VoiceError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        data = _read_upload(file, MAX_VOICE_UPLOAD_BYTES)
        service: ReaderService = app.state.service
        # The client's file name is never used on disk: only its whitelisted suffix, which is what
        # tells the model audio from a voice state.
        upload_dir = cache_dir / "uploads" / secrets.token_urlsafe(8)
        upload = upload_dir / f"upload{suffix}"
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
            upload.write_bytes(data)
        except OSError:
            shutil.rmtree(upload_dir, ignore_errors=True)
            logger.exception("Saving the voice upload %r failed", name)
            raise HTTPException(status_code=500, detail="Could not add that voice.") from None

        def work() -> Voice:
            try:
                return store.add(name, lambda dest: encode_voice(service.model, upload, dest))
            finally:
                shutil.rmtree(upload_dir, ignore_errors=True)

        try:
            voice = service.run_on_worker(work).result(timeout=CHUNK_WAIT_SECONDS)
        except VoiceError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except FutureTimeout:
            raise HTTPException(
                status_code=503,
                detail="The reader is busy. The voice may still appear in the list shortly.",
            ) from None
        except Exception:
            logger.exception("Adding the voice %r failed", name)
            raise HTTPException(status_code=500, detail="Could not add that voice.") from None
        return {"id": voice.id, "name": voice.name}

    @app.delete("/voices/{voice_id}", status_code=204)
    def delete_voice(voice_id: str) -> Response:
        if not store.delete(voice_id):
            raise HTTPException(status_code=404, detail="That voice does not exist.")
        app.state.service.forget_cached_voices()
        return Response(status_code=204)

    def resolve_voice(voice: str) -> str | Path:
        if voice in _ORIGINS_OF_PREDEFINED_VOICES:
            return voice
        path = store.state_path(voice)
        if path is None:
            raise HTTPException(status_code=400, detail=f"Unknown voice: {voice}")
        return path

    def start_reading(make_article: Callable[[], Article], voice: str) -> dict[str, object]:
        """Shared by every way of starting a reading: check the voice, extract, chunk, queue."""
        resolved = resolve_voice(voice)
        try:
            article = make_article()
            chunks = chunk_text(article.text)
            if not chunks:
                raise ExtractionError("There is nothing readable in that text.")
        except ExtractionError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        job_id = app.state.service.add_job(article.title, resolved, chunks)
        return {"job": job_id, "title": article.title, "chunks": len(chunks)}

    def article_from_input(text: str) -> Article:
        if _looks_like_a_link(text):
            return extract_article(fetch_page(text))
        cleaned = clean_text(text)
        return Article(title=title_from(cleaned), text=cleaned)

    @app.post("/prepare")
    def prepare(request: PrepareRequest) -> dict[str, object]:
        return start_reading(lambda: article_from_input(request.input.strip()), request.voice)

    @app.post("/prepare-file")
    def prepare_file(
        file: UploadFile = File(...), voice: str = Form(DEFAULT_VOICE)
    ) -> dict[str, object]:
        def article() -> Article:
            return extract_document(file.filename or "", _read_upload(file, MAX_DOCUMENT_BYTES))

        return start_reading(article, voice)

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
