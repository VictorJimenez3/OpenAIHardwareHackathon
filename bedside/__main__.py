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

from .camera_preview import CameraPreview
from .session import CheckIn
from .voice import Voice


LOG = logging.getLogger("bedside")


class CameraInput:
    def __init__(self, emit, current_scope):
        self.emit = emit
        self.current_scope = current_scope
        self.frame = queue.Queue(maxsize=1)
        self.submit_lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._run, name="head-gestures", daemon=True)
        self.worker.start()

    def submit(self, scope, jpeg):
        with self.submit_lock:
            try:
                self.frame.get_nowait()
            except queue.Empty:
                pass
            self.frame.put_nowait((time.monotonic(), scope, jpeg))

    def _run(self):
        from .vision import HeadGestures

        active_scope = None
        failed_scope = None
        count = 0
        ready_reported = False
        last_report = time.monotonic()
        # The CoreS3 now samples up to 12.5 fps. Keep the motion and
        # occlusion windows long enough for a deliberate down-and-up nod.
        with HeadGestures(calibration_frames=10, max_gap_frames=7, candidate_max_frames=30) as model:
            while not self.stop.is_set():
                try:
                    received, scope, jpeg = self.frame.get(timeout=0.2)
                except queue.Empty:
                    continue
                if (
                    scope == failed_scope or scope != self.current_scope()
                    or time.monotonic() - received > 0.7
                ):
                    continue
                prompt_id, generation = scope
                if active_scope != scope:
                    active_scope = scope
                    model.reset(prompt_id)
                    count = 0
                    ready_reported = False
                    last_report = time.monotonic()
                try:
                    answer = model.feed(jpeg)
                except Exception:
                    LOG.exception("Camera recognition failed; use touch")
                    failed_scope = scope
                    self.emit("camera", {"type": "camera_error", "prompt_id": prompt_id,
                                         "generation": generation})
                    continue
                if scope != self.current_scope() or self.stop.is_set():
                    continue
                count += 1
                if ready_reported != model.ready:
                    ready_reported = model.ready
                    self.emit("camera", {"type": "camera_ready", "prompt_id": prompt_id,
                                         "generation": generation, "ready": ready_reported})
                elapsed = time.monotonic() - last_report
                if elapsed >= 3:
                    LOG.info("Camera input: %.1f fps (%s, %s, %s)", count / elapsed, model.backend,
                             model.feature_mode or "no pose", model.last_status)
                    count = 0
                    last_report = time.monotonic()
                if answer:
                    self.emit("camera", {"type": "input", "prompt_id": prompt_id,
                                         "generation": generation, "value": answer,
                                         "source": model.answer_source})

    def close(self):
        self.stop.set()
        self.worker.join(timeout=1)


class AdaptivePlanner:
    """Run the synchronous question chooser away from the host event loop."""

    def __init__(self, emit):
        self.emit = emit
        self.jobs = queue.Queue()
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._run, name="adaptive-planner", daemon=True)
        self.worker.start()

    def submit(self, prompt_id, candidates, confirmed, context):
        self.jobs.put(("choice", prompt_id, candidates, confirmed, context))

    def submit_handoff(self, prompt_id, context, declined):
        self.jobs.put(("handoff", prompt_id, context, declined))

    def submit_pain_followup(self, prompt_id, context):
        self.jobs.put(("pain_followup", prompt_id, context))

    def _run(self):
        try:
            from . import adaptive as adaptive_module
            choose_question = getattr(adaptive_module, "choose_question", None)
            draft_request = getattr(adaptive_module, "draft_request", None)
        except Exception:
            adaptive_module = None
            choose_question = None
            draft_request = None
            LOG.warning("Live adaptive planner unavailable; using local scenarios")
        while not self.stop.is_set():
            try:
                job = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                return
            job_type = job[0]
            if job_type == "handoff":
                _, prompt_id, context, declined = job
                phrase = None
                if draft_request is not None:
                    try:
                        phrase = draft_request(context=context, declined=tuple(declined))
                    except Exception:
                        LOG.warning("Handoff planner failed; showing no-message choice")
                self.emit("adaptive", {
                    "type": "handoff_draft",
                    "prompt_id": prompt_id,
                    "phrase": phrase,
                    "source": "live" if isinstance(phrase, str) else "local",
                })
                continue
            if job_type == "pain_followup":
                _, prompt_id, context = job
                draft = None
                draft_pain_followup = (
                    getattr(adaptive_module, "draft_pain_followup", None)
                    if adaptive_module is not None else None
                )
                if draft_pain_followup is not None:
                    try:
                        draft = draft_pain_followup(context=context)
                    except Exception:
                        LOG.warning("Pain follow-up planner failed; using pain-only review")
                self.emit("adaptive", {
                    "type": "pain_followup_draft",
                    "prompt_id": prompt_id,
                    "draft": draft,
                    "source": "live" if isinstance(draft, dict) else "local",
                })
                continue
            _, prompt_id, candidates, confirmed, context = job
            choice = None
            if choose_question is not None:
                try:
                    choice = choose_question(candidates=candidates, confirmed=confirmed, context=context)
                except Exception:
                    LOG.warning("Adaptive planner failed; using local scenario")
            source = "live" if choice in candidates else "local"
            self.emit("adaptive", {
                "type": "question_choice",
                "prompt_id": prompt_id,
                "question_id": choice,
                "source": source,
            })

    def close(self):
        self.stop.set()
        self.jobs.put(None)
        self.worker.join(timeout=1)


def load_voice_settings():
    settings = dotenv_values(Path(__file__).resolve().parents[1] / ".env", interpolate=False)
    for name in ("OPENAI_API_KEY", "OPENAI_LIVE_MODEL", "OPENAI_LIVE_VOICE", "OPENAI_ADAPTIVE_MODEL"):
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
    parser.add_argument("--camera", action="store_true", help="Enable CoreS3 camera head and hand gestures")
    parser.add_argument("--gesture", action="store_true",
                        help="Opt in to the patient's low-movement swipe input")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--adaptive", action="store_true",
                      help="Let the planner choose a useful follow-up question")
    mode.add_argument("--handoff", metavar="NOTE",
                      help="Draft a patient request from a fictional caregiver note")
    mode.add_argument("--scripted", action="store_true",
                      help="Use the legacy fixed pain-and-position script")
    parser.add_argument("--scenario", choices=("comfort", "pain", "mouth"), default="pain",
                        help="Simulated care context for adaptive mode")
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

    camera = None
    preview = CameraPreview() if args.camera else None
    planner = AdaptivePlanner(emit) if (args.adaptive or args.handoff) else None

    def show_preview_status(screen):
        cue = "\n".join(filter(None, (screen.text, screen.status)))
        preview.set_status(cue, screen.receipt)

    def on_wire(role, event):
        if role not in ("patient", "caregiver") or role in simulated:
            return
        kind = event.get("type")
        if kind == "frame":
            jpeg = event.get("jpeg")
            if role == "patient" and preview is not None and isinstance(jpeg, str) and jpeg:
                scope = controller.camera_scope
                preview.submit(jpeg)
                if camera and scope is not None and event.get("prompt_id") == scope[0]:
                    camera.submit(scope, jpeg)
            return
        if role == "patient" and kind == "hello":
            # SerialLinks emits hello once per connection. A replacement can
            # arrive without an intervening disconnect from the old port.
            event = {**event, "connection": object()}
        emit(role, event)

    links = None
    if simulated != {"patient", "caregiver"}:
        from .serial_link import SerialLinks

        links = SerialLinks(on_wire, patient_port=args.patient_port, caregiver_port=args.caregiver_port)

    simulated_events = deque()
    simulated_caregiver_request = ""

    def send(role, packet):
        nonlocal simulated_caregiver_request
        if role in simulated:
            if packet["type"] == "show_request":
                simulated_caregiver_request = packet["request_id"]
                LOG.info("Simulated caregiver display | %s", packet["text"])
                simulated_events.append((role, {
                    "type": "displayed",
                    "request_id": packet["request_id"],
                    "report_id": packet.get("report_id"),
                    "revision": packet.get("revision"),
                }))
            elif packet["type"] == "clear_request" and packet["request_id"] == simulated_caregiver_request:
                simulated_caregiver_request = ""
            return True
        return links is not None and links.send(role, packet)

    load_voice_settings()
    voice = Voice(args.voice)
    controller = CheckIn(
        send,
        voice.speak,
        camera=args.camera,
        adaptive=args.adaptive,
        scenario=args.scenario,
        planner=planner.submit if planner else None,
        handoff=args.handoff,
        handoff_planner=planner if args.handoff else None,
        gesture=args.gesture,
        progressive=not (args.adaptive or args.handoff or args.scripted),
        screen_changed=show_preview_status if preview is not None else None,
    )
    try:
        if preview is not None:
            show_preview_status(controller.screen)
            try:
                preview.start()
            except Exception:
                LOG.exception("Could not open the CoreS3 camera preview")
            camera = CameraInput(emit, lambda: controller.camera_scope)
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
        LOG.info("Voice: %s | Camera gestures: %s | Swipe gestures: %s", voice_label,
                 "on" if camera else "off", "on" if args.gesture else "off")
        if args.adaptive:
            LOG.info("Adaptive mode: %s scenario | OpenAI Responses with local fallback", args.scenario)
        if args.handoff:
            LOG.info("Handoff mode: fictional note | bounded draft with explicit review")
        if args.scripted:
            LOG.info("Scripted mode: legacy pain-and-position flow")
        elif not args.adaptive and not args.handoff:
            LOG.info("Progressive mode: pain alert first, optional location update")

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
                    controller.handle("caregiver", {"type": "acknowledged",
                                                   "request_id": simulated_caregiver_request})
                elif command != "ack" and "patient" in simulated:
                    controller.handle("patient", {"type": "input", "prompt_id": controller.prompt_id,
                                                  "value": command, "source": "touch"})
            else:
                controller.handle(role, event)
            while simulated_events:
                controller.handle(*simulated_events.popleft())
            controller.tick()
    except KeyboardInterrupt:
        pass
    finally:
        if links:
            if args.gesture:
                links.send("patient", {"type": "gesture", "enabled": False})
            controller.stop_camera_stream()
            links.close()
        if camera:
            camera.close()
        if preview is not None:
            preview.close()
        if planner:
            planner.close()
        voice.close()


if __name__ == "__main__":
    main()
