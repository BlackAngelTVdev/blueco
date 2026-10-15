"""Tests du codec de trames."""

from __future__ import annotations

import json
import socket
import unittest

from blueco import protocol as proto


class EncodeDecodeTests(unittest.TestCase):
    def test_request_roundtrip(self) -> None:
        message = proto.request(proto.CMD_RELEASE, {"mac": "AA:BB:CC:DD:EE:FF"}, token="t",
                               origin="phone")
        raw = proto.encode(message)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(proto.decode(raw), message)

    def test_command_is_uppercased(self) -> None:
        decoded = proto.decode(b'{"v":1,"id":"a1","cmd":"release"}')
        self.assertEqual(decoded["cmd"], proto.CMD_RELEASE)

    def test_reply_roundtrip(self) -> None:
        reply = proto.ok("abc", {"connected": False})
        self.assertEqual(proto.decode(proto.encode(reply)), reply)
        bad = proto.failure("abc", proto.E_NO_DEVICE, "casque inconnu")
        self.assertEqual(proto.decode(proto.encode(bad)), bad)

    def test_event_roundtrip(self) -> None:
        message = proto.event(proto.EVT_STATE, {"connected": True})
        self.assertEqual(proto.decode(proto.encode(message)), message)

    def test_unicode_survives(self) -> None:
        message = proto.failure("a", proto.E_INTERNAL, "écouteurs « libérés » → ok")
        self.assertEqual(proto.decode(proto.encode(message))["error"]["message"],
                         "écouteurs « libérés » → ok")

    def test_unknown_command(self) -> None:
        with self.assertRaises(proto.ProtocolError) as ctx:
            proto.decode(b'{"v":1,"id":"a","cmd":"DROP_TABLE"}')
        self.assertEqual(ctx.exception.code, proto.E_UNSUPPORTED)

    def test_bad_version(self) -> None:
        with self.assertRaises(proto.ProtocolError):
            proto.decode(b'{"v":9,"id":"a","cmd":"PING"}')

    def test_missing_id(self) -> None:
        with self.assertRaises(proto.ProtocolError):
            proto.decode(b'{"v":1,"cmd":"PING"}')

    def test_not_json(self) -> None:
        with self.assertRaises(proto.ProtocolError) as ctx:
            proto.decode(b"hello world")
        self.assertEqual(ctx.exception.code, proto.E_BAD_FRAME)

    def test_not_an_object(self) -> None:
        with self.assertRaises(proto.ProtocolError):
            proto.decode(b"[1,2,3]")

    def test_empty(self) -> None:
        with self.assertRaises(proto.ProtocolError):
            proto.decode(b"   \n")

    def test_reply_without_id(self) -> None:
        with self.assertRaises(proto.ProtocolError):
            proto.decode(b'{"v":1,"ok":true}')

    def test_oversized_frame_is_refused(self) -> None:
        with self.assertRaises(proto.ProtocolError):
            proto.encode({"v": 1, "id": "a", "cmd": "PING", "pad": "x" * proto.MAX_FRAME})

    def test_newlines_are_not_allowed_inside_payload(self) -> None:
        raw = proto.encode(proto.failure("a", proto.E_INTERNAL, "ligne 1\nligne 2"))
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual(json.loads(raw)["error"]["message"], "ligne 1\nligne 2")


class ResultTests(unittest.TestCase):
    def test_result_of_ok(self) -> None:
        self.assertEqual(proto.result_of(proto.ok("a", {"x": 1})), {"x": 1})

    def test_result_of_failure(self) -> None:
        with self.assertRaises(proto.RemoteError) as ctx:
            proto.result_of(proto.failure("a", proto.E_BUSY, "occupé"))
        self.assertEqual(ctx.exception.code, proto.E_BUSY)
        self.assertEqual(ctx.exception.message, "occupé")


class LineReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.left, self.right = socket.socketpair()
        self.addCleanup(self.left.close)
        self.addCleanup(self.right.close)

    def test_reads_multiple_frames(self) -> None:
        reader = proto.LineReader(self.left)
        self.right.sendall(b'{"a":1}\n{"b":2}\n')
        self.assertEqual(reader.readline(timeout=1), b'{"a":1}')
        self.assertEqual(reader.readline(timeout=1), b'{"b":2}')

    def test_partial_frame_then_completion(self) -> None:
        reader = proto.LineReader(self.left)
        self.right.sendall(b'{"a"')
        with self.assertRaises(TimeoutError):
            reader.readline(timeout=0.1)
        self.right.sendall(b':1}\n')
        self.assertEqual(reader.readline(timeout=1), b'{"a":1}')

    def test_eof_returns_none(self) -> None:
        reader = proto.LineReader(self.left)
        self.right.close()
        self.assertIsNone(reader.readline(timeout=1))

    def test_trailing_data_without_newline_is_returned_at_eof(self) -> None:
        reader = proto.LineReader(self.left)
        self.right.sendall(b'{"a":1}')
        self.right.close()
        self.assertEqual(reader.readline(timeout=1), b'{"a":1}')

    def test_oversized_line(self) -> None:
        reader = proto.LineReader(self.left, max_frame=16)
        self.right.sendall(b"x" * 64)
        with self.assertRaises(proto.ProtocolError):
            reader.readline(timeout=1)

    def test_closed_socket_raises_not_connected(self) -> None:
        reader = proto.LineReader(self.left)
        self.left.close()
        with self.assertRaises(proto.ProtocolError) as ctx:
            reader.readline(timeout=1)
        self.assertEqual(ctx.exception.code, proto.E_NOT_CONNECTED)


if __name__ == "__main__":
    unittest.main()
