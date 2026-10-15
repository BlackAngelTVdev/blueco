"""Tests de l'orchestration multi-appareils (qui tient les écouteurs ?).

Aucun matériel n'est nécessaire : les clients de chaque pair sont remplacés par
des doubles de test qui enregistrent les appels reçus.
"""

from __future__ import annotations

import unittest

from blueco import protocol as proto
from blueco.audio import FakeAudioSink
from blueco.config import Config, ConfigError, PeerConfig
from blueco.switch import Switchboard
from blueco.transport import TransportError


class FakePeerClient:
    """Double de :class:`blueco.client.BluecoClient` pour un pair donné."""

    def __init__(self, config: Config, *, holds: bool = False, reachable: bool = True,
                 fail_release: bool = False, fail_status: bool = False) -> None:
        self.config = config
        self.holds = holds
        self.reachable = reachable
        self.fail_release = fail_release
        self.fail_status = fail_status
        self.calls: list[str] = []

    @property
    def label(self) -> str:
        return self.config.peers[0].label()

    def status(self, timeout: float | None = None) -> dict:
        self.calls.append("status")
        if not self.reachable:
            raise TransportError(f"{self.label} injoignable")
        if self.fail_status:
            raise proto.RemoteError(proto.E_INTERNAL, "status en échec")
        return {"device": self.label, "headphones_held": self.holds,
                "audio": {"connected": self.holds, "name": "Écouteurs"}}

    def release(self, timeout: float | None = None) -> dict:
        self.calls.append("release")
        if self.fail_release:
            raise proto.RemoteError(proto.E_AUDIO_UNAVAILABLE, "refus")
        was = self.holds
        self.holds = False
        return {"released": "AA:BB:CC:DD:EE:FF", "was_connected": was, "already_free": not was}

    def grab(self, timeout: float | None = None) -> dict:
        self.calls.append("grab")
        self.holds = True
        return {"connected": True, "mac": "AA:BB:CC:DD:EE:FF", "already_connected": False}


class SwitchboardTestCase(unittest.TestCase):
    def build(self, *, holders: dict[str, dict], sink: FakeAudioSink | None = None):
        """``holders`` : étiquette → options du double (holds, reachable, fail_*)."""
        config = Config()
        config.device_name = "pc1"
        config.peers = [PeerConfig(name=name, mac=mac)
                        for name, mac in (("pc2", "11:22:33:44:55:66"),
                                          ("pc3", "22:33:44:55:66:77"),
                                          ("téléphone", "33:44:55:66:77:88"))]
        created: dict[str, FakePeerClient] = {}

        def factory(clone: Config) -> FakePeerClient:
            label = clone.peers[0].label()
            client = FakePeerClient(clone, **holders.get(label, {}))
            created[label] = client
            return client

        self.sink = sink or FakeAudioSink(connected=False)
        self.board = Switchboard(config, self.sink, client_factory=factory)
        return created

    # -- interrogation ---------------------------------------------------- #
    def test_reports_cover_every_configured_peer(self) -> None:
        self.build(holders={"pc2": {"holds": True}, "pc3": {}, "téléphone": {"reachable": False}})
        reports = {r.label: r for r in self.board.reports()}
        self.assertEqual(set(reports), {"pc2", "pc3", "téléphone"})
        self.assertTrue(reports["pc2"].reachable)
        self.assertFalse(reports["téléphone"].reachable)
        self.assertIn("injoignable", reports["téléphone"].error)

    def test_holders_lists_only_the_holder(self) -> None:
        self.build(holders={"pc2": {"holds": True}})
        self.assertEqual([r.label for r in self.board.holders()], ["pc2"])

    def test_unreachable_peer_is_not_fatal(self) -> None:
        created = self.build(holders={"pc2": {"reachable": False}, "pc3": {}})
        self.assertTrue(self.board.reports())
        self.assertEqual(created["pc2"].calls, ["status"])

    # -- take -------------------------------------------------------------- #
    def test_take_releases_only_the_holder_then_grabs_locally(self) -> None:
        created = self.build(holders={"pc3": {"holds": True}})
        result = self.board.take()
        self.assertTrue(result["ok"])
        self.assertEqual(result["holders"], ["pc3"])
        self.assertEqual(created["pc3"].calls, ["status", "release"])
        self.assertEqual(created["pc2"].calls, ["status"], "les non-détenteurs ne reçoivent rien")
        self.assertEqual(self.sink.calls, ["grab"])
        self.assertTrue(result["grabbed"]["connected"])

    def test_take_with_no_holder_still_connects_locally(self) -> None:
        self.build(holders={})
        result = self.board.take()
        self.assertTrue(result["ok"])
        self.assertEqual(result["holders"], [])
        self.assertEqual(self.sink.calls, ["grab"])

    def test_take_from_a_named_peer_only_probes_it(self) -> None:
        created = self.build(holders={"pc3": {"holds": True}})
        result = self.board.take(source="pc2")
        self.assertTrue(result["ok"])
        self.assertEqual(created["pc2"].calls, ["status"])
        self.assertNotIn("pc3", created, "un seul pair doit être interrogé")
        self.assertEqual(self.sink.calls, ["grab"])

    def test_take_aborts_when_release_fails(self) -> None:
        self.build(holders={"pc2": {"holds": True, "fail_release": True}})
        result = self.board.take()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["grabbed"])
        self.assertEqual(self.sink.calls, [], "on ne se connecte pas tant que l'autre tient le casque")
        self.assertEqual(result["failures"][0]["code"], proto.E_AUDIO_UNAVAILABLE)

    def test_take_skip_release(self) -> None:
        created = self.build(holders={"pc2": {"holds": True}})
        result = self.board.take(skip_release=True)
        self.assertTrue(result["ok"])
        self.assertEqual(created["pc2"].calls, ["status"])
        self.assertEqual(self.sink.calls, ["grab"])

    def test_take_reports_local_failure(self) -> None:
        self.build(holders={}, sink=FakeAudioSink(connected=False, fail_on={"grab"}))
        result = self.board.take()
        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"][0]["code"], proto.E_AUDIO_UNAVAILABLE)

    def test_take_with_no_peer_configured_just_connects(self) -> None:
        """Personne à interroger : on se connecte, et c'est tout."""
        sink = FakeAudioSink(connected=False)
        result = Switchboard(Config(), sink).take()
        self.assertTrue(result["ok"])
        self.assertEqual(result["holders"], [])
        self.assertEqual(sink.calls, ["grab"])

    def test_unknown_peer_name(self) -> None:
        self.build(holders={})
        with self.assertRaises(ConfigError):
            self.board.take(source="nas")

    # -- give -------------------------------------------------------------- #
    def test_give_releases_locally_then_grabs_on_target(self) -> None:
        created = self.build(holders={}, sink=FakeAudioSink(connected=True))
        result = self.board.give(target="téléphone")
        self.assertTrue(result["ok"])
        self.assertEqual(self.sink.calls, ["release"])
        self.assertEqual(created["téléphone"].calls, ["grab"])
        self.assertEqual(created["téléphone"].config.peers[0].label(), "téléphone")

    def test_give_requires_a_target_when_several_peers(self) -> None:
        self.build(holders={}, sink=FakeAudioSink(connected=True))
        with self.assertRaises(ConfigError):
            self.board.give()

    def test_give_reports_remote_failure(self) -> None:
        self.build(holders={}, sink=FakeAudioSink(connected=True))
        peer = self.board.config.require_peer("téléphone")
        client = self.board.client(peer)

        def boom(timeout: float | None = None) -> dict:
            raise TransportError("pc éteint")

        client.grab = boom  # type: ignore[method-assign]
        result = self.board.give(target="téléphone")
        self.assertFalse(result["ok"])
        self.assertTrue(result["released"], "la libération locale a bien eu lieu")

    def test_give_skip_grab(self) -> None:
        self.build(holders={}, sink=FakeAudioSink(connected=True))
        result = self.board.give(target="pc2", skip_grab=True)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["grabbed"])

    # -- snapshot ---------------------------------------------------------- #
    def test_snapshot_contains_local_and_remote_state(self) -> None:
        self.build(holders={"pc2": {"holds": True}}, sink=FakeAudioSink(connected=True))
        snapshot = self.board.snapshot()
        self.assertTrue(snapshot["headphones_held"])
        self.assertEqual(len(snapshot["peers"]), 3)
        holder = [p for p in snapshot["peers"] if p["holds_headphones"]]
        self.assertEqual(holder[0]["peer"], "pc2")


class ResolverTests(unittest.TestCase):
    def test_resolver_is_used_to_fill_missing_mac(self) -> None:
        config = Config()
        config.peers = [PeerConfig(name="bureau")]
        seen: list[str] = []

        def factory(clone: Config) -> FakePeerClient:
            seen.append(clone.peers[0].mac)
            return FakePeerClient(clone, holds=True)

        board = Switchboard(config, FakeAudioSink(connected=False),
                            resolver=lambda peer: "AA:BB:CC:DD:EE:FF",
                            client_factory=factory)
        board.take()
        self.assertEqual(seen, ["AA:BB:CC:DD:EE:FF"])

    def test_missing_mac_without_resolver_is_a_clear_error(self) -> None:
        config = Config()
        config.peers = [PeerConfig(name="bureau")]
        board = Switchboard(config, FakeAudioSink(), client_factory=FakePeerClient)
        reports = board.reports()
        self.assertFalse(reports[0].reachable)
        self.assertIn("adresse", reports[0].error.lower())


class InboundOnlyPeerTests(unittest.TestCase):
    """Un pair purement client (le téléphone) n'est jamais appelé par nous."""

    class StubSession:
        """Connexion déjà ouverte par le pair (mêmes appels que Session)."""

        def __init__(self, holds: bool = True) -> None:
            self.holds = holds
            self.calls: list[str] = []

        def request(self, command: str, args=None, timeout=None, token=None) -> dict:
            self.calls.append(command)
            if command == proto.CMD_STATUS:
                return {"device": "téléphone", "headphones_held": self.holds,
                        "audio": {"connected": self.holds}}
            if command == proto.CMD_RELEASE:
                was = self.holds
                self.holds = False
                return {"released": "AA:BB:CC:DD:EE:FF", "was_connected": was}
            if command == proto.CMD_GRAB:
                self.holds = True
                return {"connected": True}
            return {}

    class StubServer:
        """Rend la connexion ouverte par le pair, si elle existe."""

        def __init__(self, session=None) -> None:
            self.session = session

        def session_for(self, peer) -> object | None:
            return self.session

    def build(self, *, session=None) -> Switchboard:
        config = Config()
        config.device_name = "pc"
        config.peers = [PeerConfig(name="téléphone", mac="33:44:55:66:77:88", outbound=False)]

        def factory(clone: Config):
            raise AssertionError("un pair purement client ne doit pas être appelé")

        return Switchboard(config, FakeAudioSink(connected=False),
                           client_factory=factory,
                           server=self.StubServer(session))

    def test_without_link_nothing_is_dialled(self) -> None:
        report = self.build().reports()[0]
        self.assertFalse(report.reachable)
        self.assertEqual(report.error_code, "NOT_CONNECTED")
        self.assertIn("lien ouvert", report.error)

    def test_take_goes_through_the_link_the_phone_opened(self) -> None:
        session = self.StubSession(holds=True)
        result = self.build(session=session).take()
        self.assertTrue(result["ok"])
        self.assertEqual(result["holders"], ["téléphone"])
        self.assertEqual(session.calls, [proto.CMD_STATUS, proto.CMD_RELEASE])

    def test_outbound_defaults_to_true(self) -> None:
        self.assertTrue(PeerConfig(name="bureau").outbound)


if __name__ == "__main__":
    unittest.main()
