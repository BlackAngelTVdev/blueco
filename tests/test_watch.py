"""Tests de la surveillance de proximité (verrouillage quand le téléphone part)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from blueco.config import Config
from blueco.transport import TransportError
from blueco.watch import PresenceWatcher, read_rssi, run_command


class StubClient:
    def __init__(self) -> None:
        self.reachable = True
        self.grabs = 0

    def ping(self, timeout: float | None = None) -> dict:
        if not self.reachable:
            raise TransportError("pair injoignable")
        return {"pong": True}

    def grab(self, timeout: float | None = None) -> dict:
        self.grabs += 1
        return {"connected": True}


class PresenceWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = Path(self.tmp.name) / "locked"
        self.unlocked = Path(self.tmp.name) / "unlocked"
        self.config = Config()
        self.client = StubClient()
        self.watcher = PresenceWatcher(
            self.config,
            self.client,  # type: ignore[arg-type]
            interval=0.01,
            failures=2,
            lock_command=f"touch {self.marker}",
            unlock_command=f"touch {self.unlocked}",
            grab_on_return=True,
        )

    def test_present_peer_does_nothing(self) -> None:
        outcome = self.watcher.tick()
        self.assertTrue(outcome["present"])
        self.assertFalse(outcome["locked"])
        self.assertFalse(self.marker.exists())

    def test_locks_after_consecutive_failures(self) -> None:
        self.client.reachable = False
        first = self.watcher.tick()
        self.assertTrue(first["present"], "un seul échec ne doit pas verrouiller")
        second = self.watcher.tick()
        self.assertFalse(second["present"])
        self.assertTrue(second["locked"])
        self.assertTrue(self.marker.exists())

    def test_unlocks_and_grabs_when_peer_returns(self) -> None:
        self.client.reachable = False
        self.watcher.tick()
        self.watcher.tick()
        self.client.reachable = True
        outcome = self.watcher.tick()
        self.assertTrue(outcome["present"])
        self.assertFalse(outcome["locked"])
        self.assertTrue(self.unlocked.exists())
        self.assertEqual(self.client.grabs, 1)

    def test_lock_command_is_only_run_once(self) -> None:
        self.client.reachable = False
        self.watcher.tick()
        self.watcher.tick()
        stamp = self.marker.stat().st_mtime_ns
        self.watcher.tick()
        self.assertEqual(self.marker.stat().st_mtime_ns, stamp, "verrouillage exécuté deux fois")

    def test_threshold_without_rssi_tool_falls_back_to_presence(self) -> None:
        watcher = PresenceWatcher(self.config, self.client,  # type: ignore[arg-type]
                                  failures=1, rssi_threshold=-60, lock_command="")
        outcome = watcher.tick()
        self.assertTrue(outcome["present"])
        self.assertTrue(outcome["locked"] is False)


class CommandTests(unittest.TestCase):
    def test_run_command_success(self) -> None:
        self.assertEqual(run_command("true"), 0)

    def test_run_command_failure(self) -> None:
        self.assertEqual(run_command("false"), 1)

    def test_run_command_empty(self) -> None:
        self.assertEqual(run_command(""), 0)

    def test_read_rssi_without_hcitool(self) -> None:
        # hcitool est absent de la plupart des distributions récentes :
        # la fonction doit alors renvoyer None sans lever.
        self.assertIsNone(read_rssi("AA:BB:CC:DD:EE:FF"))

    def test_read_rssi_without_mac(self) -> None:
        self.assertIsNone(read_rssi(""))


if __name__ == "__main__":
    unittest.main()
