import json
import queue
import unittest

from bedside.serial_link import SerialLinks


PING = b'{"type":"ping"}\n'
RESET_AND_PING = b"\x18\n" + PING


class FakeBoard:
    def __init__(self, role):
        self.role = role
        self.pending = bytearray()
        self.received = []
        self.writes = queue.Queue()
        self.write_failure = None

    def open(self, **_kwargs):
        return FakePort(self)


class FakePort:
    def __init__(self, board):
        self.board = board
        self.replies = queue.Queue()

    def write(self, payload):
        board = self.board
        board.writes.put(payload)
        failure = board.write_failure
        board.write_failure = None
        delivered = payload[:-1] if failure else payload
        for byte in delivered:
            if byte == 0x18:
                board.pending.clear()
            elif byte == 10:
                if board.pending:
                    event = json.loads(board.pending)
                    board.pending.clear()
                    if event == {"type": "ping"}:
                        hello = json.dumps({"type": "hello", "role": board.role})
                        self.replies.put((hello + "\n").encode())
                    else:
                        board.received.append(event)
            else:
                board.pending.append(byte)
        if failure == "timeout":
            raise TimeoutError("serial write timed out")
        return len(delivered)

    def flush(self):
        raise AssertionError("serial writes must not wait for a drain")

    def read(self, _size):
        try:
            return self.replies.get(timeout=0.01)
        except queue.Empty:
            return b""

    def close(self):
        pass


class BufferedFakePort(FakePort):
    def __init__(self, board):
        super().__init__(board)
        self.buffer = b""
        self.read_sizes = queue.Queue()

    @property
    def in_waiting(self):
        if not self.buffer:
            try:
                self.buffer = self.replies.get_nowait()
            except queue.Empty:
                pass
        return len(self.buffer)

    def read(self, size):
        self.read_sizes.put(size)
        if not self.buffer:
            self.buffer = super().read(size)
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result


class SerialLinkTests(unittest.TestCase):
    def connect(self, board, *, ping_interval=60, max_frame_bytes=80_000, probes=(PING,)):
        events = queue.Queue()
        links = SerialLinks(
            lambda role, event: events.put((role, event["type"])),
            patient_port="/fake/board",
            caregiver_port="/fake/board",
            reconnect_interval=0.1,
            ping_interval=ping_interval,
            max_frame_bytes=max_frame_bytes,
            serial_factory=board.open,
        )
        self.addCleanup(links.close)
        links.start()
        self.assertEqual(events.get(timeout=2), (board.role, "hello"))
        for probe in probes:
            self.assertEqual(board.writes.get(timeout=2), probe)
        return links, events

    def test_initial_probe_periodic_ping_and_send_do_not_drain(self):
        board = FakeBoard("patient")
        links, _ = self.connect(board, ping_interval=0.25)
        self.assertEqual(board.writes.get(timeout=2), PING)
        self.assertTrue(links.send("patient", {"type": "screen"}))
        self.assertEqual(board.received, [{"type": "screen"}])

    def test_reader_waits_for_one_byte_and_drains_pending_bytes_in_order(self):
        board = FakeBoard("caregiver")
        port = BufferedFakePort(board)
        board.open = lambda **_kwargs: port
        _, events = self.connect(board)
        hello = json.dumps({"type": "hello", "role": "caregiver"}) + "\n"
        self.assertEqual(port.read_sizes.get(timeout=2), len(hello.encode()))
        self.assertEqual(port.read_sizes.get(timeout=2), 1)

        frame = b'{"type":"frame","jpeg":"' + b"A" * 5000 + b'"}\n'
        port.replies.put(b'{"type":"displayed"}\n' + frame + b'{"type":"acknowledged"}\n')
        self.assertEqual(events.get(timeout=2), ("caregiver", "displayed"))
        self.assertEqual(events.get(timeout=2), ("caregiver", "frame"))
        self.assertEqual(events.get(timeout=2), ("caregiver", "acknowledged"))
        reads = [port.read_sizes.get_nowait() for _ in range(port.read_sizes.qsize())]
        self.assertEqual(max(reads), 4096)

    def test_each_firmware_limit_counts_utf8_bytes_without_newline(self):
        for role, limit in (("patient", 2048), ("caregiver", 768)):
            with self.subTest(role=role):
                board = FakeBoard(role)
                links, _ = self.connect(board)
                event = {"text": "é" * ((limit - len(b'{"text":""}')) // 2) + "x"}
                self.assertTrue(links.send(role, event))
                self.assertEqual(len(board.writes.get(timeout=2)), limit + 1)
                self.assertFalse(links.send(role, {"text": event["text"] + "x"}))
                self.assertEqual(board.received, [event])
                self.assertIsNotNone(links.port_for(role))

    def test_configured_frame_limit_still_bounds_smaller_writes(self):
        board = FakeBoard("patient")
        links, _ = self.connect(board, max_frame_bytes=256)
        self.assertFalse(links.send("patient", {"text": "a" * 256}))
        self.assertTrue(board.writes.empty())

    def test_short_initial_probe_is_canceled_before_reading_hello(self):
        board = FakeBoard("patient")
        board.write_failure = "short"
        links, events = self.connect(board, probes=(PING, RESET_AND_PING))
        self.assertTrue(events.empty())
        self.assertTrue(links.send("patient", {"type": "screen"}))

    def test_short_or_timed_out_write_cancels_pending_command_after_reconnect(self):
        for failure in ("short", "timeout"):
            with self.subTest(failure=failure):
                board = FakeBoard("caregiver")
                links, events = self.connect(board)
                board.write_failure = failure
                self.assertFalse(links.send("caregiver", {"type": "show_request", "text": "first"}))
                self.assertEqual(events.get(timeout=2), ("caregiver", "disconnected"))
                self.assertEqual(events.get(timeout=2), ("caregiver", "hello"))
                self.assertEqual(board.writes.get(timeout=2), b'{"type":"show_request","text":"first"}\n')
                self.assertEqual(board.writes.get(timeout=2), RESET_AND_PING)
                self.assertTrue(links.send("caregiver", {"type": "show_request", "text": "second"}))
                self.assertEqual(board.received, [{"type": "show_request", "text": "second"}])


if __name__ == "__main__":
    unittest.main()
