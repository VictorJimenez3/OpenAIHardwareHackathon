"""State machine for a simulated bedside check-in."""

from dataclasses import dataclass
import logging
import re
import time
from uuid import uuid4


LOG = logging.getLogger(__name__)
PAIN_QUESTION = "Are you experiencing pain?"
POSITION_QUESTION = "Would you like help changing your position?"
FOLLOWUP_QUESTIONS = {
    "reposition": POSITION_QUESTION,
    "pain_check": "Would you like someone to check on your pain?",
    "mouth_care": "Would you like help with mouth care?",
}
FOLLOWUP_MESSAGES = {
    "reposition": "help changing my position",
    "pain_check": "someone to check on my pain",
    "mouth_care": "mouth care",
}
SCENARIO_CONTEXT = {
    "comfort": "Simulated handoff: the patient has been in the same position for several hours. "
               "They previously indicated that shifting position was helpful.",
    "pain": "Simulated handoff: the caregiver needs the patient's own report about whether they feel pain. "
            "A confirmed yes is sufficient to draft that report for review.",
    "mouth": "Simulated handoff: the patient signaled dry mouth during an earlier check-in. "
             "The caregiver is checking whether they want mouth care now.",
}
SCENARIO_FALLBACK = {
    "comfort": "reposition",
    "pain": "review_now",
    "mouth": "mouth_care",
}
QUESTION_STAGES = {"pain", "position", "followup", "handoff_question"}
DELIVERY_TIMEOUT = 7
ANSWER_HINT_DELAY = 9
CAMERA_ANSWER_WINDOW = 14
PLANNER_TIMEOUT = 3


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
    def __init__(
        self,
        send,
        speak,
        *,
        camera=False,
        clock=time.monotonic,
        adaptive=False,
        scenario="pain",
        planner=None,
        handoff=None,
        handoff_planner=None,
        gesture=False,
    ):
        self.send = send
        self.speak = speak
        self.camera = camera
        self.clock = clock
        self.adaptive = bool(adaptive)
        self.scenario = scenario if scenario in SCENARIO_CONTEXT else "pain"
        # ``planner`` is a non-blocking submit callback supplied by __main__.
        self.planner = planner
        self.handoff_planner = handoff_planner
        self.handoff_context = str(handoff or "").strip()[:800]
        self.handoff_mode = bool(self.handoff_context)
        self.handoff_declined = ()
        self.handoff_attempts = 0
        self.handoff_phrase = ""
        self.handoff_followup_kind = ""
        self.gesture = bool(gesture)
        self.gesture_ready = False
        self.online = set()
        self.run_id = uuid4().hex[:8]
        self.prompt_number = 0
        self.request_id = ""
        self.message = ""
        self.pain = False
        self.confirmed = {}
        self.followup_id = ""
        self.followup_source = ""
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
                if self.gesture:
                    self.send("patient", {"type": "gesture", "enabled": True})
                    LOG.info("Gesture input: opt-in requested")
                self._update_screen(reset_input=True)
            return

        if kind == "disconnected":
            self.online.discard(role)
            LOG.info("%s disconnected", role.capitalize())
            if role == "patient":
                self.camera_prompt = ""
                self.camera_ready = False
                self.gesture_ready = False
            elif self.stage in {"sending", "delivered"}:
                self._enter("retry")
            return

        # Planner results are host-local events, so they do not require a USB
        # role to be connected. The prompt guard rejects stale model results.
        if role == "adaptive":
            if event.get("prompt_id") != self.prompt_id:
                return
            if kind == "question_choice" and self.stage == "choosing":
                self._apply_followup(event.get("question_id"), event.get("source", "local"))
            elif kind == "handoff_draft" and self.stage == "handoff_choosing":
                self._apply_handoff_draft(event.get("phrase"), event.get("source", "live"))
            elif kind == "pain_followup_draft" and self.stage == "handoff_pain_choosing":
                self._apply_pain_followup(event.get("draft"))
            return

        if role not in self.online:
            return

        if role == "patient":
            if kind == "gesture_ready" and self.gesture:
                self.gesture_ready = True
                LOG.info("Gesture input ready")
                self._update_screen()
            elif kind == "gesture_error" and self.gesture:
                self.gesture_ready = False
                LOG.warning("Gesture input unavailable: %s", event.get("error", "unknown error"))
                self._update_screen()
            elif event.get("prompt_id") == self.prompt_id:
                if kind == "input":
                    if event.get("source") == "gesture" and (
                        not self.gesture_ready or event.get("value") not in {"start", "yes", "no"}
                    ):
                        return
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
        if self.stage == "choosing" and elapsed >= PLANNER_TIMEOUT:
            self._apply_followup(None, "local")
        if self.stage == "handoff_choosing" and elapsed >= PLANNER_TIMEOUT:
            self._handoff_no_message()
        if self.stage == "handoff_pain_choosing" and elapsed >= PLANNER_TIMEOUT:
            self._pain_only_review()
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
        if value == "send" and source != "touch":
            return
        if source == "camera" and self.camera_prompt != self.prompt_id:
            return
        if source == "gesture" and (
            not self.gesture_ready or value not in {"start", "yes", "no"}
        ):
            return
        if (self.stage, value) in {("ready", "start"), ("empty", "start"), ("acknowledged", "start")}:
            if self.stage == "acknowledged":
                self.send("caregiver", {"type": "clear_request", "request_id": self.request_id})
            self.request_id = ""
            self.message = ""
            self.pain = False
            self.confirmed = {}
            self.followup_id = ""
            self.followup_source = ""
            self.handoff_followup_kind = ""
            if self.handoff_mode:
                self.handoff_declined = ()
                self.handoff_attempts = 0
                self.handoff_phrase = ""
                self._enter("pain")
            else:
                self._enter("pain")
        elif self.stage == "pain" and value in {"yes", "no"}:
            LOG.info("Pain: %s (%s)", value, source)
            self.pain = value == "yes"
            self.confirmed["pain"] = self.pain
            if self.handoff_mode and self.pain:
                self._enter("handoff_pain_choosing")
                self._request_pain_followup()
            elif self.handoff_mode:
                self._enter("handoff_choosing")
                self._request_handoff()
            elif self.adaptive:
                self._enter("choosing")
                self._request_followup()
            else:
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
        elif self.stage == "followup" and value in {"yes", "no"}:
            answer = value == "yes"
            self.confirmed[self.followup_id] = answer
            LOG.info("Adaptive follow-up %s: %s (%s)", self.followup_id, value, source)
            self.message = self._adaptive_message(answer)
            self._enter("review" if self.message else "empty")
        elif self.stage == "handoff_question" and value in {"yes", "no"}:
            if value == "yes":
                if self.handoff_followup_kind == "location":
                    self.message = f"I have pain in my {self.handoff_phrase}."
                elif self.handoff_followup_kind == "request":
                    if self.pain:
                        self.message = f"I'm in pain and would like {self.handoff_phrase}."
                    else:
                        self.message = f"I'd like {self.handoff_phrase}."
                else:
                    self.message = f"I'd like {self.handoff_phrase}."
                self._enter("review")
            else:
                if self.pain:
                    self.message = "I'm in pain."
                    self._enter("review")
                else:
                    self.message = ""
                    self._enter("empty")
        elif self.stage == "handoff_empty":
            if value == "no_message":
                self.message = ""
                self._enter("empty")
        elif self.stage == "review" and value == "cancel":
            self.message = ""
            self._enter("ready")
        elif self.stage == "review" and value == "send":
            self.request_id = uuid4().hex[:12]
            self._deliver()
        elif self.stage == "retry" and value == "retry":
            self._deliver()

    def _request_handoff(self):
        """Ask the bounded planner for one request phrase in the background."""

        planner = self.handoff_planner or self.planner
        if not planner or self.handoff_attempts >= 3:
            self._handoff_no_message()
            return
        self.handoff_attempts += 1
        try:
            submit = getattr(planner, "submit_handoff", None)
            if submit is None:
                raise AttributeError("submit_handoff")
            submit(self.prompt_id, self.handoff_context, tuple(self.handoff_declined))
        except AttributeError:
            LOG.warning("Handoff planner unavailable; showing no-message choice")
            self._handoff_no_message()
        except Exception:
            LOG.warning("Handoff planner submission failed; showing no-message choice")
            self._handoff_no_message()

    def _request_pain_followup(self):
        planner = self.handoff_planner or self.planner
        if not planner:
            self._pain_only_review()
            return
        try:
            submit = getattr(planner, "submit_pain_followup", None)
            if submit is None:
                raise AttributeError("submit_pain_followup")
            submit(self.prompt_id, self.handoff_context)
        except Exception:
            LOG.warning("Pain follow-up planner unavailable; using pain-only review")
            self._pain_only_review()

    @staticmethod
    def _validate_handoff_phrase(value):
        if not isinstance(value, str):
            return None
        phrase = " ".join(value.strip().split()).lower()
        # The model supplies content only; punctuation and non-ASCII output
        # are rejected so both displayed question and sent body stay exact.
        if not phrase or len(phrase) > 52 or not phrase.isascii():
            return None
        if not re.fullmatch(r"[a-z0-9]+(?: [a-z0-9]+)*", phrase):
            return None
        return phrase

    def _apply_handoff_draft(self, phrase, source):
        if self.stage != "handoff_choosing":
            return
        phrase = self._validate_handoff_phrase(phrase)
        if phrase is None or phrase in self.handoff_declined:
            LOG.info("Handoff planner returned no distinct grounded phrase")
            self._handoff_no_message()
            return
        self.handoff_phrase = phrase
        self.handoff_followup_kind = "request"
        LOG.info("Handoff phrase selected (%s)", "Live planner" if source == "live" else "local")
        self._enter("handoff_question")

    def _apply_pain_followup(self, draft):
        if self.stage != "handoff_pain_choosing" or not isinstance(draft, dict):
            self._pain_only_review()
            return
        kind = draft.get("kind")
        phrase = self._validate_handoff_phrase(draft.get("phrase"))
        if kind not in {"location", "request"} or phrase is None:
            LOG.info("Pain planner returned no grounded follow-up")
            self._pain_only_review()
            return
        self.handoff_followup_kind = kind
        self.handoff_phrase = phrase
        LOG.info("Pain follow-up selected: %s", kind)
        self._enter("handoff_question")

    def _pain_only_review(self):
        if self.stage != "handoff_pain_choosing":
            return
        self.handoff_phrase = ""
        self.handoff_followup_kind = ""
        self.message = "I'm in pain."
        self._enter("review")

    def _handoff_no_message(self):
        if self.stage not in {"handoff_choosing", "handoff_question"}:
            return
        self.handoff_phrase = ""
        self.message = ""
        self._enter("handoff_empty")

    def _followup_options(self):
        candidates = dict(FOLLOWUP_QUESTIONS)
        if self.pain:
            candidates["review_now"] = "Review the patient's confirmed pain report now."
        else:
            del candidates["pain_check"]
        return candidates

    def _request_followup(self):
        if not self.planner:
            self._apply_followup(None, "local")
            return
        try:
            self.planner(self.prompt_id, self._followup_options(), dict(self.confirmed),
                         SCENARIO_CONTEXT[self.scenario])
        except Exception:
            LOG.warning("Adaptive planner submission failed; using local fallback")
            self._apply_followup(None, "local")

    def _apply_followup(self, question_id, source):
        if self.stage != "choosing":
            return
        options = self._followup_options()
        if not isinstance(question_id, str) or question_id not in options:
            question_id = SCENARIO_FALLBACK[self.scenario]
            if question_id not in options:
                question_id = "reposition"
            source = "local"
        source = "live" if source == "live" else "local"
        LOG.info("Adaptive choice: %s (%s)", question_id, "model" if source == "live" else "local")
        if question_id == "review_now":
            self.message = "I'm in pain."
            self._enter("review")
            return
        self.followup_id = question_id
        self.followup_source = source
        self._enter("followup")

    def _adaptive_message(self, followup_answer):
        """Build only from explicit patient answers; never infer urgency."""

        if not followup_answer:
            return "I'm in pain." if self.confirmed.get("pain") else ""
        tail = FOLLOWUP_MESSAGES[self.followup_id]
        if self.confirmed.get("pain"):
            return f"I'm in pain and would like {tail}."
        return f"I'd like {tail}."

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
            elif self.gesture_ready:
                hint = "Swipe right Yes, left No."
            elif self.camera_ready:
                hint = "Nod for Yes. Shake for No."
            elif self.camera_prompt:
                hint = "Face the camera and hold still."
            elif self.stage == "position":
                hint = "Pain recorded: yes." if self.pain else "Pain recorded: no."
            else:
                hint = "Choose Yes or No."
            if self.stage == "followup":
                question = FOLLOWUP_QUESTIONS[self.followup_id]
                title = "Adaptive follow-up"
                if not self.no_answer_hint and not self.camera_ready and not self.camera_prompt:
                    hint = "AI selected this question." if self.followup_source == "live" else "Choose Yes or No."
            elif self.stage == "handoff_question":
                if self.handoff_followup_kind == "location":
                    question = f"Is the pain in your {self.handoff_phrase}?"
                else:
                    question = f"Would you like {self.handoff_phrase}?"
                title = "Care handoff"
                if not self.no_answer_hint and not self.camera_ready and not self.camera_prompt:
                    hint = "Confirm only if this is what you need."
            else:
                question = PAIN_QUESTION if self.stage == "pain" else POSITION_QUESTION
                title = "Pain" if self.stage == "pain" else "Position"
            return Screen(title, question, "No", "no", "Yes", "yes", hint)
        if self.stage == "choosing":
            return Screen("Choosing a follow-up", "Checking the care context.")
        if self.stage == "handoff_pain_choosing":
            return Screen("Choosing a pain follow-up", "Checking one grounded detail.")
        if self.stage == "handoff_choosing":
            return Screen("Drafting a request", "Checking the caregiver note.")
        if self.stage == "handoff_empty":
            return Screen("No message drafted", "No grounded request was found.",
                          "No message", "no_message",
                          "Nothing will be sent unless you confirm a request.")
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
        subtitle = (
            "Start with a pain check, then review one grounded follow-up."
            if self.handoff_mode
            else "Answer two questions, then review your message."
        )
        return Screen("Bedside check-in", subtitle, "Start", "start")
