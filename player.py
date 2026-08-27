"""Server-side audio playback engine for YouTube videos.

Resolves audio streams with yt-dlp and plays them on the machine running this
process by driving an ffplay subprocess.  ffplay has no control channel, so
pause / seek / volume changes are implemented by respawning it at the desired
offset.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from yt_dlp import YoutubeDL

FFPLAY = os.environ.get("FFPLAY") or shutil.which("ffplay") or "ffplay"

# Googlevideo stream URLs are signed and expire; re-resolve well before that.
STREAM_TTL = 30 * 60

_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"

_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class PlayerError(RuntimeError):
    """Raised for problems the user should see in the UI."""


@dataclass
class Track:
    video_id: str
    title: str
    webpage_url: str
    duration: Optional[float] = None
    uploader: Optional[str] = None
    thumbnail: Optional[str] = None
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # cached direct stream: (url, headers, expires_at)
    stream: Optional[tuple] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "video_id": self.video_id,
            "title": self.title,
            "url": self.webpage_url,
            "duration": self.duration,
            "uploader": self.uploader,
            "thumbnail": self.thumbnail,
        }


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def resolve_entries(query: str) -> list[Track]:
    """Turn a URL, video id or search phrase into one or more tracks.

    Playlists are expanded flat (metadata only) so adding them stays fast; the
    playable stream URL is resolved lazily just before each track starts.
    """
    query = query.strip()
    if not query:
        raise PlayerError("URL または検索語を入力してください")

    if not query.startswith(("http://", "https://", "ytsearch")):
        # Bare 11-char video id, otherwise treat it as a search phrase.
        if len(query) == 11 and all(c.isalnum() or c in "-_" for c in query):
            query = _watch_url(query)
        else:
            query = f"ytsearch1:{query}"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "format": _FORMAT,
        "noprogress": True,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception as exc:  # yt_dlp raises a zoo of exception types
        raise PlayerError(f"取得に失敗しました: {exc}") from exc

    if info is None:
        raise PlayerError("動画が見つかりませんでした")

    if info.get("_type") in ("playlist", "multi_video"):
        entries = info.get("entries") or []
    else:
        entries = [info]

    tracks: list[Track] = []
    for entry in entries:
        if not entry:
            continue
        video_id = entry.get("id")
        if not video_id:
            continue
        tracks.append(
            Track(
                video_id=video_id,
                title=entry.get("title") or video_id,
                webpage_url=entry.get("webpage_url") or _watch_url(video_id),
                duration=entry.get("duration"),
                uploader=entry.get("uploader") or entry.get("channel"),
                thumbnail=entry.get("thumbnail")
                or f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
            )
        )
    if not tracks:
        raise PlayerError("再生できる動画が見つかりませんでした")
    return tracks


def resolve_stream(track: Track) -> tuple[str, str]:
    """Return (direct stream url, ffmpeg header block) for a track."""
    cached = track.stream
    if cached and cached[2] > time.time():
        return cached[0], cached[1]

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": _FORMAT,
        "noplaylist": True,
        "noprogress": True,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(track.webpage_url, download=False)
    except Exception as exc:
        raise PlayerError(f"音声ストリームを取得できませんでした: {exc}") from exc

    source = info
    url = info.get("url")
    if not url:
        for fmt in info.get("requested_formats") or []:
            if fmt.get("acodec") != "none" and fmt.get("url"):
                source, url = fmt, fmt["url"]
                break
    if not url:
        raise PlayerError("音声ストリームが見つかりませんでした")

    headers = "".join(
        f"{k}: {v}\r\n"
        for k, v in (source.get("http_headers") or {}).items()
        if k.lower() != "accept-encoding"
    )
    if track.duration is None:
        track.duration = info.get("duration")
    track.stream = (url, headers, time.time() + STREAM_TTL)
    return url, headers


class Player:
    """Queue-based audio player. All public methods are thread safe."""

    def __init__(self) -> None:
        self._op = threading.RLock()   # serializes commands
        self._st = threading.RLock()   # guards the fields below (held briefly)
        self.queue: list[Track] = []
        self.index: int = -1
        self.state: str = "idle"       # idle | playing | paused
        self.volume: int = 80
        self.repeat: str = "off"       # off | one | all
        self.error: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._offset: float = 0.0      # seconds of the current track already played
        self._started_at: Optional[float] = None
        threading.Thread(target=self._monitor, daemon=True).start()

    # ---------------------------------------------------------------- helpers

    def _position_locked(self) -> float:
        if self.state == "playing" and self._started_at is not None:
            return self._offset + (time.monotonic() - self._started_at)
        return self._offset

    def _current_locked(self) -> Optional[Track]:
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    def _kill_locked(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
        except OSError:
            pass
        threading.Thread(target=self._reap, args=(proc,), daemon=True).start()

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _spawn(self, track: Track, offset: float) -> None:
        """Resolve and start playback.  Call with `_op` held, `_st` released."""
        url, headers = resolve_stream(track)
        args = [
            FFPLAY,
            "-nodisp",
            "-autoexit",
            "-hide_banner",
            "-loglevel", "error",
            "-vn",
            "-volume", str(self.volume),
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
        ]
        if headers:
            args += ["-headers", headers]
        if offset > 0.5:
            args += ["-ss", f"{offset:.3f}"]
        args += ["-i", url]

        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATION_FLAGS,
            )
        except FileNotFoundError as exc:
            raise PlayerError(
                f"ffplay が見つかりません ({FFPLAY})。FFmpeg をインストールしてください。"
            ) from exc

        with self._st:
            self._kill_locked()
            self._proc = proc
            self._offset = offset
            self._started_at = time.monotonic()
            self.state = "playing"
            self.error = None

    def _play_index(self, idx: int, offset: float = 0.0) -> None:
        with self._st:
            if not 0 <= idx < len(self.queue):
                raise PlayerError("キューの範囲外です")
            track = self.queue[idx]
            self.index = idx
            self._kill_locked()
            self.state = "idle"
            self._offset = offset
            self._started_at = None
        try:
            self._spawn(track, offset)
        except PlayerError as exc:
            with self._st:
                self.state = "idle"
                self.error = str(exc)
            raise

    # ------------------------------------------------------------- monitoring

    def _monitor(self) -> None:
        """Advance the queue when ffplay exits on its own (track finished)."""
        while True:
            time.sleep(0.4)
            with self._st:
                proc = self._proc
                if proc is None or self.state != "playing":
                    continue
                if proc.poll() is None:
                    continue
                self._proc = None
                self.state = "idle"
                self._started_at = None
                self._offset = 0.0
                repeat, index, length = self.repeat, self.index, len(self.queue)

            if repeat == "one":
                nxt = index
            elif index + 1 < length:
                nxt = index + 1
            elif repeat == "all" and length:
                nxt = 0
            else:
                continue

            with self._op:
                try:
                    self._play_index(nxt)
                except PlayerError:
                    pass

    # ---------------------------------------------------------- command layer

    def add(self, query: str, play_now: bool = False) -> list[Track]:
        tracks = resolve_entries(query)
        with self._op:
            with self._st:
                start = len(self.queue)
                self.queue.extend(tracks)
                idle = self.index < 0 or self.state == "idle"
            if play_now or idle:
                self._play_index(start)
        return tracks

    def play_at(self, idx: int) -> None:
        with self._op:
            self._play_index(idx)

    def stop(self) -> None:
        with self._op, self._st:
            self._kill_locked()
            self.state = "idle"
            self._offset = 0.0
            self._started_at = None

    def pause(self) -> None:
        with self._op, self._st:
            if self.state != "playing":
                return
            self._offset = self._position_locked()
            self._kill_locked()
            self.state = "paused"
            self._started_at = None

    def resume(self) -> None:
        with self._op:
            with self._st:
                if self.state != "paused" or self._current_locked() is None:
                    return
                idx, offset = self.index, self._offset
            self._play_index(idx, offset)

    def toggle(self) -> None:
        with self._st:
            state = self.state
            idx = self.index if self._current_locked() is not None else -1
        if state == "playing":
            self.pause()
        elif state == "paused":
            self.resume()
        elif idx >= 0:
            self.play_at(idx)

    def skip(self, delta: int) -> None:
        with self._op:
            with self._st:
                if not self.queue:
                    raise PlayerError("キューが空です")
                idx = self.index + delta
                if self.repeat == "all":
                    idx %= len(self.queue)
                idx = max(0, min(idx, len(self.queue) - 1))
            self._play_index(idx)

    def seek(self, position: float) -> None:
        with self._op:
            with self._st:
                track = self._current_locked()
                if track is None:
                    raise PlayerError("再生中の曲がありません")
                position = max(0.0, float(position))
                if track.duration:
                    position = min(position, max(0.0, track.duration - 1))
                state, idx = self.state, self.index
                if state != "playing":
                    self._offset = position
            if state == "playing":
                self._play_index(idx, position)

    def set_volume(self, volume: int) -> None:
        with self._op:
            with self._st:
                self.volume = max(0, min(100, int(volume)))
                state, idx = self.state, self.index
                position = self._position_locked()
            if state == "playing":
                # ffplay fixes its volume at launch, so restart in place.
                self._play_index(idx, position)

    def set_repeat(self, mode: str) -> None:
        if mode not in ("off", "one", "all"):
            raise PlayerError(f"不明なリピートモード: {mode}")
        with self._st:
            self.repeat = mode

    def remove(self, idx: int) -> None:
        with self._op:
            with self._st:
                if not 0 <= idx < len(self.queue):
                    raise PlayerError("キューの範囲外です")
                was_current = idx == self.index
                self.queue.pop(idx)
                if idx < self.index:
                    self.index -= 1
                length = len(self.queue)
            if not was_current:
                return
            self.stop()
            if idx < length:
                self._play_index(idx)
            else:
                with self._st:
                    self.index = length - 1

    def clear(self) -> None:
        self.stop()
        with self._op, self._st:
            self.queue.clear()
            self.index = -1

    def status(self) -> dict[str, Any]:
        with self._st:
            current = self._current_locked()
            return {
                "state": self.state,
                "position": round(self._position_locked(), 2),
                "volume": self.volume,
                "repeat": self.repeat,
                "index": self.index,
                "error": self.error,
                "current": current.to_dict() if current else None,
                "queue": [t.to_dict() for t in self.queue],
            }
