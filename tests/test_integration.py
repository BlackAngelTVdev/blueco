"""Tests d'intégration : serveur + client réels sur socket locale.

Le transport ``unix`` emprunte exactement le même chemin de code que le transport
RFCOMM (mêmes trames, même session, même dispatch) ; seul le transport change.
C'est ce qui permet de valider toute la logique sans matériel Bluetooth.
"""

from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from blueco import protocol as proto
from blueco.audio import FakeAudioSink
from blueco.client import BluecoClient
from blueco.config import TRANSPORT_UNIX, Config
from blueco.server import BluecoServer
from blueco.session import Session
from blueco.transport import TransportError, UnixListener, connect_to


class IntegrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config()
        self.config.device_name = "pc-integration"
        self.config.transport.mode = TRANSPORT_UNIX
        self.config.transport.socket_path = str(Path(self.tmp.name) / "blueco.sock")
        self.config.request_timeout_s = 5.0
        self.sink = FakeAudioSink()
        self.listener = UnixListener(self.config.transport.socket_path)
        self.server = BluecoServer(self.config, self.sink, self.listener)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, args=(self.stop,),
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown)
        self.client = BluecoClient(self.config)

    def _teardown(self) -> None:
        self.stop.set()
        self.server.stop()
        self.thread.join(timeout=5)

    # -- helpers ------------------------------------------------------------ #
    def connect_phone(self, on_request=None) -> Session:
        conn = connect_to(self.config, timeout=5.0)
        session = Session(conn, on_request=on_request, name="phone").start()
        self.addCleanup(session.close)
        return session

    def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    # -- tests -------------------------------------------------------------- #
    def test_ping_and_status(self) -> None:
        self.assertTrue(self.client.ping()["pong"])
        status = self.client.status()
        self.assertEqual(status["device"], "pc-integration")
        self.assertTrue(status["headphones_held"])

    def test_release_then_grab_from_phone(self) -> None:
        released = self.client.release()
        self.assertTrue(released["was_connected"])
        self.assertFalse(self.sink.connected)
        grabbed = self.client.grab()
        self.assertTrue(grabbed["connected"])
        self.assertTrue(self.sink.connected)

    def test_one_shot_client_reconnects_each_time(self) -> None:
        self.client.status()
        self.client.status()
        self.assertEqual(len(self.sink.calls), 0)

    def test_server_pushes_request_to_peer(self) -> None:
        received: list[str] = []

        def handler(message: dict) -> dict:
            received.append(message["cmd"])
            return proto.ok(message["id"], accepted=True)

        session = self.connect_phone(handler)
        self.assertTrue(self.wait_for(lambda: self.server.live_session() is not None))
        result = self.server.push(proto.CMD_RELEASE, timeout=5.0)
        self.assertTrue(result["accepted"])
        self.assertEqual(received, [proto.CMD_RELEASE])
        self.assertTrue(session.alive)

    def test_queued_command_is_delivered_on_connect(self) -> None:
        self.server.push(proto.CMD_GRAB, allow_queue=True)
        received: list[str] = []
        self.connect_phone(lambda m: (received.append(m["cmd"]), proto.ok(m["id"]))[1])
        self.assertTrue(self.wait_for(lambda: proto.CMD_GRAB in received, timeout=5.0))

    def test_events_are_received_by_peer(self) -> None:
        events: list[dict] = []
        conn = connect_to(self.config, timeout=5.0)
        session = Session(conn, on_event=events.append, name="phone").start()
        self.addCleanup(session.close)
        self.assertTrue(self.wait_for(lambda: any(e.get("evt") == proto.EVT_HELLO for e in events)))
        self.client.release()
        self.assertTrue(self.wait_for(
            lambda: any(e.get("evt") == proto.EVT_STATE for e in events), timeout=5.0))

    def test_state_event_is_not_sent_to_the_service_we_killed(self) -> None:
        received: list[str] = []
        session = self.connect_phone(lambda m: (received.append(m["cmd"]), proto.ok(m["id"]))[1])
        self.assertTrue(self.wait_for(lambda: self.server.live_session() is not None))
        self.server.push(proto.CMD_PING, timeout=5.0)
        session.close()
        self.assertTrue(self.wait_for(lambda: self.server.live_session() is None))
        self.server.broadcast_state()  # ne doit pas lever

    def test_garbage_frames_do_not_kill_the_connection(self) -> None:
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(raw.close)
        raw.settimeout(5.0)
        raw.connect(self.config.transport.socket_path)
        raw.sendall(b"pas du json\n")
        raw.sendall(b'{"v":1,"id":"1","cmd":"NOPE"}\n')
        raw.sendall(proto.encode(proto.request(proto.CMD_PING, request_id="ok1")))
        reader = proto.LineReader(raw)
        reply = None
        for _ in range(5):
            line = reader.readline(timeout=5.0)
            if line is None:
                break
            message = proto.decode(line)
            if message.get("id") == "ok1":
                reply = message
                break
        self.assertIsNotNone(reply)
        self.assertTrue(reply["ok"])

    def test_unreachable_peer_raises_transport_error(self) -> None:
        self.stop.set()
        self.server.stop()
        self.thread.join(timeout=5)
        with self.assertRaises(TransportError):
            self.client.ping(timeout=1.0)

    def test_remote_error_propagates(self) -> None:
        self.server.sink = FakeAudioSink(fail_on={"grab"})
        with self.assertRaises(proto.RemoteError) as ctx:
            self.client.grab()
        self.assertEqual(ctx.exception.code, proto.E_AUDIO_UNAVAILABLE)

    def test_token_protection_end_to_end(self) -> None:
        # le serveur exige un jeton ; un client qui n'en a pas doit être refusé
        self.config.token = "s3cret"
        anonymous = Config.from_dict({**self.config.to_dict(), "token": ""})
        with self.assertRaises(proto.RemoteError) as ctx:
            BluecoClient(anonymous).ping()
        self.assertEqual(ctx.exception.code, proto.E_UNAUTHORIZED)
        # et un client qui le connaît passe
        self.assertTrue(BluecoClient(self.config).ping()["pong"])

    def test_status_from_peer_perspective(self) -> None:
        from blueco.watch import peer_state_event

        outcome = peer_state_event(self.client)
        self.assertTrue(outcome["reachable"])
        self.assertEqual(outcome["status"]["device"], "pc-integration")


if __name__ == "__main__":
    unittest.main()
