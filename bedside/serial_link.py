"""Small, reconnecting JSON-lines transport for the two bedside boards.

The boards expose USB CDC serial ports.  ``SerialLinks`` deliberately scans
only USB-looking macOS device names, so a nearby Bluetooth accessory is never
opened by accident.  The first ``hello`` from a board assigns its role
(``patient`` or ``caregiver``), after which all application events are routed
through the callback supplied by the host application.

The module keeps pyserial optional at import time.  This makes the rest of the
host app importable on a fresh laptop and gives a useful error only when the
transport is started without pyserial installed.
"""

from __future__ import annotations

import glob
import json
import platform
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


EventCallback = Callable[[str, dict[str, Any]], None]


def _usb_candidates() -> list[str]:
    """Return USB serial names without including Bluetooth/debug ports."""

    if platform.system() == "Darwin":
        patterns = (
            "/dev/cu.usbmodem*",
            "/dev/cu.usbserial*",
            "/dev/cu.SLAB*",
            "/dev/cu.wchusbserial*",
        )
    else:
        patterns = ("/dev/ttyACM*", "/dev/ttyUSB*")

    paths: set[str] = set()
    for pattern in patterns:
        paths.update(glob.glob(pattern))
    return sorted(paths)


@dataclass
class _Connection:
    path: str
    serial: Any
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    thread: Optional[threading.Thread] = None
    role: Optional[str] = None
    hello_announced: bool = False
    disconnected_reported: bool = False


class SerialLinks:
    """Manage USB links to the patient and caregiver boards.

    ``on_event`` is called as ``on_event(role, event)`` from reader threads.
    Events are decoded JSON objects.  A hello is delivered once for each new
    physical connection.  If a link drops, one synthetic
    ``{"type": "disconnected"}`` event is delivered for its role.

    Explicit ports are recommended for a live demo.  When omitted, the
    transport scans only USB-looking device names and identifies boards from
    their hello role after sending a ping.
    """

    def __init__(
        self,
        on_event: Optional[EventCallback] = None,
        *,
        patient_port: Optional[str] = None,
        caregiver_port: Optional[str] = None,
        baudrate: int = 115200,
        max_frame_bytes: int = 80_000,
        reconnect_interval: float = 1.0,
        ping_interval: float = 2.0,
        serial_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        if max_frame_bytes < 256:
            raise ValueError("max_frame_bytes must be at least 256")
        self.on_event = on_event or (lambda _role, _event: None)
        self.patient_port = patient_port
        self.caregiver_port = caregiver_port
        self.baudrate = baudrate
        self.max_frame_bytes = max_frame_bytes
        self.reconnect_interval = max(0.1, reconnect_interval)
        self.ping_interval = max(0.25, ping_interval)
        self._serial_factory = serial_factory

        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._manager_thread: Optional[threading.Thread] = None
        self._connections: Dict[str, _Connection] = {}
        self._by_role: Dict[str, _Connection] = {}
        self._started = False
        self._last_error: Optional[str] = None

    @property
    def last_error(self) -> Optional[str]:
        """Most recent non-fatal transport error, useful for a debug status."""

        with self._lock:
            return self._last_error

    @property
    def ports(self) -> dict[str, str]:
        """A snapshot of the currently identified role-to-port mapping."""

        with self._lock:
            return {
                role: conn.path
                for role, conn in self._by_role.items()
                if self._connections.get(conn.path) is conn
            }

    def port_for(self, role: str) -> Optional[str]:
        """Return the active device path for ``role``, if connected."""

        return self.ports.get(role)

    def start(self) -> None:
        """Start discovery and reader threads.  Calling twice is harmless."""

        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
            self._manager_thread = threading.Thread(
                target=self._manager_loop,
                name="bedside-serial-manager",
                daemon=True,
            )
            self._manager_thread.start()

    def close(self) -> None:
        """Stop discovery and close every open port."""

        with self._lock:
            if not self._started:
                return
            self._started = False
            self._stop.set()
            connections = list(self._connections.values())
            manager = self._manager_thread
            self._manager_thread = None

        for conn in connections:
            self._close_serial(conn.serial)
        if manager and manager is not threading.current_thread():
            manager.join(timeout=1.5)
        for conn in connections:
            if conn.thread and conn.thread is not threading.current_thread():
                conn.thread.join(timeout=1.5)
        with self._lock:
            self._connections.clear()
            self._by_role.clear()

    def send(self, role: str, event: dict[str, Any]) -> bool:
        """Send one JSON event to a connected role.

        The payload is bounded before writing.  ``False`` means no current
        connection exists or the write failed.  A later manager pass will
        reconnect the port after a failed write.
        """

        if not isinstance(event, dict):
            raise TypeError("event must be a dictionary")
        try:
            payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            encoded = (payload + "\n").encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            self._record_error(f"encode failed: {exc}")
            return False
        if len(encoded) > self.max_frame_bytes:
            self._record_error(f"outgoing frame exceeds {self.max_frame_bytes} bytes")
            return False

        with self._lock:
            conn = self._by_role.get(role)
            if conn is None or self._connections.get(conn.path) is not conn:
                return False
        try:
            with conn.write_lock:
                conn.serial.write(encoded)
                flush = getattr(conn.serial, "flush", None)
                if callable(flush):
                    flush()
            return True
        except Exception as exc:  # pyserial raises several platform exceptions
            self._record_error(f"write to {conn.path} failed: {exc}")
            self._drop_connection(conn)
            return False

    def _manager_loop(self) -> None:
        while not self._stop.is_set():
            self._connect_available()
            self._stop.wait(self.reconnect_interval)

    def _connect_available(self) -> None:
        for path in self._candidate_paths():
            if self._stop.is_set():
                return
            with self._lock:
                if path in self._connections:
                    continue
            self._open(path)

    def _candidate_paths(self) -> list[str]:
        paths: list[str] = []
        for path in (self.patient_port, self.caregiver_port):
            if path and path not in paths:
                paths.append(path)
        # An explicit patient (or caregiver) path does not prevent discovery
        # of the other board.  Discovery remains constrained to the USB
        # prefixes in ``_usb_candidates``.
        if self.patient_port is None or self.caregiver_port is None:
            paths.extend(path for path in _usb_candidates() if path not in paths)
        return paths

    def _open(self, path: str) -> None:
        factory = self._serial_factory
        if factory is None:
            try:
                import serial  # type: ignore

                factory = serial.Serial
            except ImportError as exc:
                self._record_error("pyserial is required for USB boards (pip install pyserial)")
                self._stop.set()
                return

        try:
            serial_port = factory(
                port=path,
                baudrate=self.baudrate,
                timeout=0.1,
                write_timeout=0.5,
            )
        except Exception as exc:
            self._record_error(f"open {path} failed: {exc}")
            return

        conn = _Connection(path=path, serial=serial_port)
        with self._lock:
            if self._stop.is_set():
                self._close_serial(serial_port)
                return
            self._connections[path] = conn
            conn.thread = threading.Thread(
                target=self._reader_loop,
                args=(conn,),
                name=f"bedside-serial-{path.rsplit('/', 1)[-1]}",
                daemon=True,
            )
            conn.thread.start()
        # Boards that were already powered before the host opened the port
        # answer this ping with hello, giving us an unambiguous role.
        self._write_direct(conn, {"type": "ping"})

    def _reader_loop(self, conn: _Connection) -> None:
        buffer = bytearray()
        oversized = False
        next_ping = time.monotonic() + self.ping_interval
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_ping:
                    self._write_direct(conn, {"type": "ping"})
                    next_ping = now + self.ping_interval
                try:
                    chunk = conn.serial.read(4096)
                except Exception as exc:
                    self._record_error(f"read from {conn.path} failed: {exc}")
                    break
                if not chunk:
                    continue
                buffer.extend(chunk)
                while True:
                    try:
                        newline = buffer.index(10)  # b"\\n"
                    except ValueError:
                        if len(buffer) > self.max_frame_bytes:
                            oversized = True
                            buffer.clear()
                        break
                    raw = bytes(buffer[:newline])
                    del buffer[: newline + 1]
                    if oversized:
                        oversized = False
                        continue
                    if len(raw) > self.max_frame_bytes:
                        self._record_error(f"incoming frame from {conn.path} exceeds limit")
                        continue
                    self._decode_and_dispatch(conn, raw)
        finally:
            self._drop_connection(conn)

    def _decode_and_dispatch(self, conn: _Connection, raw: bytes) -> None:
        raw = raw.rstrip(b"\r")
        if not raw:
            return
        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._record_error(f"invalid JSON from {conn.path}")
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "hello":
            self._assign_role(conn, event)
            return
        with self._lock:
            role = conn.role or "unknown"
        self._emit(role, event)

    def _assign_role(self, conn: _Connection, event: dict[str, Any]) -> None:
        advertised = event.get("role")
        role = advertised.strip() if isinstance(advertised, str) else ""
        role = role or "unknown"
        announce = False
        previous: Optional[_Connection] = None
        with self._lock:
            if self._connections.get(conn.path) is not conn:
                return
            if conn.role is None:
                existing = self._by_role.get(role)
                if existing is not None and existing is not conn:
                    # A board can re-enumerate at the same path while an old
                    # pyserial object is still waiting in a read timeout. The
                    # newest hello is authoritative. Retire the old object
                    # without emitting a misleading transient disconnect.
                    previous = existing
                    existing.disconnected_reported = True
                conn.role = role
                self._by_role[role] = conn
                announce = not conn.hello_announced
                conn.hello_announced = True
        if previous is not None:
            self._close_serial(previous.serial)
        if announce:
            self._emit(role, event)

    def _write_direct(self, conn: _Connection, event: dict[str, Any]) -> bool:
        try:
            encoded = (
                json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            with conn.write_lock:
                conn.serial.write(encoded)
                flush = getattr(conn.serial, "flush", None)
                if callable(flush):
                    flush()
            return True
        except Exception as exc:
            self._record_error(f"write to {conn.path} failed: {exc}")
            self._drop_connection(conn)
            return False

    def _drop_connection(self, conn: _Connection) -> None:
        role: Optional[str]
        should_report = False
        with self._lock:
            if self._connections.get(conn.path) is conn:
                del self._connections[conn.path]
            role = conn.role or "unknown"
            if self._by_role.get(role) is conn:
                del self._by_role[role]
            if not conn.disconnected_reported:
                conn.disconnected_reported = True
                should_report = bool(conn.hello_announced)
        self._close_serial(conn.serial)
        if should_report and not self._stop.is_set():
            self._emit(role or "unknown", {"type": "disconnected"})

    @staticmethod
    def _close_serial(serial_port: Any) -> None:
        try:
            serial_port.close()
        except Exception:
            pass

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message

    def _emit(self, role: str, event: dict[str, Any]) -> None:
        try:
            self.on_event(role, event)
        except Exception as exc:
            # An application callback must never kill the transport thread.
            self._record_error(f"event callback failed: {exc}")


__all__ = ["SerialLinks"]
