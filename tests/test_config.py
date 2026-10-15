"""Tests de la configuration."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from blueco import config as cfgmod
from blueco.config import Config, ConfigError


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
        cfg.peer.mac = "11:22:33:44:55:66"
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
        self.assertTrue(cfg.peer.is_set())
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


if __name__ == "__main__":
    unittest.main()
