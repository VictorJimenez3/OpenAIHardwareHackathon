"""State machine for a simulated bedside check-in."""

from dataclasses import dataclass
import logging
import time
from uuid import uuid4


LOG = logging.getLogger(__name__)
PAIN_QUESTION = "Are you experiencing pain?"
POSITION_QUESTION = "Would you like help changing your position?"
QUESTION_STAGES = {"pain", "position"}
DELIVERY_TIMEOUT = 7
ANSWER_HINT_DELAY = 9
CAMERA_ANSWER_WINDOW = 14


@dataclass(frozen=True)
class Screen:
    title: str
    text: str
    left_label: str = ""
    left_value: str = ""
    right_label: str = ""
    right_value: str = ""
    status: str = ""


class CheckIn:
    def __init__(self, send, speak, *, camera=False, clock=time.monotonic):
        self.send = send
        self.speak = speak
        self.camera = camera
        self.clock = clock
        self.online = set()
        self.run_id = uuid4().hex[:8]
        self.prompt_number = 0
        self.request_id = ""
        self.message = ""
        self.pain = False
        self.no_answer_hint = False
        self.camera_prompt = ""
        self.camera_ready = False
        self.camera_failed = False
        self.stage = "ready"
        self.entered = self.clock()
        self.screen = self._screen()

    @property
    def prompt_id(self):
        return f"{self.run_id}-{self.prompt_number}"

    def handle(self, role, event):
        kind = event.get("type")
        if kind == "hello":
            self.online.add(role)
            LOG.info("%s connected", role.capitalize())
            if role == "patient":
                self._update_screen(reset_input=True)
            return

        if kind == "disconnected":
            self.online.discard(role)
            LOG.info("%s disconnected", role.capitalize())
            if role == "patient":
                self.camera_prompt = ""
                self.camera_ready = False
            elif self.stage in {"sending", "delivered"}:
                self._enter("retry")
            return

        if role not in self.online:
            return

        if role == "patient" and event.get("prompt_id") == self.prompt_id:
            if kind == "input":
                self._answer(event.get("value"), event.get("source", "touch"))
            elif kind == "camera_ready" and self.camera_prompt == self.prompt_id:
                self.camera_ready = True
                self._update_screen()
            elif kind == "camera_error":
                error = event.get("error", "capture failed")
                self._stop_camera()
                if error == "camera_timeout":
                    self.no_answer_hint = True
                else:
                    LOG.warning("CoreS3 camera unavailable: %s", error)
                    self.camera_failed = True
                self._update_screen()
        elif role == "caregiver" and event.get("request_id") == self.request_id:
            if self.stage == "sending" and kind == "displayed":
                self._enter("delivered")
            elif self.stage == "delivered" and kind == "acknowledged":
                self._enter("acknowledged")

    def tick(self):
        elapsed = self.clock() - self.entered
        if self.stage == "sending" and elapsed >= DELIVERY_TIMEOUT:
            self._enter("retry")
        if self.stage not in QUESTION_STAGES:
            return
        answer_window = CAMERA_ANSWER_WINDOW if self.camera and not self.camera_failed else ANSWER_HINT_DELAY
        if (
            self.camera and not self.camera_failed and "patient" in self.online
            and not self.no_answer_hint and not self.camera_prompt and 2.3 <= elapsed < answer_window
        ):
            if self.send("patient", {"type": "camera", "enabled": True, "prompt_id": self.prompt_id}):
                self.camera_prompt = self.prompt_id
                self._update_screen()
        if not self.no_answer_hint and elapsed >= answer_window:
            self._stop_camera()
            self.no_answer_hint = True
            self._update_screen()

    def _answer(self, value, source):
        if not isinstance(value, str):
            return
        if source == "camera" and self.camera_prompt != self.prompt_id:
            return
        if (self.stage, value) in {("ready", "start"), ("empty", "start"), ("acknowledged", "start")}:
            if self.stage == "acknowledged":
                self.send("caregiver", {"type": "clear_request", "request_id": self.request_id})
            self.request_id = ""
            self.message = ""
            self.pain = False
            self._enter("pain")
        elif self.stage == "pain" and value in {"yes", "no"}:
            LOG.info("Pain: %s (%s)", value, source)
            self.pain = value == "yes"
            self._enter("position")
        elif self.stage == "position" and value in {"yes", "no"}:
            LOG.info("Position help: %s (%s)", value, source)
            position = value == "yes"
            if self.pain and position:
                self.message = "I'm in pain and would like help changing my position."
            elif self.pain:
                self.message = "I'm in pain."
            elif position:
                self.message = "I'd like help changing my position."
            self._enter("review" if self.message else "empty")
        elif self.stage == "review" and value == "cancel":
            self.message = ""
            self._enter("ready")
        elif self.stage == "review" and value == "send":
            self.request_id = uuid4().hex[:12]
            self._deliver()
        elif self.stage == "retry" and value == "retry":
            self._deliver()

    def _deliver(self):
        packet = {"type": "show_request", "request_id": self.request_id, "text": self.message}
        if "caregiver" in self.online and self.send("caregiver", packet):
            self._enter("sending")
        else:
            self._enter("retry")

    def _stop_camera(self):
        if self.camera_prompt:
            self.send("patient", {"type": "camera", "enabled": False, "prompt_id": self.camera_prompt})
            self.camera_prompt = ""
        self.camera_ready = False

    def _enter(self, stage):
        self._stop_camera()
        self.stage = stage
        self.prompt_number += 1
        self.entered = self.clock()
        self.no_answer_hint = False
        self._update_screen()
        if stage in QUESTION_STAGES:
            self.speak(self.screen.text)
        elif stage == "review":
            self.speak(self.message, exact=True)

    def _update_screen(self, *, reset_input=False):
        self.screen = self._screen()
        LOG.info("%s | %s%s", self.screen.title, self.screen.text,
                 f" [{self.screen.status}]" if self.screen.status else "")
        if "patient" in self.online:
            packet = {"type": "screen", "prompt_id": self.prompt_id, **vars(self.screen)}
            if reset_input:
                packet["reset_input"] = True
            self.send("patient", packet)

    def _screen(self):
        if self.stage in QUESTION_STAGES:
            if self.no_answer_hint:
                hint = "No answer yet. Use touch anytime."
            elif self.camera_ready:
                hint = "Nod for Yes. Shake for No."
            elif self.camera_prompt:
                hint = "Face the camera and hold still."
            elif self.stage == "position":
                hint = "Pain recorded: yes." if self.pain else "Pain recorded: no."
            else:
                hint = "Choose Yes or No."
            question = PAIN_QUESTION if self.stage == "pain" else POSITION_QUESTION
            title = "Pain" if self.stage == "pain" else "Position"
            return Screen(title, question, "No", "no", "Yes", "yes", hint)
        if self.stage == "review":
            return Screen("Review message", self.message, "Cancel", "cancel", "Send", "send",
                          "Send only if the wording is right.")
        if self.stage == "sending":
            return Screen("Sending", self.message, status="Waiting for the caregiver display.")
        if self.stage == "delivered":
            return Screen("Message delivered", self.message, status="Waiting for acknowledgment.")
        if self.stage == "acknowledged":
            return Screen("Caregiver acknowledged", self.message, "New check-in", "start")
        if self.stage == "retry":
            return Screen("Delivery unconfirmed", self.message, "Retry", "retry",
                          status="Check the caregiver USB connection.")
        if self.stage == "empty":
            return Screen("Check-in complete", "No message was sent.", "New check-in", "start")
        return Screen("Bedside check-in", "Answer two questions, then review your message.",
                      "Start", "start")
