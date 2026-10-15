"""Tests des backends audio."""

from __future__ import annotations

import unittest
from dataclasses import replace

from blueco import protocol as proto
from blueco.audio import AudioError, BluezAudioSink, FakeAudioSink, create_sink
from blueco.bluez import BluezError, DeviceInfo
from blueco.config import AUDIO_BLUEZ, AUDIO_FAKE, Config


class StubClient:
    """Double de test de BluezClient."""

    def __init__(self, connected: bool = True, missing: bool = False) -> None:
        self.missing = missing
        self.device = DeviceInfo(
            path="/org/bluez/hci0/dev_AA",
            address="AA:BB:CC:DD:EE:FF",
            name="Mes Écouteurs",
            connected=connected,
            paired=True,
            trusted=True,
        )
        self.calls: list[tuple[str, str]] = []

    def find_device(self, address: str = "", name: str = "") -> DeviceInfo:
        if self.missing:
            raise BluezError("appareil inconnu de BlueZ")
        return self.device

    def connect(self, address: str) -> DeviceInfo:
        self.calls.append(("connect", address))
        self.device = replace(self.device, connected=True)
        return self.device

    def disconnect(self, address: str) -> DeviceInfo:
        self.calls.append(("disconnect", address))
        self.device = replace(self.device, connected=False)
        return self.device

    def adapter_info(self) -> dict:
        return {"address": "11:22:33:44:55:66", "name": "hci0", "powered": True}


class FakeSinkTests(unittest.TestCase):
    def test_release_then_grab(self) -> None:
        sink = FakeAudioSink()
        self.assertTrue(sink.status()["connected"])
        released = sink.release()
        self.assertTrue(released["was_connected"])
        self.assertFalse(sink.connected)
        again = sink.release()
        self.assertTrue(again["already_free"])
        grabbed = sink.grab()
        self.assertTrue(grabbed["connected"])
        self.assertEqual(sink.calls, ["release", "release", "grab"])

    def test_injected_failure(self) -> None:
        sink = FakeAudioSink(fail_on={"grab"})
        with self.assertRaises(AudioError) as ctx:
            sink.grab()
        self.assertEqual(ctx.exception.code, proto.E_AUDIO_UNAVAILABLE)

    def test_status_shape(self) -> None:
        status = FakeAudioSink(mac="11:22:33:44:55:66", name="Buds").status()
        self.assertEqual(status["mac"], "11:22:33:44:55:66")
        self.assertEqual(status["backend"], "fake")


class BluezSinkTests(unittest.TestCase):
    def test_release_disconnects(self) -> None:
        client = StubClient(connected=True)
        sink = BluezAudioSink(client, mac="AA:BB:CC:DD:EE:FF")
        self.assertTrue(sink.status()["connected"])
        result = sink.release()
        self.assertTrue(result["was_connected"])
        self.assertEqual(client.calls, [("disconnect", "AA:BB:CC:DD:EE:FF")])

    def test_release_is_idempotent(self) -> None:
        client = StubClient(connected=False)
        sink = BluezAudioSink(client, mac="AA:BB:CC:DD:EE:FF")
        result = sink.release()
        self.assertTrue(result["already_free"])
        self.assertEqual(client.calls, [])

    def test_grab_connects(self) -> None:
        client = StubClient(connected=False)
        sink = BluezAudioSink(client, mac="AA:BB:CC:DD:EE:FF")
        result = sink.grab()
        self.assertTrue(result["connected"])
        self.assertFalse(result["already_connected"])
        self.assertEqual(client.calls, [("connect", "AA:BB:CC:DD:EE:FF")])

    def test_missing_device_raises_no_device(self) -> None:
        sink = BluezAudioSink(StubClient(missing=True), name="Introuvable")
        with self.assertRaises(AudioError) as ctx:
            sink.release()
        self.assertEqual(ctx.exception.code, proto.E_NO_DEVICE)
        status = sink.status()
        self.assertFalse(status["found"])

    def test_adapter_info_is_exposed(self) -> None:
        sink = BluezAudioSink(StubClient(), mac="AA:BB:CC:DD:EE:FF")
        self.assertEqual(sink.adapter_info()["address"], "11:22:33:44:55:66")


class FactoryTests(unittest.TestCase):
    def test_fake_backend(self) -> None:
        cfg = Config()
        cfg.audio_backend = AUDIO_FAKE
        cfg.headphones.mac = "11:22:33:44:55:66"
        sink = create_sink(cfg)
        self.assertIsInstance(sink, FakeAudioSink)
        self.assertEqual(sink.mac, "11:22:33:44:55:66")

    def test_bluez_backend(self) -> None:
        cfg = Config()
        cfg.audio_backend = AUDIO_BLUEZ
        cfg.headphones.mac = "AA:BB:CC:DD:EE:FF"
        sink = create_sink(cfg, StubClient())
        self.assertIsInstance(sink, BluezAudioSink)
        self.assertEqual(sink.status()["mac"], "AA:BB:CC:DD:EE:FF")

    def test_bluez_without_client_falls_back_to_fake(self) -> None:
        cfg = Config()
        cfg.audio_backend = AUDIO_BLUEZ
        self.assertIsInstance(create_sink(cfg, None), FakeAudioSink)


if __name__ == "__main__":
    unittest.main()
