"""Tests de la logique du serveur (dispatch, file d'attente, événements)."""

from __future__ import annotations

import unittest

from blueco import protocol as proto
from blueco.audio import FakeAudioSink
from blueco.config import TRANSPORT_UNIX, Config
from blueco.server import BluecoServer
from blueco.transport import TransportError, UnixListener


class ServerTestCase(unittest.TestCase):
    """Le serveur est construit sans socket réel : on teste ``dispatch``."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config()
        self.config.device_name = "pc-test"
        self.config.transport.mode = TRANSPORT_UNIX
        self.config.transport.socket_path = str(Path(self.tmp.name) / "test.sock")
        self.sink = FakeAudioSink()
        self.listener = UnixListener(self.config.transport.socket_path)
        self.addCleanup(self.listener.close)
        self.server = BluecoServer(self.config, self.sink, self.listener)

    def test_ping(self) -> None:
        reply = self.server.dispatch(proto.request(proto.CMD_PING))
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["result"]["pong"])
        self.assertEqual(reply["result"]["device"], "pc-test")

    def test_status_reports_held_headphones(self) -> None:
        reply = self.server.dispatch(proto.request(proto.CMD_STATUS))
        self.assertTrue(reply["result"]["headphones_held"])
        self.assertEqual(reply["result"]["listener"], f"unix:{self.config.transport.socket_path}")

    def test_release_and_grab_update_state(self) -> None:
        released = self.server.dispatch(proto.request(proto.CMD_RELEASE))
        self.assertFalse(released["result"]["connected"])
        self.assertFalse(self.server.status_payload()["headphones_held"])
        grabbed = self.server.dispatch(proto.request(proto.CMD_GRAB))
        self.assertTrue(grabbed["result"]["connected"])
        self.assertTrue(self.server.status_payload()["headphones_held"])

    def test_history_is_recorded(self) -> None:
        self.server.dispatch(proto.request(proto.CMD_RELEASE))
        self.server.dispatch(proto.request(proto.CMD_GRAB))
        events = [entry["event"] for entry in self.server.status_payload()["history"]]
        self.assertEqual(events, ["release", "grab"])

    def test_audio_error_becomes_protocol_error(self) -> None:
        self.server.sink = FakeAudioSink(fail_on={"release"})
        reply = self.server.dispatch(proto.request(proto.CMD_RELEASE))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], proto.E_AUDIO_UNAVAILABLE)

    def test_unknown_command_is_rejected(self) -> None:
        reply = self.server.dispatch({"v": 1, "id": "z", "cmd": "SHUTDOWN"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], proto.E_UNSUPPORTED)

    def test_token_is_enforced(self) -> None:
        self.config.token = "s3cret"
        denied = self.server.dispatch(proto.request(proto.CMD_RELEASE))
        self.assertEqual(denied["error"]["code"], proto.E_UNAUTHORIZED)
        allowed = self.server.dispatch(proto.request(proto.CMD_PING, token="s3cret"))
        self.assertTrue(allowed["ok"])
        wrong = self.server.dispatch(proto.request(proto.CMD_PING, token="nope"))
        self.assertEqual(wrong["error"]["code"], proto.E_UNAUTHORIZED)

    def test_push_without_peer_fails(self) -> None:
        with self.assertRaises(TransportError):
            self.server.push(proto.CMD_RELEASE, timeout=0.2)

    def test_push_can_be_queued(self) -> None:
        outcome = self.server.push(proto.CMD_RELEASE, allow_queue=True)
        self.assertTrue(outcome["queued"])
        self.assertEqual(self.server.status_payload()["queued"], 1)

    def test_identity_is_included(self) -> None:
        server = BluecoServer(self.config, self.sink, self.listener,
                              identity=lambda: {"user": "damien"})
        self.assertEqual(server.status_payload()["identity"], {"user": "damien"})

    def test_whoami(self) -> None:
        reply = self.server.dispatch(proto.request(proto.CMD_WHOAMI))
        self.assertEqual(reply["result"]["device"], "pc-test")
        self.assertEqual(reply["result"]["protocol"], proto.PROTOCOL_VERSION)

    def test_broadcast_state_without_sessions_is_safe(self) -> None:
        self.server.broadcast_state()


if __name__ == "__main__":
    unittest.main()
