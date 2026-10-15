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


class MultiDeviceIntegrationTests(unittest.TestCase):
    """Bout-en-bout multi-appareils : pc1 détient les écouteurs, pc2 les veut.

    Deux vrais serveurs blueco tournent sur deux sockets locales (le transport
    change, pas le protocole) : on vérifie que la bascule interroge les deux,
    ne fait lâcher que le détenteur, puis se connecte côté local.
    """

    def setUp(self) -> None:
        from blueco.config import TRANSPORT_UNIX, Config, PeerConfig
        from blueco.switch import Switchboard

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stop = threading.Event()
        self.servers: list[tuple] = []
        self.sinks: dict[str, FakeAudioSink] = {}
        self.sockets: dict[str, str] = {}

        for name, connected in (("pc1", True), ("pc2", False)):
            socket_path = str(Path(self.tmp.name) / f"{name}.sock")
            cfg = Config()
            cfg.device_name = name
            cfg.transport.mode = TRANSPORT_UNIX
            cfg.transport.socket_path = socket_path
            cfg.request_timeout_s = 5.0
            sink = FakeAudioSink(connected=connected, mac="AA:BB:CC:DD:EE:FF")
            server = BluecoServer(cfg, sink, UnixListener(socket_path))
            thread = threading.Thread(target=server.serve_forever, args=(self.stop,), daemon=True)
            thread.start()
            self.servers.append((server, thread))
            self.sinks[name] = sink
            self.sockets[name] = socket_path
        self.addCleanup(self._teardown)

        # configuration « locale » = pc2, avec les deux appareils comme pairs
        self.local = Config()
        self.local.device_name = "pc2"
        self.local.transport.mode = TRANSPORT_UNIX
        self.local.request_timeout_s = 5.0
        self.local.peers = [
            PeerConfig(name="pc1", mac="11:22:33:44:55:66"),
            PeerConfig(name="pc2", mac="22:33:44:55:66:77"),
        ]
        self.board = Switchboard(self.local, self.sinks["pc2"],
                                 client_factory=self._client_for)

    def _teardown(self) -> None:
        self.stop.set()
        for server, thread in self.servers:
            server.stop()
            thread.join(timeout=5)

    def _client_for(self, clone: Config) -> BluecoClient:
        """Route chaque pair vers sa socket locale (le transport unix tient lieu de RFCOMM)."""
        label = clone.peers[0].label()
        clone.transport.socket_path = self.sockets[label]
        return BluecoClient(clone)

    def test_take_releases_the_holder_over_real_sockets(self) -> None:
        result = self.board.take()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["holders"], ["pc1"])
        self.assertFalse(self.sinks["pc1"].connected, "pc1 a libéré les écouteurs")
        self.assertTrue(self.sinks["pc2"].connected, "pc2 s'est connecté")
        self.assertEqual(self.sinks["pc2"].calls, ["grab"])

    def test_give_hands_the_headphones_to_a_named_peer(self) -> None:
        self.sinks["pc2"].connected = True
        result = self.board.give(target="pc1")
        self.assertTrue(result["ok"], result)
        self.assertFalse(self.sinks["pc2"].connected)
        self.assertTrue(self.sinks["pc1"].connected)

    def test_status_reports_every_peer(self) -> None:
        snapshot = self.board.snapshot()
        holder = [p for p in snapshot["peers"] if p["holds_headphones"]]
        self.assertEqual([p["peer"] for p in holder], ["pc1"])
        self.assertEqual(snapshot["peers"][1]["device"], "pc2")

    def test_take_when_nobody_holds_them(self) -> None:
        self.sinks["pc1"].connected = False
        result = self.board.take()
        self.assertTrue(result["ok"])
        self.assertEqual(result["holders"], [])
        self.assertEqual(self.sinks["pc1"].calls, [], "aucun RELEASE inutile")
        self.assertTrue(self.sinks["pc2"].connected)


class ControlChannelTests(unittest.TestCase):
    """`blueco take` doit utiliser le lien que le téléphone a ouvert.

    Le démon est le seul à voir ce lien : le CLI le pilote par la socket de
    contrôle locale (``TAKE``), et cette commande doit être refusée si elle
    arrive par Bluetooth.
    """

    PHONE_MAC = "AA:AA:AA:AA:AA:AA"

    def setUp(self) -> None:
        from blueco.config import TRANSPORT_UNIX, Config, PeerConfig
        from blueco.control import ControlServer

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        peer_socket = str(Path(self.tmp.name) / "peer.sock")
        control_socket = str(Path(self.tmp.name) / "control.sock")

        self.config = Config()
        self.config.device_name = "pc1"
        self.config.transport.mode = TRANSPORT_UNIX
        self.config.transport.socket_path = peer_socket
        self.config.transport.control_socket = control_socket
        self.config.request_timeout_s = 5.0
        self.config.peers = [PeerConfig(name="téléphone", mac=self.PHONE_MAC)]

        # le PC ne détient pas les écouteurs : c'est le téléphone qui les a
        self.sink = FakeAudioSink(connected=False)
        self.server = BluecoServer(self.config, self.sink, UnixListener(peer_socket))
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, args=(self.stop,), daemon=True)
        self.thread.start()
        self.control = ControlServer(self.server, control_socket).start()
        self.addCleanup(self._teardown)

        # le « téléphone » ouvre le lien et se déclare
        self.phone_commands: list[str] = []
        phone_config = Config()
        phone_config.transport.mode = TRANSPORT_UNIX
        phone_config.transport.socket_path = peer_socket
        self.phone = Session(connect_to(phone_config, timeout=5.0),
                             on_request=self._phone_handler, name="phone").start()
        self.addCleanup(self.phone.close)
        self.phone.notify(proto.EVT_HELLO, {"device": "téléphone", "mac": self.PHONE_MAC})
        # On attend que le HELLO soit traité, pas seulement que le lien existe :
        # c'est le HELLO qui porte le nom et l'adresse, et donc ce qui permet de
        # reconnaître le pair configuré. Attendre moins rend le test instable.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
            session.metadata.get("device") for session in self.server.sessions()
        ):
            time.sleep(0.02)

    def _teardown(self) -> None:
        self.control.stop()
        self.stop.set()
        self.server.stop()
        self.thread.join(timeout=5)

    def _phone_handler(self, message: dict) -> dict:
        command = message["cmd"]
        self.phone_commands.append(command)
        if command == proto.CMD_STATUS:
            return proto.ok(message["id"], device="téléphone", headphones_held=True,
                            audio={"connected": True})
        if command == proto.CMD_RELEASE:
            return proto.ok(message["id"], released="AA:BB:CC:DD:EE:FF", was_connected=True,
                            connected=False)
        return proto.ok(message["id"])

    def test_session_is_matched_with_the_configured_peer(self) -> None:
        peer = self.config.peers[0]
        session = self.server.session_for(peer)
        self.assertIsNotNone(session, "le lien du téléphone doit être reconnu par son adresse")
        self.assertEqual(session.metadata.get("device"), "téléphone")

    def test_take_through_the_control_channel(self) -> None:
        from blueco.control import call_control

        result = call_control(self.config, proto.CMD_TAKE, timeout=5.0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["holders"], ["téléphone"])
        self.assertEqual(self.phone_commands, [proto.CMD_STATUS, proto.CMD_RELEASE],
                         "le PC doit d'abord demander qui détient, puis faire lâcher")
        self.assertEqual(self.sink.calls, ["grab"], "le PC s'est ensuite connecté")

    def test_take_is_refused_over_a_peer_connection(self) -> None:
        with self.assertRaises(proto.RemoteError) as ctx:
            self.phone.request(proto.CMD_TAKE, timeout=5.0)
        self.assertEqual(ctx.exception.code, proto.E_UNSUPPORTED)

    def test_give_through_the_control_channel(self) -> None:
        from blueco.control import call_control

        self.sink.connected = True
        result = call_control(self.config, proto.CMD_GIVE, {"to": "téléphone"}, timeout=5.0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.sink.calls, ["release"])
        self.assertIn(proto.CMD_GRAB, self.phone_commands)

    def test_call_control_without_daemon(self) -> None:
        from blueco.config import Config
        from blueco.control import ControlUnavailable, call_control

        lonely = Config()
        lonely.transport.control_socket = str(Path(self.tmp.name) / "absent.sock")
        with self.assertRaises(ControlUnavailable):
            call_control(lonely, proto.CMD_TAKE)


class CliParserTests(unittest.TestCase):
    """Le CLI doit accepter les options globales avant *et* après la sous-commande."""

    def setUp(self) -> None:
        from blueco.cli import build_parser, _common_options

        self.parser = build_parser()
        self.common = _common_options

    def _parse(self, argv: list[str]):
        from blueco.cli import OPTION_DEFAULTS

        args = self.parser.parse_args(argv)
        for name, value in OPTION_DEFAULTS.items():
            if not hasattr(args, name):
                setattr(args, name, value)
        return args

    def test_globals_before_and_after_subcommand(self) -> None:
        before = self._parse(["--fake", "--transport", "unix", "status"])
        after = self._parse(["status", "--fake", "--transport", "unix"])
        for args in (before, after):
            self.assertTrue(args.fake)
            self.assertEqual(args.transport, "unix")
            self.assertEqual(args.verbose, 0)

    def test_verbose_counts_either_side(self) -> None:
        self.assertEqual(self._parse(["-v", "status"]).verbose, 1)
        self.assertEqual(self._parse(["status", "-v", "-v"]).verbose, 2)
        self.assertEqual(self._parse(["status", "-vv"]).verbose, 2)

    def test_peer_targeting_options(self) -> None:
        take = self._parse(["take", "--from", "bureau"])
        self.assertEqual(take.from_peer, "bureau")
        give = self._parse(["give", "--to", "téléphone"])
        self.assertEqual(give.to_peer, "téléphone")
        status = self._parse(["status", "--all"])
        self.assertTrue(status.all)
        self.assertFalse(status.remote)

    def test_peers_subcommands(self) -> None:
        listing = self._parse(["peers"])
        self.assertEqual(listing.action, "list")
        adding = self._parse(["peers", "add", "bureau", "--mac", "11:22:33:44:55:66", "--bt-name", "DESKTOP"])
        self.assertEqual((adding.action, adding.name, adding.bt_name),
                         ("add", "bureau", "DESKTOP"))
        removing = self._parse(["peers", "rm", "bureau"])
        self.assertEqual(removing.action, "rm")

    def test_peer_mac_override_is_available_everywhere(self) -> None:
        args = self._parse(["status", "--peer-mac", "11:22:33:44:55:66"])
        self.assertEqual(args.peer_mac, "11:22:33:44:55:66")


if __name__ == "__main__":
    unittest.main()
