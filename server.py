"""HTTP front end for the server-side YouTube audio player."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from player import Player, PlayerError

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

player = Player()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    player.stop()


app = FastAPI(
    title="YouTube Music Server",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)


# ------------------------------------------------------------------- schemas


class AddRequest(BaseModel):
    url: str
    play_now: bool = False


class SeekRequest(BaseModel):
    position: float


class VolumeRequest(BaseModel):
    volume: int


class RepeatRequest(BaseModel):
    mode: str


# --------------------------------------------------------------------- utils


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PlayerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _ok(extra: Optional[dict] = None) -> dict:
    status = player.status()
    if extra:
        status.update(extra)
    return status


# ---------------------------------------------------------------------- API


@app.get("/api/status")
def get_status() -> dict:
    return player.status()


@app.post("/api/add")
def add(req: AddRequest) -> dict:
    tracks = _guard(player.add, req.url, req.play_now)
    return _ok({"added": [t.to_dict() for t in tracks]})


@app.post("/api/play")
def play(req: AddRequest) -> dict:
    tracks = _guard(player.add, req.url, True)
    return _ok({"added": [t.to_dict() for t in tracks]})


@app.post("/api/toggle")
def toggle() -> dict:
    _guard(player.toggle)
    return _ok()


@app.post("/api/pause")
def pause() -> dict:
    player.pause()
    return _ok()


@app.post("/api/resume")
def resume() -> dict:
    _guard(player.resume)
    return _ok()


@app.post("/api/stop")
def stop() -> dict:
    player.stop()
    return _ok()


@app.post("/api/next")
def next_track() -> dict:
    _guard(player.skip, 1)
    return _ok()


@app.post("/api/prev")
def prev_track() -> dict:
    _guard(player.skip, -1)
    return _ok()


@app.post("/api/seek")
def seek(req: SeekRequest) -> dict:
    _guard(player.seek, req.position)
    return _ok()


@app.post("/api/volume")
def volume(req: VolumeRequest) -> dict:
    _guard(player.set_volume, req.volume)
    return _ok()


@app.post("/api/repeat")
def repeat(req: RepeatRequest) -> dict:
    _guard(player.set_repeat, req.mode)
    return _ok()


@app.post("/api/queue/{index}/play")
def queue_play(index: int) -> dict:
    _guard(player.play_at, index)
    return _ok()


@app.delete("/api/queue/{index}")
def queue_remove(index: int) -> dict:
    _guard(player.remove, index)
    return _ok()


@app.delete("/api/queue")
def queue_clear() -> dict:
    player.clear()
    return _ok()


# --------------------------------------------------------------------- pages


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )
