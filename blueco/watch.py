"""Verrouillage de session quand le téléphone s'éloigne (fonctionnalité optionnelle).

Le lien Bluetooth est notre seule source de vérité :

* on interroge le pair avec ``PING`` à intervalle régulier ;
* après ``failures`` échecs consécutifs (ou si le RSSI passe sous un seuil quand
  un outil permet de le lire), on exécute ``lock_command`` ;
* au retour du pair, on exécute ``unlock_command`` si configuré, et on peut
  reprendre le casque (``--grab-on-return``).

La lecture du RSSI en BR/EDR n'est pas exposée par BlueZ via D-Bus ; on tente
``hcitool rssi <mac>`` (paquet ``bluez-utils`` avec l'outil historique, absent de
nombreuses distributions récentes). Si l'outil est absent, on se rabat sur la
simple présence du lien, ce qui suffit pour « je m'éloigne ».
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from typing import Any, Callable

from . import protocol as proto
from .client import BluecoClient
from .config import Config

log = logging.getLogger(__name__)

RSSI_RE = re.compile(r"RSSI return value:\s*(-?\d+)")


def read_rssi(mac: str, timeout: float = 5.0) -> int | None:
    """RSSI en dBm du lien BR/EDR, ou ``None`` si indisponible sur cet hôte."""
    binary = shutil.which("hcitool")
    if not binary or not mac:
        return None
    try:
        completed = subprocess.run([binary, "rssi", mac], capture_output=True, text=True,
                                   timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = RSSI_RE.search(completed.stdout or "")
    return int(match.group(1)) if match else None


def run_command(command: str) -> int:
    """Exécute une commande shell (verrouillage) et renvoie son code de retour."""
    if not command:
        return 0
    log.info("exécution: %s", command)
    try:
        completed = subprocess.run(command, shell=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("commande « %s » en échec: %s", command, exc)
        return 1
    return completed.returncode


class PresenceWatcher:
    def __init__(
        self,
        config: Config,
        client: BluecoClient | None = None,
        *,
        interval: float = 10.0,
        failures: int = 3,
        lock_command: str | None = None,
        unlock_command: str = "",
        rssi_threshold: int | None = None,
        grab_on_return: bool = False,
        on_change: Callable[[bool], None] | None = None,
    ) -> None:
        self.config = config
        self.client = client or BluecoClient(config)
        self.interval = interval
        self.failures = max(1, failures)
        self.lock_command = config.lock_command if lock_command is None else lock_command
        self.unlock_command = unlock_command
        self.rssi_threshold = rssi_threshold
        self.grab_on_return = grab_on_return
        self.on_change = on_change
        self.present = True
        self.locked = False
        self.misses = 0

    def _set_present(self, present: bool) -> None:
        if present == self.present:
            return
        self.present = present
        if self.on_change is not None:
            try:
                self.on_change(present)
            except Exception:  # pragma: no cover - défensif
                log.debug("on_change en échec", exc_info=True)

    def tick(self) -> dict[str, Any]:
        """Un cycle : ping du pair, lecture du RSSI, décision verrouillage/déverrouillage."""
        outcome: dict[str, Any] = {"present": self.present, "locked": self.locked}
        reachable = False
        try:
            self.client.ping(timeout=min(self.interval, 10.0))
            reachable = True
        except Exception as exc:  # TimeoutError, TransportError, RemoteError
            log.debug("pair injoignable: %s", exc)

        rssi = read_rssi(self.config.peer.mac) if self.rssi_threshold is not None else None
        outcome["rssi"] = rssi

        if reachable and self.rssi_threshold is not None and rssi is not None:
            if rssi < self.rssi_threshold:
                log.info("RSSI %s dBm sous le seuil %s dBm", rssi, self.rssi_threshold)
                reachable = False

        if reachable:
            self.misses = 0
            self._set_present(True)
            if self.locked:
                self.locked = False
                if self.unlock_command:
                    run_command(self.unlock_command)
                if self.grab_on_return:
                    try:
                        self.client.grab()
                    except Exception as exc:
                        log.warning("reprise du casque impossible: %s", exc)
        else:
            self.misses += 1
            if self.misses >= self.failures:
                self._set_present(False)
                if not self.locked:
                    log.warning("pair absent depuis %s cycles: verrouillage", self.misses)
                    if run_command(self.lock_command) == 0:
                        self.locked = True

        outcome.update({"present": self.present, "locked": self.locked, "misses": self.misses})
        return outcome

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        log.info("surveillance du pair toutes les %.1fs (verrouillage après %d échecs)",
                 self.interval, self.failures)
        while not stop.is_set():
            try:
                outcome = self.tick()
                log.debug("état surveillance: %s", outcome)
            except Exception:  # pragma: no cover - le veilleur ne doit jamais mourir
                log.exception("cycle de surveillance en échec")
            stop.wait(self.interval)


def peer_state_event(client: BluecoClient, timeout: float = 5.0) -> dict:
    """Petit utilitaire : ping puis état du pair, utilisé par ``blueco status --remote``."""
    try:
        return {"reachable": True, "status": client.status(timeout=timeout)}
    except Exception as exc:
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}",
                "code": getattr(exc, "code", None) or proto.E_NOT_CONNECTED}
