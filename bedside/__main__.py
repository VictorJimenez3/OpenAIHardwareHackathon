"""Run the Mac host. See ``python -m bedside --help`` for demo modes."""

import argparse
from collections import deque
import logging
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time

from dotenv import dotenv_values

from .session import CheckIn
from .voice import Voice


LOG = logging.getLogger("bedside")


class CameraInput:
    def __init__(self, emit):
        self.emit = emit
        self.frame = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._run, name="head-gestures", daemon=True)
        self.worker.start()

    def submit(self, event):
        try:
            self.frame.get_nowait()
        except queue.Empty:
            pass
        self.frame.put_nowait((time.monotonic(), event))

    def _run(self):
        from .vision import HeadGestures

        prompt_id = ""
        count = 0
        ready_reported = False
        last_report = time.monotonic()
        with HeadGestures() as model:
            while not self.stop.is_set():
                try:
                    received, event = self.frame.get(timeout=0.2)
                except queue.Empty:
                    continue
                if time.monotonic() - received > 0.7:
                    continue
                if prompt_id != event["prompt_id"]:
                    prompt_id = event["prompt_id"]
                    model.reset(prompt_id)
                    count = 0
                    ready_reported = False
                    last_report = time.monotonic()
                try:
                    answer = model.feed(event["jpeg"])
                except Exception:
                    LOG.exception("Camera recognition failed; use touch")
                    self.emit("patient", {"type": "camera_error", "prompt_id": prompt_id})
                    return
                count += 1
                if not ready_reported and model.last_status == "ready":
                    ready_reported = True
                    self.emit("patient", {"type": "camera_ready", "prompt_id": prompt_id})
                elapsed = time.monotonic() - last_report
                if elapsed >= 3:
                    LOG.info("Camera input: %.1f fps (%s, %s, %s)", count / elapsed, model.backend,
                             model.feature_mode or "no pose", model.last_status)
                    count = 0
                    last_report = time.monotonic()
                if answer:
                    self.emit("patient", {"type": "input", "prompt_id": prompt_id,
                                          "value": answer, "source": "camera"})

    def close(self):
        self.stop.set()
        self.worker.join(timeout=1)


def load_voice_settings():
    settings = dotenv_values(Path(__file__).resolve().parents[1] / ".env", interpolate=False)
    for name in ("OPENAI_API_KEY", "OPENAI_LIVE_MODEL", "OPENAI_LIVE_VOICE"):
        if settings.get(name) and not os.getenv(name):
            os.environ[name] = settings[name]


def read_commands(events):
    for line in sys.stdin:
        events.put(("console", {"type": line.strip().lower()}))


def main():
    parser = argparse.ArgumentParser(description="Bedside check-in Mac host")
    parser.add_argument("--patient-port", help="CoreS3 serial port (auto-detected by default)")
    parser.add_argument("--caregiver-port", help="StickS3 serial port (auto-detected by default)")
    parser.add_argument("--voice", choices=("auto", "live", "local", "off"), default="auto")
    parser.add_argument("--camera", action="store_true", help="Enable experimental CoreS3 head gestures")
    parser.add_argument("--simulate", choices=("both", "patient", "caregiver"),
                        help="Replace selected hardware roles with a clearly labeled terminal simulation")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    simulated = {"patient", "caregiver"} if args.simulate == "both" else {args.simulate} - {None}
    if args.camera and "patient" in simulated:
        parser.error("--camera requires a physical CoreS3")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    events = queue.Queue()

    def emit(role, packet):
        events.put((role, packet))

    camera = CameraInput(emit) if args.camera else None

    def on_wire(role, event):
        if role in simulated:
            return
        if event.get("type") == "frame":
            if camera and camera.worker.is_alive() and isinstance(event.get("jpeg"), str) and isinstance(event.get("prompt_id"), str):
                camera.submit(event)
            return
        emit(role, event)

    links = None
    if simulated != {"patient", "caregiver"}:
        from .serial_link import SerialLinks

        links = SerialLinks(on_wire, patient_port=args.patient_port, caregiver_port=args.caregiver_port)

    simulated_events = deque()

    def send(role, packet):
        if role in simulated:
            if packet["type"] == "show_request":
                LOG.info("Simulated caregiver display | %s", packet["text"])
                simulated_events.append((role, {"type": "displayed", "request_id": packet["request_id"]}))
            return True
        return links is not None and links.send(role, packet)

    load_voice_settings()
    voice = Voice(args.voice)
    controller = CheckIn(send, voice.speak, camera=args.camera)
    try:
        if simulated:
            LOG.info("Simulated role(s): %s", ", ".join(sorted(simulated)))
            LOG.info("Terminal commands: start, yes, no, send, cancel, retry, ack, quit")
            for role in simulated:
                emit(role, {"type": "hello"})
            threading.Thread(target=read_commands, args=(events,), daemon=True).start()
        if links:
            LOG.info("Looking for ESP32-S3 boards on USB")
            links.start()
        voice_label = voice.backend
        if args.voice in {"auto", "live"} and os.getenv("OPENAI_API_KEY"):
            voice_label = "GPT-Live on demand, macOS fallback"
        LOG.info("Voice: %s | Head gestures: %s", voice_label, "on" if camera else "off")

        while not stop.is_set():
            try:
                role, event = events.get(timeout=0.1)
            except queue.Empty:
                controller.tick()
                continue
            if role == "console":
                command = event["type"]
                if command == "quit":
                    break
                if command == "ack" and "caregiver" in simulated:
                    controller.handle("caregiver", {"type": "acknowledged", "request_id": controller.request_id})
                elif command != "ack" and "patient" in simulated:
                    controller.handle("patient", {"type": "input", "prompt_id": controller.prompt_id,
                                                  "value": command, "source": "simulation"})
            else:
                controller.handle(role, event)
            while simulated_events:
                controller.handle(*simulated_events.popleft())
            controller.tick()
    except KeyboardInterrupt:
        pass
    finally:
        if links:
            if controller.camera_prompt:
                links.send("patient", {"type": "camera", "enabled": False, "prompt_id": controller.camera_prompt})
            links.close()
        if camera:
            camera.close()
        voice.close()


if __name__ == "__main__":
    main()
