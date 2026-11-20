"""State machine for a simulated bedside check-in."""

from dataclasses import dataclass, replace
import logging
import re
import time
from uuid import uuid4

from .pain_location import (
    CONFIRM_STAGE,
    FIRST_STAGE,
    PROMPTS,
    confirmation_question,
    next_location_stage,
    prompt_for,
    update_message,
)


LOG = logging.getLogger(__name__)
PAIN_QUESTION = "Are you experiencing pain?"
POSITION_QUESTION = "Would you like help changing your position?"
OTHER_NEED_QUESTION = "Do you need help with something else?"
PLAIN_PAIN_MESSAGE = "I'm in pain."
OTHER_NEED_MESSAGE = "I need help with something else."
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
LOCATION_STAGES = set(PROMPTS) | {CONFIRM_STAGE}
QUESTION_STAGES = {"pain", "position", "followup", "handoff_question", "other_need"} | LOCATION_STAGES
DELIVERY_TIMEOUT = 7
ANSWER_HINT_DELAY = 9
CAMERA_ARM_DELAY = 0.75
ANSWER_RECEIPT_SECONDS = 2.5
LOCATION_IDLE_TIMEOUT = 25
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
    receipt: str = ""


@dataclass(frozen=True)
class RecordedAnswer:
    value: str
    source: str
    advance_at: float


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
        progressive=False,
        screen_changed=None,
    ):
        self.send = send
        self.speak = speak
        self.screen_changed = screen_changed
        self.camera = camera
        self.clock = clock
        self.adaptive = bool(adaptive)
        self.scenario = scenario if scenario in SCENARIO_CONTEXT else "pain"
        # ``planner`` is a non-blocking submit callback supplied by __main__.
        self.planner = planner
        self.handoff_planner = handoff_planner
        self.handoff_context = str(handoff or "").strip()[:800]
        self.handoff_mode = bool(self.handoff_context)
        self.progressive = bool(progressive and not (adaptive or self.handoff_mode))
        self.handoff_declined = ()
        self.handoff_attempts = 0
        self.handoff_phrase = ""
        self.handoff_followup_kind = ""
        self.gesture = bool(gesture)
        self.gesture_ready = False
        self.online = set()
        self.patient_connection = None
        self.run_id = uuid4().hex[:8]
        self.prompt_number = 0
        self.report_id = ""
        self.revision = 0
        self.request_id = ""
        self.initial_request_id = ""
        self.update_request_id = ""
        self.initial_acknowledged = False
        self.location = ""
        self.message = ""
        self.pain = False
        self.confirmed = {}
        self.followup_id = ""
        self.followup_source = ""
        self.no_answer_hint = False
        self.camera_streaming = False
        self.camera_scope = None
        self.camera_generation = 0
        self.camera_arm_at = 0
        self.camera_ready = False
        self.camera_failed = False
        self.receipt = ""
        self.recorded_answer: RecordedAnswer | None = None
        self.stage = "ready"
        self.entered = self.clock()
        self.screen = self._screen()
        self.rendered_prompt = self.prompt_id

    @property
    def prompt_id(self):
        return f"{self.run_id}-{self.prompt_number}"

    def handle(self, role, event):
        kind = event.get("type")
        if kind == "hello":
            if role in self.online and (
                role != "patient" or event.get("connection") is self.patient_connection
            ):
                return
            self.online.add(role)
            LOG.info("%s connected", role.capitalize())
            if role == "patient":
                self.patient_connection = event.get("connection")
                self._disarm_camera()
                self.camera_failed = False
                if self.camera:
                    self.camera_streaming = self.send("patient", {
                        "type": "camera", "enabled": True, "prompt_id": self.prompt_id,
                    })
                    self.camera_arm_at = self.clock() + CAMERA_ARM_DELAY
                if self.gesture:
                    self.send("patient", {"type": "gesture", "enabled": True})
                    LOG.info("Gesture input: opt-in requested")
                self._update_screen(reset_input=True)
            return

        if kind == "disconnected":
            self.online.discard(role)
            LOG.info("%s disconnected", role.capitalize())
            if role == "patient":
                self.patient_connection = None
                self.camera_streaming = False
                self._disarm_camera()
                self.gesture_ready = False
                self._update_screen()
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

        if role == "camera":
            self._handle_camera_event(kind, event)
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
            elif kind == "camera_error" and self.camera_streaming:
                # Firmware capture errors belong to the stream, which may
                # have moved to a different screen before this event arrived.
                self.camera_streaming = False
                self._camera_error(event)
            elif event.get("prompt_id") == self.prompt_id:
                if kind == "input" and event.get("source", "touch") in ("touch", "gesture"):
                    self._answer(event.get("value"), event.get("source", "touch"))
        elif role == "caregiver":
            self._handle_caregiver_event(kind, event)

    def _handle_camera_event(self, kind, event):
        scope = (event.get("prompt_id"), event.get("generation"))
        if "patient" not in self.online or self.stage not in QUESTION_STAGES or scope != self.camera_scope:
            return
        if kind == "camera_ready":
            ready = event.get("ready", True)
            if ready != self.camera_ready:
                self.camera_ready = ready
                self._update_screen()
        elif kind == "camera_error":
            self._camera_error(event)
        elif kind == "input" and self.camera_ready and event.get("value") in ("yes", "no"):
            source = event.get("source", "camera")
            if source in ("camera", "camera_hand"):
                self._answer(event["value"], source)

    def _handle_caregiver_event(self, kind, event):
        """Handle display/ack events without conflating report revisions.

        Legacy firmware echoes only ``request_id``.  Newer hosts include the
        logical report and revision, but those fields are optional on input so
        an existing StickS3 remains usable.
        """

        request_id = event.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return
        expected_revision = None
        if request_id == self.initial_request_id:
            expected_revision = 0
        elif request_id == self.update_request_id:
            expected_revision = 1
        if expected_revision is None:
            return
        if event.get("report_id") not in (None, self.report_id):
            return
        if event.get("revision") not in (None, expected_revision):
            return

        if kind == "acknowledged":
            if expected_revision == 0:
                self.initial_acknowledged = True
                # The first report is intentionally acknowledged out of band
                # while the patient answers location questions.  Never let
                # that stale/early ack advance the location tree or mark a
                # later update delivered.
                if self.stage in {"delivered", "plain_sent"} and self.revision == 0:
                    self._enter("acknowledged")
            elif self.stage == "delivered" and self.revision == 1:
                self._enter("acknowledged")
            return

        if kind != "displayed" or request_id != self.request_id:
            return
        if self.stage != "sending":
            return
        if self.progressive and self.pain and expected_revision == 0:
            # The location dialogue starts from a confirmed display, not from
            # caregiver acknowledgment.  The initial report remains active
            # on the Stick until an optional, explicitly confirmed update is
            # approved.
            self._enter(FIRST_STAGE)
        else:
            self._enter("delivered")
            # A reconnecting legacy caregiver can theoretically replay its
            # acknowledgment before the host receives the display event.  Do
            # not leave that already-acknowledged revision stuck in delivery.
            if expected_revision == 0 and self.initial_acknowledged:
                self._enter("acknowledged")

    def tick(self):
        now = self.clock()
        if self.recorded_answer is not None:
            recorded = self.recorded_answer
            if now >= recorded.advance_at:
                self.recorded_answer = None
                self._apply_answer(recorded.value, recorded.source)
            return
        elapsed = now - self.entered
        if self.stage == "sending" and elapsed >= DELIVERY_TIMEOUT:
            self._enter("retry")
        if self.stage == "choosing" and elapsed >= PLANNER_TIMEOUT:
            self._apply_followup(None, "local")
        if self.stage == "handoff_choosing" and elapsed >= PLANNER_TIMEOUT:
            self._handoff_no_message()
        if self.stage == "handoff_pain_choosing" and elapsed >= PLANNER_TIMEOUT:
            self._pain_only_review()
        if not self.camera and self.stage in LOCATION_STAGES and elapsed >= LOCATION_IDLE_TIMEOUT:
            self._keep_plain_report()
        if self.stage not in QUESTION_STAGES:
            return
        if self.camera:
            if (
                self.camera_streaming and not self.camera_failed and self.camera_scope is None
                and now >= self.camera_arm_at
            ):
                self.camera_generation += 1
                self.camera_scope = (self.prompt_id, self.camera_generation)
        elif not self.no_answer_hint and elapsed >= ANSWER_HINT_DELAY:
            self.no_answer_hint = True
            self._update_screen()

    def _answer(self, value, source):
        if self.recorded_answer is not None:
            return
        if not isinstance(value, str) or source not in ("touch", "camera", "camera_hand", "gesture"):
            return
        if (value == "send" or (self.camera and value == "start")) and source != "touch":
            return
        if source == "gesture" and (
            not self.gesture_ready or value not in {"start", "yes", "no"}
        ):
            return
        if value in {"yes", "no"} and self.stage in QUESTION_STAGES:
            self._record_answer(value, source)
            return
        self._apply_answer(value, source)

    def _apply_answer(self, value, source):
        if value == "start" and self.stage in {
            "ready", "empty", "acknowledged",
        }:
            self._start_new_checkin()
        elif self.stage == "pain" and value in {"yes", "no"}:
            LOG.info("Pain: %s (%s)", value, source)
            self.pain = value == "yes"
            self.confirmed["pain"] = self.pain
            if self.progressive and self.pain:
                self.message = PLAIN_PAIN_MESSAGE
                self._enter("review")
            elif self.progressive:
                self._enter("other_need")
            elif self.handoff_mode and self.pain:
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
        elif self.stage == "other_need" and value in {"yes", "no"}:
            # A No is an actual answer and ends the check-in.  A Yes gets a
            # deliberately generic sentence; it must not guess a symptom,
            # location, or requested intervention.
            if value == "yes":
                self.message = OTHER_NEED_MESSAGE
                self._enter("review")
            else:
                self.message = ""
                self._enter("empty")
        elif self.stage in PROMPTS and value in {"yes", "no"}:
            next_stage, location = next_location_stage(self.stage, value)
            if next_stage is None:
                return
            if location is not None:
                self.location = location
            if next_stage == "review":
                self._review_location()
            else:
                self._enter(next_stage)
        elif self.stage == CONFIRM_STAGE and value in {"yes", "no"}:
            if value == "yes" and self.location:
                self._review_location()
            else:
                self._keep_plain_report()
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
            if self.progressive and self.revision == 1:
                self._keep_plain_report()
            else:
                self.message = ""
                self._enter("ready")
        elif self.stage == "review" and value == "send":
            self._prepare_delivery_request()
            self._deliver()
        elif self.stage == "retry" and value == "retry":
            self._deliver()

    def _record_answer(self, value, source):
        self._disarm_camera()
        answer = value.capitalize()
        self.recorded_answer = RecordedAnswer(value, source, self.clock() + ANSWER_RECEIPT_SECONDS)
        if source == "gesture":
            method, label = "Swipe accepted", "Swipe"
        elif source == "camera_hand":
            method = label = "Hand accepted"
        elif source == "camera":
            method = label = "Nod accepted" if value == "yes" else "Shake accepted"
        else:
            method, label = "Touch accepted", "Touch"
        self.receipt = f"{label}: {answer}"
        if source != "gesture":
            self.send("patient", {
                "type": "answer_feedback", "prompt_id": self.prompt_id,
                "answer": answer, "method": method,
            })
        self._update_screen()
        self.speak(f"{answer} recorded.", exact=True)

    def _start_new_checkin(self):
        """Reset all report/revision state after an explicit new check-in."""

        old_request_id = self.update_request_id or self.initial_request_id or self.request_id
        if old_request_id:
            old_revision = 1 if old_request_id == self.update_request_id else 0
            self.send("caregiver", {
                "type": "clear_request",
                "request_id": old_request_id,
                "report_id": self.report_id,
                "revision": old_revision,
            })
        self.run_id = uuid4().hex[:8]
        self.prompt_number = 0
        self.report_id = uuid4().hex[:12]
        self.revision = 0
        self.request_id = ""
        self.initial_request_id = ""
        self.update_request_id = ""
        self.initial_acknowledged = False
        self.message = ""
        self.pain = False
        self.confirmed = {}
        self.followup_id = ""
        self.followup_source = ""
        self.handoff_followup_kind = ""
        self.location = ""
        if self.handoff_mode:
            self.handoff_declined = ()
            self.handoff_attempts = 0
            self.handoff_phrase = ""
        self._enter("pain")

    def _review_location(self):
        self.message = update_message(self.location)
        self.revision = 1
        self.request_id = ""
        self.update_request_id = ""
        self._enter("review")

    def _keep_plain_report(self):
        """Finish optional narrowing without replacing the explicit alert."""

        self.location = ""
        self.revision = 0
        self.message = PLAIN_PAIN_MESSAGE
        self.request_id = self.initial_request_id
        self.update_request_id = ""
        self._enter("acknowledged" if self.initial_acknowledged else "plain_sent")

    def _prepare_delivery_request(self):
        """Assign one wire ID to the current approved report revision.

        A retry reuses ``request_id``.  A new approved revision gets a new
        request ID while retaining the same logical ``report_id``.
        """

        if self.request_id:
            return
        self.request_id = uuid4().hex[:12]
        if self.revision == 0:
            self.initial_request_id = self.request_id
        elif self.revision == 1:
            self.update_request_id = self.request_id

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
        self.message = PLAIN_PAIN_MESSAGE
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
            self.message = PLAIN_PAIN_MESSAGE
            self._enter("review")
            return
        self.followup_id = question_id
        self.followup_source = source
        self._enter("followup")

    def _adaptive_message(self, followup_answer):
        """Build only from explicit patient answers; never infer urgency."""

        if not followup_answer:
            return PLAIN_PAIN_MESSAGE if self.confirmed.get("pain") else ""
        tail = FOLLOWUP_MESSAGES[self.followup_id]
        if self.confirmed.get("pain"):
            return f"I'm in pain and would like {tail}."
        return f"I'd like {tail}."

    def _deliver(self):
        packet = {
            "type": "show_request",
            "request_id": self.request_id,
            "report_id": self.report_id,
            "revision": self.revision,
            "text": self.message,
        }
        if "caregiver" in self.online and self.send("caregiver", packet):
            self._enter("sending")
        else:
            self._enter("retry")

    def _disarm_camera(self):
        self.camera_scope = None
        self.camera_ready = False

    def _camera_error(self, event):
        LOG.warning("CoreS3 camera unavailable: %s", event.get("error", "recognition failed"))
        self.camera_failed = True
        self._disarm_camera()
        self._update_screen()

    def stop_camera_stream(self):
        self._disarm_camera()
        self.camera_streaming = False
        if self.camera:
            self.send("patient", {"type": "camera", "enabled": False, "prompt_id": self.prompt_id})

    def _enter(self, stage):
        self._disarm_camera()
        self.recorded_answer = None
        self.receipt = ""
        self.stage = stage
        self.prompt_number += 1
        self.entered = self.clock()
        self.camera_arm_at = self.entered + CAMERA_ARM_DELAY
        self.no_answer_hint = False
        self._update_screen()
        if stage in QUESTION_STAGES:
            self.speak(self.screen.text)
        elif stage == "review":
            self.speak(f"Your message is: {self.message} {self.screen.status}", exact=True)
        elif stage == "empty":
            self.speak(self.screen.text, exact=True)

    def _update_screen(self, *, reset_input=False):
        screen = replace(self._screen(), receipt=self.receipt)
        if not reset_input and screen == self.screen and self.rendered_prompt == self.prompt_id:
            return
        self.screen = screen
        self.rendered_prompt = self.prompt_id
        if self.screen_changed:
            self.screen_changed(screen)
        LOG.info("%s | %s%s", self.screen.title, self.screen.text,
                 f" [{self.screen.status}]" if self.screen.status else "")
        if "patient" in self.online:
            packet = {"type": "screen", "prompt_id": self.prompt_id, **vars(self.screen)}
            if reset_input:
                packet["reset_input"] = True
            self.send("patient", packet)

    def _screen(self):
        if self.recorded_answer is not None:
            # Keep the answered question in view until its receipt expires.
            # Clearing the choices also makes a reconnect during the hold inert.
            return Screen(self.screen.title, self.screen.text, status="Continuing automatically.")
        if self.stage in QUESTION_STAGES:
            if self.no_answer_hint:
                hint = "No answer yet. Use touch anytime."
            elif self.camera_ready:
                hint = "Yes: nod or hand up/down. No: shake or hand sideways."
            elif self.camera_streaming and not self.camera_failed:
                hint = "Hold your face or one hand still."
            elif self.gesture_ready:
                hint = "Swipe right Yes, left No."
            elif self.stage == "position":
                hint = "Pain recorded: yes." if self.pain else "Pain recorded: no."
            else:
                hint = "Choose Yes or No."
            if self.stage == "followup":
                question = FOLLOWUP_QUESTIONS[self.followup_id]
                title = "Adaptive follow-up"
                if not self.no_answer_hint and not self.camera_streaming:
                    hint = "AI selected this question." if self.followup_source == "live" else "Choose Yes or No."
            elif self.stage == "handoff_question":
                if self.handoff_followup_kind == "location":
                    question = f"Is the pain in your {self.handoff_phrase}?"
                else:
                    question = f"Would you like {self.handoff_phrase}?"
                title = "Care handoff"
                if not self.no_answer_hint and not self.camera_streaming:
                    hint = "Confirm only if this is what you need."
            elif self.progressive and self.stage == "other_need":
                question = OTHER_NEED_QUESTION
                title = "Other help"
            elif self.progressive and self.stage == CONFIRM_STAGE:
                question = confirmation_question(self.location)
                title = "Confirm location"
                if not self.no_answer_hint and not self.camera_streaming:
                    hint = "Confirm only if this location is correct."
            elif self.progressive and (prompt := prompt_for(self.stage)) is not None:
                question = prompt.text
                title = "Optional pain detail"
                if not self.camera_streaming and not self.no_answer_hint and not self.gesture_ready:
                    hint = "Pain alert sent. Location is optional."
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
                          "Tap Send if this is right.")
        if self.stage == "sending":
            return Screen("Sending", self.message, status="Waiting for the caregiver display.")
        if self.stage == "delivered":
            title = "Location update delivered" if self.progressive and self.revision == 1 else "Message delivered"
            return Screen(title, self.message, status="Waiting for acknowledgment.")
        if self.stage == "acknowledged":
            return Screen("Caregiver acknowledged", self.message, "New check-in", "start")
        if self.stage == "retry":
            return Screen("Delivery unconfirmed", self.message, "Retry", "retry",
                          status="Check the caregiver USB connection.")
        if self.stage == "empty":
            return Screen("Check-in complete", "No message was sent.", "New check-in", "start")
        if self.stage == "plain_sent":
            return Screen(
                "Pain alert sent",
                PLAIN_PAIN_MESSAGE,
                status="Waiting for acknowledgment.",
            )
        subtitle = (
            "Start with a pain check, then review one grounded follow-up."
            if self.handoff_mode
            else (
                "Answer pain, send the alert, then add optional location detail."
                if self.progressive
                else "Answer two questions, then review your message."
            )
        )
        return Screen("Bedside check-in", subtitle, "Start", "start")
