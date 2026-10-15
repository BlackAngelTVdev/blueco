"""Tests de la couche BlueZ (macOS du parseur et logique de recherche)."""

from __future__ import annotations

import unittest

from blueco.bluez import (
    BluezClient,
    BluezCommandClient,
    BluezError,
    DeviceInfo,
    is_mac,
    normalize_mac,
)

DEVICES_OUT = """\
Device AA:BB:CC:DD:EE:FF Mes Écouteurs
Device 11:22:33:44:55:66 Téléphone
Device not-a-mac Ligne invalide
"""

INFO_OUT = """\
Device AA:BB:CC:DD:EE:FF (public)
\tName: Mes Écouteurs
\tAlias: Mes Écouteurs
\tPaired: yes
\tTrusted: yes
\tConnected: yes
"""


class HelperTests(unittest.TestCase):
    def test_is_mac(self) -> None:
        self.assertTrue(is_mac("aa:bb:cc:dd:ee:ff"))
        self.assertTrue(is_mac("AA:BB:CC:DD:EE:FF"))
        self.assertFalse(is_mac("aa-bb-cc-dd-ee-ff"))
        self.assertFalse(is_mac("hello"))

    def test_normalize(self) -> None:
        self.assertEqual(normalize_mac(" aa:bb:cc:dd:ee:ff "), "AA:BB:CC:DD:EE:FF")


class CommandClientTests(unittest.TestCase):
    def _client(self, responses: dict[tuple[str, ...], str]) -> BluezCommandClient:
        client = BluezCommandClient.__new__(BluezCommandClient)

        def fake_run(*args: str) -> str:
            for key, value in responses.items():
                if key == args:
                    return value
            raise AssertionError(f"appel inattendu: {args}")

        client._run = fake_run  # type: ignore[method-assign]
        client._binary = "bluetoothctl"
        client._timeout = 5.0
        return client

    def test_devices_parsing(self) -> None:
        client = self._client({("devices",): DEVICES_OUT})
        devices = client.devices()
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0].address, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(devices[0].name, "Mes Écouteurs")

    def test_info_parsing(self) -> None:
        client = self._client({("devices",): DEVICES_OUT,
                               ("info", "AA:BB:CC:DD:EE:FF"): INFO_OUT})
        device = client.find_device("AA:BB:CC:DD:EE:FF")
        self.assertTrue(device.connected)
        self.assertTrue(device.paired)
        self.assertEqual(device.name, "Mes Écouteurs")

    def test_find_by_name_is_case_insensitive(self) -> None:
        client = self._client({("devices",): DEVICES_OUT,
                               ("info", "11:22:33:44:55:66"): "Alias: Téléphone\nConnected: no\n"})
        device = client.find_device(name="téléphone")
        self.assertEqual(device.address, "11:22:33:44:55:66")
        self.assertFalse(device.connected)

    def test_find_unknown_device(self) -> None:
        client = self._client({("devices",): DEVICES_OUT})
        with self.assertRaises(BluezError):
            client.find_device("FF:FF:FF:FF:FF:FF")

    def test_ambiguous_name(self) -> None:
        client = self._client({("devices",): "Device AA:AA:AA:AA:AA:AA Buds\nDevice BB:BB:BB:BB:BB:BB Buds Pro\n"})
        with self.assertRaises(BluezError):
            client.find_device(name="buds")

    def test_connect_failure(self) -> None:
        client = self._client({("connect", "AA:BB:CC:DD:EE:FF"): "Failed to connect: org.bluez.Error.Failed"})
        with self.assertRaises(BluezError):
            client.connect("AA:BB:CC:DD:EE:FF")

    def test_disconnect_already_disconnected(self) -> None:
        client = self._client({
            ("disconnect", "AA:BB:CC:DD:EE:FF"): "Failed to disconnect: Not connected",
            ("devices",): DEVICES_OUT,
            ("info", "AA:BB:CC:DD:EE:FF"): "Alias: Mes Écouteurs\nConnected: no\n",
        })
        device = client.disconnect("AA:BB:CC:DD:EE:FF")
        self.assertFalse(device.connected)

    def test_no_target(self) -> None:
        client = self._client({("devices",): DEVICES_OUT})
        with self.assertRaises(BluezError):
            client.find_device()


class DbusClientLogicTests(unittest.TestCase):
    """La logique de recherche est testée sans bus D-Bus (``devices()`` stubbé)."""

    def _client(self, devices: list[DeviceInfo]) -> BluezClient:
        client = BluezClient.__new__(BluezClient)
        client.devices = lambda: list(devices)  # type: ignore[method-assign]
        return client

    def setUp(self) -> None:
        self.buds = DeviceInfo(path="/org/bluez/hci0/dev_AA", address="AA:BB:CC:DD:EE:FF",
                               name="Mes Écouteurs", connected=True, paired=True, trusted=True)
        self.phone = DeviceInfo(path="/org/bluez/hci0/dev_11", address="11:22:33:44:55:66",
                                name="Téléphone", connected=False, paired=True, trusted=False)

    def test_find_by_address(self) -> None:
        self.assertEqual(self._client([self.buds, self.phone]).find_device("aa:bb:cc:dd:ee:ff"),
                         self.buds)

    def test_find_by_partial_name(self) -> None:
        self.assertEqual(self._client([self.buds, self.phone]).find_device(name="écout"),
                         self.buds)

    def test_single_connected_device_is_used_by_default(self) -> None:
        self.assertEqual(self._client([self.buds, self.phone]).find_device(), self.buds)

    def test_no_connected_device(self) -> None:
        offline = DeviceInfo(path="/x", address="AA:BB:CC:DD:EE:FF", name="Buds",
                             connected=False, paired=True, trusted=True)
        with self.assertRaises(BluezError):
            self._client([offline]).find_device()

    def test_device_as_dict(self) -> None:
        payload = self.buds.as_dict()
        self.assertEqual(payload["mac"], "AA:BB:CC:DD:EE:FF")
        self.assertTrue(payload["connected"])


if __name__ == "__main__":
    unittest.main()
