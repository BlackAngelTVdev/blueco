"""Canal de contrôle local.

Le démon (`blueco serve`) est le **seul** processus qui voit la connexion ouverte
par le téléphone : c'est lui qui reçoit le lien SPP entrant. Or `blueco take`
est un autre processus. Pour qu'il puisse s'en servir (pousser un ``RELEASE`` au
téléphone), le démon écoute en plus de son profil Bluetooth une **socket unix
locale** (0600, dans ``$XDG_RUNTIME_DIR/blueco``) sur laquelle il accepte les
commandes de pilotage ``TAKE`` et ``GIVE``.

Ce canal n'est jamais exposé sur Bluetooth : `PEER_COMMANDS` filtre ce qui est
acceptable depuis une connexion distante (voir ``server.dispatch``).
"""

from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path
from typing import Any

from .client import BluecoClient
from .config import TRANSPORT_UNIX, Config
from .transport import TransportError, UnixListener

log = logging.getLogger(__name__)


class ControlUnavailable(RuntimeError):
    """Aucun démon à l'écoute, ou canal local inaccessible."""


class ControlServer:
    """Expose le démon sur une socket unix locale."""

    def __init__(self, bridge: Any, path: str | None = None,
                 logger: logging.Logger | None = None) -> None:
        self.bridge = bridge
        self.log = logger or log
        self.listener = UnixListener(path or bridge.config.transport.resolved_control_socket())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "ControlServer":
        self._thread = threading.Thread(target=self.serve_forever, name="blueco-control", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self.log.info("canal de contrôle local sur %s", self.listener.describe())
        while not self._stop.is_set():
            try:
                conn = self.listener.accept(timeout=0.5)
            except TransportError as exc:
                if not self._stop.is_set():
                    self.log.error("canal de contrôle: %s", exc)
                break
            if conn is None:
                continue
            threading.Thread(target=self.bridge.handle, args=(conn, True),
                             name="blueco-control-conn", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.listener.close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def daemon_running(config: Config) -> bool:
    """Un démon écoute-t-il sur le canal de contrôle ?"""
    path = Path(config.transport.resolved_control_socket())
    return path.exists()


def call_control(config: Config, command: str, args: dict[str, Any] | None = None,
                 timeout: float | None = None) -> dict[str, Any]:
    """Envoie une commande locale au démon. Lève :class:`ControlUnavailable`."""
    path = config.transport.resolved_control_socket()
    if not Path(path).exists():
        raise ControlUnavailable(f"aucun démon à l'écoute ({path})")
    local = copy.deepcopy(config)
    local.transport.mode = TRANSPORT_UNIX
    local.transport.socket_path = path
    try:
        return BluecoClient(local).call(command, args, timeout=timeout)
    except TransportError as exc:
        raise ControlUnavailable(f"canal de contrôle inaccessible: {exc}") from exc
