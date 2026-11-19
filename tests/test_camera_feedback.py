import queue
import sys
import threading
import unittest
from unittest.mock import patch

from bedside import __main__ as host
from bedside.pain_location import FIRST_STAGE
from bedside.session import ANSWER_HINT_DELAY, CAMERA_ARM_DELAY, LOCATION_IDLE_TIMEOUT, CheckIn


class CameraSessionTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.sent = []
        self.controller = CheckIn(self.send, lambda *_args, **_kwargs: None,
                                  camera=True, clock=lambda: self.now, progressive=True)
        self.controller.handle("patient", {"type": "hello"})

    def send(self, role, packet):
        self.sent.append((role, packet))
        return True

    def touch(self, value):
        self.controller.handle("patient", {"type": "input", "prompt_id": self.controller.prompt_id,
                                           "value": value, "source": "touch"})

    def camera(self, scope, kind, **fields):
        self.controller.handle("camera", {"type": kind, "prompt_id": scope[0],
                                           "generation": scope[1], **fields})

    def arm(self):
        self.now += CAMERA_ARM_DELAY
        self.controller.tick()
        return self.controller.camera_scope

    def packets(self, kind):
        return [packet for role, packet in self.sent if role == "patient" and packet["type"] == kind]

    def test_receipts_survive_transitions_and_only_touch_sends(self):
        self.controller.handle("caregiver", {"type": "hello"})
        self.touch("start")
        self.assertIsNone(self.controller.camera_scope)
        pain = self.arm()
        self.camera(pain, "input", value="yes")
        self.assertFalse(self.packets("answer_feedback"))
        self.camera(pain, "camera_ready")
        screen_count = len(self.packets("screen"))
        self.camera(pain, "camera_ready")
        self.controller.handle("patient", {"type": "hello"})
        self.assertEqual(len(self.packets("screen")), screen_count)
        self.assertEqual(self.controller.camera_scope, pain)
        self.camera(pain, "input", value="yes")
        review = self.controller.prompt_id
        self.assertEqual(self.controller.stage, "review")
        self.assertEqual(self.packets("answer_feedback"), [
            {"type": "answer_feedback", "prompt_id": pain[0], "answer": "Yes", "method": "Nod accepted"},
        ])
        self.assertEqual(self.packets("screen")[-1]["receipt"], "Nod accepted: Yes")
        self.camera(pain, "input", value="no")
        self.camera((review, pain[1]), "input", value="send")
        self.assertEqual(self.controller.stage, "review")
        self.assertFalse(any(packet["type"] == "show_request" for _, packet in self.sent))
        self.touch("send")
        self.controller.handle("caregiver", {"type": "displayed", "request_id": self.controller.request_id})
        self.assertEqual(self.controller.stage, FIRST_STAGE)
        self.now += 60
        self.controller.tick()
        location = self.controller.camera_scope
        self.assertIsNotNone(location)
        self.assertEqual(self.controller.stage, FIRST_STAGE)
        self.camera(pain, "input", value="yes")
        self.assertEqual(self.controller.stage, FIRST_STAGE)
        self.camera(location, "camera_ready")
        self.camera(location, "input", value="no")
        self.assertEqual(self.packets("answer_feedback")[-1]["method"], "Shake accepted")
        self.assertEqual(self.packets("screen")[-1]["receipt"], "Shake accepted: No")
        self.assertIsNone(self.controller.camera_scope)
        self.touch("yes")
        self.assertEqual(self.packets("answer_feedback")[-1]["method"], "Touch accepted")
        self.assertEqual(self.packets("screen")[-1]["receipt"], "Touch: Yes")
        self.assertEqual([packet["enabled"] for packet in self.packets("camera")], [True])
        self.controller.stop_camera_stream()
        self.assertEqual([packet["enabled"] for packet in self.packets("camera")], [True, False])

    def test_new_connection_rearms_same_question_with_fresh_generation(self):
        self.touch("start")
        old_scope = self.arm()
        connection = object()
        hello = {"type": "hello", "connection": connection}
        self.controller.handle("patient", hello)
        self.assertIsNone(self.controller.camera_scope)
        new_scope = self.arm()
        self.controller.handle("patient", hello)
        self.assertEqual(new_scope, self.controller.camera_scope)
        self.assertEqual(old_scope[0], new_scope[0])
        self.assertNotEqual(old_scope[1], new_scope[1])
        self.camera(new_scope, "camera_ready")
        self.camera(old_scope, "input", value="yes")
        self.assertEqual(self.controller.stage, "pain")
        self.camera(new_scope, "input", value="yes")
        self.assertEqual(self.controller.stage, "review")
        self.assertEqual([packet["enabled"] for packet in self.packets("camera")], [True, True])

    def test_confirmations_precede_the_next_prompt_and_never_change_the_alert(self):
        speech = []
        self.controller.speak = lambda words, **options: speech.append((words, options))
        self.controller.handle("caregiver", {"type": "hello"})
        self.touch("start")
        pain = self.arm()
        self.camera(pain, "camera_ready")
        self.camera(pain, "input", value="yes")
        self.assertEqual(speech[-1], (
            "Yes recorded. Your message is: I'm in pain. Tap Send if this is right.", {"exact": True},
        ))
        self.assertFalse(any(p["type"] == "show_request" for _, p in self.sent))
        self.touch("send")
        report = next(p for _, p in self.sent if p["type"] == "show_request")
        self.assertEqual(report["text"], "I'm in pain.")
        self.controller.handle("caregiver", {"type": "displayed", "request_id": report["request_id"]})
        self.assertEqual(speech[-1][0], "Is the pain in your upper body?")
        location = self.arm()
        self.camera(location, "camera_ready")
        self.camera(location, "input", value="no")
        self.assertEqual(speech[-1][0], "No recorded. Pain in your hip, pelvis, or thigh?")
        count = len(speech)
        self.camera(location, "input", value="no")
        self.assertEqual(len(speech), count)

    def test_touch_only_mode_retains_hint_and_optional_location_timeout(self):
        self.controller = CheckIn(self.send, lambda *_args, **_kwargs: None,
                                  clock=lambda: self.now, progressive=True)
        self.controller.handle("patient", {"type": "hello"})
        self.controller.handle("caregiver", {"type": "hello"})
        self.touch("start")
        self.now = ANSWER_HINT_DELAY
        self.controller.tick()
        self.assertTrue(self.controller.no_answer_hint)
        self.touch("yes")
        self.touch("send")
        self.controller.handle("caregiver", {"type": "displayed", "request_id": self.controller.request_id})
        self.assertEqual(self.controller.stage, FIRST_STAGE)
        self.now += LOCATION_IDLE_TIMEOUT
        self.controller.tick()
        self.assertEqual(self.controller.stage, "plain_sent")


class CameraHostTests(unittest.TestCase):
    def test_main_displays_all_frames_and_routes_only_armed_matches(self):
        frames = []
        classified = []
        statuses = []
        outgoing = []
        callbacks = {}

        class FakePreview:
            def start(self):
                frames.append("started")

            def submit(self, jpeg):
                frames.append(jpeg)

            def set_status(self, cue, receipt):
                statuses.append((cue, receipt))

            def close(self):
                frames.append("closed")

        class FakeCamera:
            def __init__(self, emit, _current_scope):
                self.emit = emit

            def submit(self, scope, jpeg):
                classified.append(jpeg)
                fields = {"prompt_id": scope[0], "generation": scope[1]}
                self.emit("camera", {"type": "camera_ready", **fields})
                self.emit("camera", {"type": "input", "value": "yes", **fields})

            def close(self):
                pass

        class FakeLinks:
            def __init__(self, on_wire, **_ports):
                callbacks["wire"] = on_wire

            def start(self):
                callbacks["wire"]("patient", {"type": "hello"})

            def send(self, role, packet):
                outgoing.append((role, packet))
                if packet["type"] == "screen" and packet["title"] == "Bedside check-in":
                    prompt = packet["prompt_id"]
                    callbacks["wire"]("patient", {"type": "frame", "prompt_id": prompt, "jpeg": "idle"})
                    callbacks["wire"]("patient", {"type": "input", "prompt_id": prompt,
                                                  "value": "start", "source": "touch"})
                return True

            def close(self):
                pass

        class SimulatedCheckIn(CheckIn):
            ticks = 0

            def tick(self):
                self.ticks += 1
                if self.ticks > 30:
                    raise AssertionError(f"Simulated run stalled at {self.stage}")
                if self.stage == "pain" and self.camera_scope is None:
                    wire = callbacks["wire"]
                    wire("patient", {"type": "frame", "prompt_id": self.prompt_id, "jpeg": "quiet"})
                    self.camera_arm_at = 0
                    super().tick()
                    wire("patient", {"type": "frame", "prompt_id": "stale", "jpeg": "older"})
                    wire("caregiver", {"type": "frame", "prompt_id": self.prompt_id, "jpeg": "wrong role"})
                    wire("patient", {"type": "frame", "prompt_id": self.prompt_id, "jpeg": "question"})
                if self.stage == "review":
                    callbacks["wire"]("patient", {"type": "frame", "prompt_id": self.prompt_id, "jpeg": "review"})
                    raise KeyboardInterrupt
                super().tick()

        argv = ["bedside", "--camera", "--voice", "off", "--simulate", "caregiver"]
        with (patch.object(sys, "argv", argv), patch.object(host, "CameraPreview", FakePreview),
              patch.object(host, "CameraInput", FakeCamera), patch.object(host, "CheckIn", SimulatedCheckIn),
              patch.object(host, "read_commands"), patch.object(host, "load_voice_settings"),
              patch("bedside.serial_link.SerialLinks", FakeLinks)):
            host.main()

        self.assertEqual(frames, ["started", "idle", "quiet", "older", "question", "review", "closed"])
        self.assertEqual(classified, ["question"])
        self.assertEqual(statuses[-1][1], "Nod accepted: Yes")
        self.assertIn("Send", statuses[-1][0])
        camera_packets = [packet["enabled"] for _, packet in outgoing if packet["type"] == "camera"]
        self.assertEqual(camera_packets, [True, False])

    def test_classifier_drops_inflight_old_result_and_unprocessed_frames(self):
        scope = ("old", 1)
        entered = threading.Event()
        release = threading.Event()
        packets = queue.Queue()
        fed = []
        resets = []

        class FakeGestures:
            ready = False

            def reset(self, prompt_id):
                self.ready = False
                resets.append(prompt_id)

            def feed(self, jpeg):
                fed.append(jpeg)
                if jpeg == "old":
                    entered.set()
                    release.wait(timeout=2)
                self.ready = True
                return "yes"

        with patch("bedside.vision.HeadGestures") as gestures:
            gestures.return_value.__enter__.return_value = FakeGestures()
            camera = host.CameraInput(lambda *event: packets.put(event), lambda: scope)
            try:
                camera.submit(scope, "old")
                self.assertTrue(entered.wait(timeout=2))
                scope = ("new", 2)
                for number in range(100):
                    camera.submit(scope, f"sample-{number}")
                release.set()
                events = [packets.get(timeout=2), packets.get(timeout=2)]
                self.assertTrue(packets.empty())
                self.assertEqual([packet["generation"] for _, packet in events], [2, 2])
                self.assertEqual([packet["type"] for _, packet in events], ["camera_ready", "input"])
                self.assertEqual(fed, ["old", "sample-99"])
                self.assertEqual(resets, ["old", "new"])
            finally:
                release.set()
                camera.close()
