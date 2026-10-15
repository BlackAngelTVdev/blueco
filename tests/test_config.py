"""Tests de la configuration."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from blueco import config as cfgmod
from blueco.config import Config, ConfigError, PeerConfig


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.transport.mode, cfgmod.TRANSPORT_PROFILE1)
        self.assertEqual(cfg.transport.channel, 3)
        self.assertEqual(cfg.device_name, "pc")
        self.assertEqual(cfg.audio_backend, cfgmod.AUDIO_BLUEZ)
        cfg.validate()

    def test_roundtrip_through_dict(self) -> None:
        cfg = Config()
        cfg.device_name = "bureau"
        cfg.token = "s3cret"
        cfg.headphones.mac = "AA:BB:CC:DD:EE:FF"
        cfg.peers = [PeerConfig(name="téléphone", mac="11:22:33:44:55:66")]
        cfg.transport.mode = cfgmod.TRANSPORT_RFCOMM
        cfg.transport.channel = 7
        restored = Config.from_dict(json.loads(json.dumps(cfg.to_dict())))
        self.assertEqual(restored, cfg)

    def test_unknown_key_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_dict({"device_name": "pc", "wifi": True})

    def test_unknown_transport_key_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_dict({"transport": {"mode": "unix", "port": 1234}})

    def test_bad_transport_mode(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_dict({"transport": {"mode": "carrier-pigeon"}})

    def test_channel_range(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_dict({"transport": {"mode": "rfcomm", "channel": 99}})

    def test_example_config_is_valid(self) -> None:
        """Le fichier livré dans packaging/ doit rester chargeable."""
        example = Path(__file__).resolve().parent.parent / "packaging" / "config.example.json"
        self.assertTrue(example.exists(), f"{example} manquant")
        cfg = Config.load(example)
        self.assertTrue(cfg.headphones.is_set())
        self.assertEqual([p.label() for p in cfg.peers], ["pc2", "téléphone"])
        cfg.validate()

    def test_missing_file_gives_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.load(Path(tmp) / "absent.json")
        self.assertEqual(cfg.device_name, "pc")

    def test_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "config.json"
            cfg = Config()
            cfg.token = "abc"
            cfg.headphones.name = "Mes écouteurs"
            cfg.save(path)
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = Config.load(path)
        self.assertEqual(loaded.token, "abc")
        self.assertEqual(loaded.headphones.name, "Mes écouteurs")

    def test_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ConfigError):
                Config.load(path)

    def test_socket_path_defaults_to_runtime_dir(self) -> None:
        cfg = Config()
        default = cfg.transport.resolved_socket_path()
        self.assertTrue(default.endswith("blueco.sock"))
        cfg.transport.socket_path = "/tmp/custom.sock"
        self.assertEqual(cfg.transport.resolved_socket_path(), "/tmp/custom.sock")

    def test_xdg_config_home_is_respected(self) -> None:
        previous = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg-test"
        try:
            self.assertEqual(cfgmod.config_dir(), Path("/tmp/xdg-test/blueco"))
            self.assertEqual(cfgmod.default_config_path(), Path("/tmp/xdg-test/blueco/config.json"))
        finally:
            if previous is None:
                del os.environ["XDG_CONFIG_HOME"]
            else:
                os.environ["XDG_CONFIG_HOME"] = previous


class PeerTests(unittest.TestCase):
    """Configuration multi-appareils : c'est ce qui permet PC1 → PC2 → téléphone."""

    def test_peers_list(self) -> None:
        cfg = Config.from_dict({
            "peers": [
                {"name": "bureau", "mac": "11:22:33:44:55:66"},
                {"name": "téléphone", "channel": 5},
            ]
        })
        self.assertEqual([p.label() for p in cfg.peers], ["bureau", "téléphone"])
        self.assertEqual(cfg.peers[1].channel, 5)
        self.assertEqual(cfg.peers[1].mode, "")

    def test_legacy_single_peer_is_accepted(self) -> None:
        cfg = Config.from_dict({"peer": {"mac": "11:22:33:44:55:66"}})
        self.assertEqual(len(cfg.peers), 1)
        self.assertEqual(cfg.primary_peer().mac, "11:22:33:44:55:66")

    def test_peer_and_peers_cannot_coexist(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_dict({"peer": {"mac": "11:22:33:44:55:66"},
                              "peers": [{"name": "bureau"}]})

    def test_peer_unknown_key(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_dict({"peers": [{"name": "bureau", "wifi": True}]})

    def test_peer_without_target(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_dict({"peers": [{"channel": 3}]})

    def test_duplicate_peer_name(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_dict({"peers": [{"name": "a"}, {"name": "a"}]})

    def test_invalid_mac_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_dict({"peers": [{"name": "bureau", "mac": "aa-bb-cc"}]})

    def test_invalid_peer_mode(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_dict({"peers": [{"name": "bureau", "mode": "carrier-pigeon"}]})

    def test_peers_are_not_a_list(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_dict({"peers": {"name": "bureau"}})

    def test_require_peer_resolves_unique_or_named(self) -> None:
        cfg = Config.from_dict({"peers": [{"name": "bureau"}, {"name": "téléphone"}]})
        self.assertEqual(cfg.require_peer("téléphone").name, "téléphone")
        self.assertEqual(cfg.require_peer("tél").name, "téléphone", "préfixe non ambigu")
        with self.assertRaises(ConfigError):
            cfg.require_peer()
        with self.assertRaises(ConfigError):
            cfg.require_peer("inconnu")
        self.assertIsNone(cfg.primary_peer(), "deux pairs = pas de pair « unique »")

    def test_for_peer_overrides_target(self) -> None:
        cfg = Config()
        cfg.token = "global"
        cfg.transport.mode = cfgmod.TRANSPORT_PROFILE1
        peer = PeerConfig(name="bureau", mac="11:22:33:44:55:66", mode=cfgmod.TRANSPORT_RFCOMM,
                          channel=7, token="local")
        clone = cfg.for_peer(peer)
        self.assertEqual(clone.peers, [peer])
        self.assertEqual(clone.primary_peer(), peer)
        self.assertEqual(clone.transport.mode, cfgmod.TRANSPORT_RFCOMM)
        self.assertEqual(clone.transport.channel, 7)
        self.assertEqual(clone.token, "local")
        self.assertEqual(cfg.peers, [], "la configuration d'origine ne doit pas être modifiée")

    def test_add_and_remove_peer(self) -> None:
        cfg = Config()
        cfg.add_peer(PeerConfig(name="bureau", mac="11:22:33:44:55:66"))
        cfg.add_peer(PeerConfig(name="bureau", mac="AA:BB:CC:DD:EE:FF"))
        self.assertEqual(len(cfg.peers), 1, "même étiquette = remplacement")
        self.assertEqual(cfg.peers[0].mac, "AA:BB:CC:DD:EE:FF")
        self.assertTrue(cfg.remove_peer("BUREAU"))
        self.assertFalse(cfg.remove_peer("bureau"))
        self.assertEqual(cfg.peers, [])

    def test_peers_roundtrip_through_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = Config()
            cfg.peers = [PeerConfig(name="bureau", mac="11:22:33:44:55:66"),
                         PeerConfig(name="téléphone")]
            cfg.device_name = "pc1"
            cfg.save(path)
            loaded = Config.load(path)
        self.assertEqual(loaded, cfg, "la liste de pairs doit survivre à un aller-retour disque")


if __name__ == "__main__":
    unittest.main()
