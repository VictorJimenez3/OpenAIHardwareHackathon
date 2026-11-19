"""Display CoreS3 frames in a separate macOS preview window."""

import hashlib
import json
from pathlib import Path
import platform
import select
import shutil
import subprocess
import tempfile
import threading


class CameraPreview:
    """Send the most recent frame and status to a local AppKit panel."""

    def __init__(self):
        self._lifecycle = threading.Lock()
        self._changed = threading.Condition()
        self._process = None
        self._writer = None
        self._running = False
        self._frame = None
        self._status = ("", "")
        self._status_changed = False

    def start(self):
        with self._lifecycle:
            if self._process is not None:
                if self._process.poll() is None:
                    return
                self._close()

            compiler = shutil.which("swiftc")
            if platform.system() != "Darwin" or compiler is None:
                raise RuntimeError("The camera preview requires macOS AppKit and swiftc")

            source = Path(__file__).with_name("camera_preview.swift")
            contents = source.read_bytes()
            digest = hashlib.sha256(contents).hexdigest()[:16]
            cache = Path(tempfile.gettempdir()) / "cores3-camera-preview"
            cache.mkdir(mode=0o700, exist_ok=True)
            binary = cache / f"preview-{platform.machine()}-{digest}"
            if not binary.is_file():
                with tempfile.TemporaryDirectory(dir=cache) as directory:
                    pinned_source = Path(directory) / source.name
                    pinned_source.write_bytes(contents)
                    output = Path(directory) / "preview"
                    subprocess.run(
                        [compiler, str(pinned_source), "-o", str(output)],
                        check=True,
                        timeout=30,
                    )
                    output.replace(binary)

            self._process = subprocess.Popen(
                [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE
            )
            try:
                with self._process.stdout as ready:
                    if not select.select([ready], [], [], 5)[0] or ready.readline() != b"ready\n":
                        raise RuntimeError("The macOS camera preview did not open")
                with self._changed:
                    self._running = True
                    self._status_changed = True
                writer = threading.Thread(
                    target=self._write, args=(self._process,), daemon=True
                )
                writer.start()
                self._writer = writer
            except BaseException:
                self._close()
                raise

    def submit(self, jpeg_b64: str):
        with self._changed:
            if self._running:
                self._frame = jpeg_b64
                self._changed.notify()

    def set_status(self, cue: str, receipt: str = ""):
        with self._changed:
            status = (cue, receipt)
            if status != self._status:
                self._status = status
                self._status_changed = True
                self._changed.notify()

    def _write(self, process):
        try:
            with process.stdin as stream:
                while True:
                    with self._changed:
                        self._changed.wait_for(
                            lambda: not self._running
                            or self._frame is not None
                            or self._status_changed
                        )
                        if not self._running:
                            return
                        status = self._status if self._status_changed else None
                        self._status_changed = False
                        frame, self._frame = self._frame, None

                    if status is not None:
                        cue, receipt = status
                        payload = json.dumps({"cue": cue, "receipt": receipt})
                        stream.write(payload.encode() + b"\n")
                        stream.flush()
                    if frame is not None:
                        stream.write(frame.strip().encode() + b"\n")
                        stream.flush()
        except OSError:
            pass
        finally:
            with self._changed:
                self._running = False
                self._frame = None

    def close(self):
        with self._lifecycle:
            self._close()

    def _close(self):
        if self._process is None:
            return
        with self._changed:
            self._running = False
            self._frame = None
            self._changed.notify()
        if self._writer is None:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        if self._writer is not None:
            self._writer.join()
            self._writer = None
        self._process.stdout.close()
        self._process = None
