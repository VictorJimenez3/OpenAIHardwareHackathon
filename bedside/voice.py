"""Non-blocking speech output for the bedside check-in.

The default backend is the macOS ``say`` command, so the MVP works without an
audio SDK.  When the OpenAI SDK and ``OPENAI_API_KEY`` are present,
``mode="auto"`` starts an optional GPT-Live session in the background.  Exact
review text is always sent through the local voice path so the words read to
the patient match the words sent to the caregiver.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import platform
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


LOGGER = logging.getLogger(__name__)
LIVE_START_TIMEOUT_SECONDS = 8.0
LIVE_AUDIO_DEADLINE_SECONDS = 4.0
LIVE_AUDIO_QUIET_SECONDS = 0.75
LIVE_SILENCE_WINDOW_SECONDS = 6.0
LIVE_SILENCE_CHUNK = base64.b64encode(b"\x00" * 960).decode("ascii")


def _event_value(event: Any, name: str, default: Any = None) -> Any:
    """Read a field from either an OpenAI SDK object or a decoded mapping."""

    if isinstance(event, dict):
        return event.get(name, default)
    return getattr(event, name, default)


class _LocalVoice:
    """An interruptible wrapper around the macOS speech synthesizer."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    def speak(self, text: str) -> None:
        if not text:
            return
        if platform.system() != "Darwin":
            self._logger.warning("local speech backend is only available on macOS")
            return
        executable = shutil.which("say")
        if executable is None:
            self._logger.warning("macOS 'say' command was not found")
            return
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
            # Do not use a shell.  Speech text is treated as content, never as
            # a command, even when it came from a model.
            self._process = subprocess.Popen(
                [executable, text],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def close(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
            self._process = None


@dataclass(frozen=True)
class _LiveCommand:
    text: str
    ticket: "_LiveTicket"


@dataclass(eq=False)
class _LiveTicket:
    """Tracks whether a queued utterance produced any audio."""

    text: str
    audio_started: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    audio_bytes: int = 0


class _LiveVoice:
    """Optional GPT-Live output worker.

    The worker consumes text commands through a queue, listens for
    ``session.output_audio.delta`` events, and streams 24 kHz PCM to ffplay.
    All SDK imports happen in the worker, keeping the local-only MVP
    dependency-free. Runtime failure is reported so the caller can fall back
    to ``say``.
    """

    def __init__(
        self,
        *,
        model: str,
        logger: logging.Logger,
        on_failure: Callable[[Exception], None],
    ) -> None:
        self.model = model
        self._logger = logger
        self._on_failure = on_failure
        self._commands: queue.Queue[_LiveCommand | None] = queue.Queue()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._player_lock = threading.Lock()
        self._player: subprocess.Popen[bytes] | None = None
        self._player_path: str | None = None
        self._audio_gate_until = 0.0
        self._ticket_lock = threading.Lock()
        self._active_ticket: _LiveTicket | None = None
        self._silence_lock = threading.Lock()
        self._silence_generation = 0
        self._thread = threading.Thread(
            target=self._thread_main, name="gpt-live-voice", daemon=True
        )
        self._thread.start()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def speak(self, text: str) -> _LiveTicket | None:
        if self._closed.is_set():
            return None
        ticket = _LiveTicket(text)
        self._commands.put(_LiveCommand(text, ticket))
        return ticket

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._silence_lock:
            self._silence_generation += 1
        if threading.current_thread() is self._thread:
            return
        self._commands.put(None)
        self._thread.join(timeout=1.5)

    def interrupt_audio(self) -> None:
        """Stop any buffered Live audio before exact local readback."""

        with self._player_lock:
            player = self._player
            self._player = None
            self._audio_gate_until = time.monotonic() + LIVE_AUDIO_QUIET_SECONDS
        with self._silence_lock:
            self._silence_generation += 1
        with self._ticket_lock:
            if self._active_ticket is not None:
                self._active_ticket.cancelled.set()
            self._active_ticket = None
        if player is not None and player.poll() is None:
            player.terminate()

    def cancel_ticket(self, ticket: _LiveTicket) -> None:
        ticket.cancelled.set()
        with self._ticket_lock:
            if self._active_ticket is ticket:
                self._active_ticket = None
        self.interrupt_audio()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # noqa: BLE001 - optional backend boundary
            if not self._closed.is_set():
                self._on_failure(exc)

    async def _run(self) -> None:
        try:
            from openai import AsyncOpenAI  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "GPT-Live backend requires the optional 'openai' package"
            ) from exc

        player_path = shutil.which("ffplay")
        if player_path is None:
            raise RuntimeError("GPT-Live backend requires ffplay for PCM playback")
        self._player_path = player_path

        try:
            async with AsyncOpenAI() as client:
                async with client.live.connect() as connection:
                    loop = asyncio.get_running_loop()
                    started: asyncio.Future[Any] = loop.create_future()
                    reader = asyncio.create_task(
                        self._read_audio(connection, started)
                    )
                    silence_sender: asyncio.Task[Any] | None = None
                    try:
                        await connection.session.start(
                            session={
                                "model": self.model,
                                "instructions": (
                                    "Ask one short bedside check-in question at a time. "
                                    "Speak clearly and wait for the patient's response."
                                ),
                                "audio": {
                                    "format": {"type": "audio/pcm", "rate": 24000},
                                    "output": {
                                        "voice": os.getenv("OPENAI_LIVE_VOICE", "marin")
                                    },
                                },
                                "delegation": {"type": "client"},
                            }
                        )
                        await asyncio.wait_for(started, LIVE_START_TIMEOUT_SECONDS)
                        self._ready.set()
                        while not self._closed.is_set():
                            if reader.done():
                                error = reader.exception()
                                if error is None:
                                    error = RuntimeError(
                                        "GPT-Live reader stopped unexpectedly"
                                    )
                                raise error
                            try:
                                command = self._commands.get_nowait()
                            except queue.Empty:
                                # Polling get_nowait avoids creating a blocked
                                # executor thread every timeout interval.
                                await asyncio.sleep(0.05)
                                continue
                            if command is None:
                                break
                            if command.ticket.cancelled.is_set():
                                continue
                            with self._ticket_lock:
                                self._active_ticket = command.ticket
                            try:
                                if silence_sender is not None:
                                    silence_sender.cancel()
                                    await asyncio.gather(
                                        silence_sender, return_exceptions=True
                                    )
                                silence_generation = self._begin_silence_window()
                                silence_sender = asyncio.create_task(
                                    self._send_silence(
                                        connection, silence_generation
                                    )
                                )
                                await self._wait_audio_gate()
                                self._start_fresh_player()
                                await connection.session.commentary.append(
                                    content=command.text,
                                    delegation_id=None,
                                )
                            except Exception:
                                with self._ticket_lock:
                                    if self._active_ticket is command.ticket:
                                        self._active_ticket = None
                                raise
                    finally:
                        if not reader.done():
                            reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
                        if silence_sender is not None:
                            silence_sender.cancel()
                            await asyncio.gather(
                                silence_sender, return_exceptions=True
                            )
        finally:
            self._ready.clear()
            with self._ticket_lock:
                self._active_ticket = None
            with self._player_lock:
                player = self._player
                self._player = None
            if player is not None:
                if player.stdin is not None:
                    player.stdin.close()
                player.terminate()
                try:
                    player.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    player.kill()

    def _spawn_player(self) -> subprocess.Popen[bytes]:
        if self._player_path is None:
            raise RuntimeError("audio player is not initialized")
        return subprocess.Popen(
            [
                self._player_path,
                "-loglevel",
                "quiet",
                "-nodisp",
                "-autoexit",
                "-f",
                "s16le",
                "-ar",
                "24000",
                "-ch_layout",
                "mono",
                "-i",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def _wait_audio_gate(self) -> None:
        with self._player_lock:
            delay = self._audio_gate_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    def _start_fresh_player(self) -> None:
        with self._player_lock:
            old_player = self._player
            self._player = None
        if old_player is not None and old_player.poll() is None:
            old_player.terminate()
        player = self._spawn_player()
        with self._player_lock:
            self._player = player

    async def _read_audio(
        self,
        connection: Any,
        started: asyncio.Future[Any],
    ) -> None:
        async for event in connection:
            event_type = _event_value(event, "type", "")
            if event_type == "session.started":
                if not started.done():
                    started.set_result(event)
                continue
            if event_type == "error":
                error = _event_value(event, "error", {})
                message = _event_value(error, "message", "GPT-Live reported an error")
                failure = RuntimeError(str(message))
                if not started.done():
                    started.set_exception(failure)
                raise failure
            if event_type != "session.output_audio.delta":
                continue
            delta = _event_value(event, "delta")
            with self._player_lock:
                if time.monotonic() < self._audio_gate_until:
                    continue
                player = self._player
            if not delta or player is None or player.stdin is None:
                continue
            with self._ticket_lock:
                ticket = self._active_ticket
            try:
                audio = base64.b64decode(delta)
                player.stdin.write(audio)
                player.stdin.flush()
                if ticket is not None and not ticket.cancelled.is_set():
                    first_audio = not ticket.audio_started.is_set()
                    ticket.audio_bytes += len(audio)
                    if first_audio:
                        ticket.audio_started.set()
                        self._logger.info("GPT-Live speech confirmed")
            except (BrokenPipeError, OSError):
                with self._player_lock:
                    if self._player is player:
                        self._player = None
        if not started.done():
            started.set_exception(
                RuntimeError("GPT-Live connection closed before session.started")
            )
        elif not self._closed.is_set():
            raise RuntimeError("GPT-Live connection closed while speaking")

    def _begin_silence_window(self) -> int:
        with self._silence_lock:
            self._silence_generation += 1
            return self._silence_generation

    async def _send_silence(self, connection: Any, generation: int) -> None:
        """Keep only the active question's audio timeline alive."""

        deadline = time.monotonic() + LIVE_SILENCE_WINDOW_SECONDS
        while not self._closed.is_set() and time.monotonic() < deadline:
            with self._silence_lock:
                if generation != self._silence_generation:
                    return
            await connection.session.input_audio.append(audio=LIVE_SILENCE_CHUNK)
            await asyncio.sleep(0.2)


class Voice:
    """Non-blocking voice output used by the bedside host.

    ``mode`` accepts ``"auto"`` (default), ``"local"``, ``"live"``, or
    ``"off"``. Calls to :meth:`speak` return immediately. Use ``exact=True``
    for patient-approved text, which always uses the local path.
    """

    def __init__(
        self,
        mode: str = "auto",
        *,
        model: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        mode = mode.lower().strip()
        if mode not in {"auto", "local", "live", "off"}:
            raise ValueError("voice mode must be auto, local, live, or off")
        self._logger = logger or LOGGER
        self._local = _LocalVoice(self._logger)
        self._live: _LiveVoice | None = None
        self._backend = "off" if mode == "off" else "local"
        self._live_enabled = mode in {"auto", "live"} and bool(
            os.getenv("OPENAI_API_KEY")
        )
        self._live_model = model or os.getenv("OPENAI_LIVE_MODEL", "gpt-live-1.1")
        self._fallback_lock = threading.Lock()
        self._fallbacks: dict[_LiveTicket, threading.Timer] = {}

        if mode == "live" and not self._live_enabled:
            self._logger.warning(
                "voice mode live requested without OPENAI_API_KEY; using local"
            )

    @property
    def backend(self) -> str:
        """Current backend name for truthful status text in the UI."""

        if self._live is not None and self._live.ready:
            return "gpt-live"
        if self._live is not None and self._live.closed:
            return "local" if self._backend != "off" else "off"
        return self._backend

    def speak(self, text: str, *, exact: bool = False) -> None:
        """Start speaking ``text`` without blocking the host event loop."""

        text = " ".join(str(text).split())
        if not text or self._backend == "off":
            return
        # A newer utterance supersedes any still-pending fallback for an older
        # prompt. This keeps an overdue question from speaking after the flow
        # has already advanced.
        self._cancel_fallbacks()
        if self._live is not None and (exact or self._live.ready):
            self._live.interrupt_audio()
        if not exact and self._live_enabled:
            live = self._ensure_live()
            ticket = live.speak(text)
            if ticket is not None:
                self._schedule_fallback(ticket)
                return
        self._local.speak(text)

    def close(self) -> None:
        self._cancel_fallbacks()
        if self._live is not None:
            self._live.close()
        self._local.close()
        self._backend = "off"

    def _live_failed(self, error: Exception) -> None:
        self._logger.warning("GPT-Live unavailable, using local speech: %s", error)
        live = self._live
        if live is not None:
            live.close()
        self._backend = "local"
        self._live = None

    def _ensure_live(self) -> _LiveVoice:
        live = self._live
        if live is not None and not live.closed:
            self._backend = "gpt-live" if live.ready else "live-starting"
            return live
        live = _LiveVoice(
            model=self._live_model,
            logger=self._logger,
            on_failure=self._live_failed,
        )
        self._live = live
        self._backend = "live-starting"
        return live

    def _schedule_fallback(self, ticket: _LiveTicket) -> None:
        timer = threading.Timer(
            LIVE_AUDIO_DEADLINE_SECONDS,
            self._audio_deadline,
            args=(ticket,),
        )
        timer.daemon = True
        with self._fallback_lock:
            self._fallbacks[ticket] = timer
        timer.start()

    def _audio_deadline(self, ticket: _LiveTicket) -> None:
        with self._fallback_lock:
            self._fallbacks.pop(ticket, None)
        if ticket.audio_started.is_set() or ticket.cancelled.is_set():
            return
        ticket.cancelled.set()
        live = self._live
        if live is not None:
            live.cancel_ticket(ticket)
            live.close()
            self._live = None
        self._backend = "local"
        self._logger.warning("GPT-Live produced no audio before deadline; using local speech")
        self._local.speak(ticket.text)

    def _cancel_fallbacks(self) -> None:
        with self._fallback_lock:
            pending = list(self._fallbacks.items())
            self._fallbacks.clear()
        for ticket, timer in pending:
            ticket.cancelled.set()
            timer.cancel()


__all__ = ["Voice"]
