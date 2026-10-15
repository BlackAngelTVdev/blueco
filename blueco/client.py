"""Côté client : envoyer des ordres au pair (le téléphone Android).

Deux usages :

* :meth:`BluecoClient.call` - une requête, une connexion, on referme (script, CLI) ;
* :meth:`BluecoClient.session` - session persistante, utilisée par ``watch`` et par
  un futur mode « bouton sur le PC ».
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from . import protocol as proto
from .config import Config
from .session import Session
from .transport import Connection, TransportError, connect_to

log = logging.getLogger(__name__)

Connector = Callable[..., Connection]


class BluecoClient:
    def __init__(
        self,
        config: Config,
        connector: Connector | None = None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.log = logger or log
        self._connector = connector or connect_to

    # -- connexions --------------------------------------------------------- #
    def open(self, timeout: float | None = None) -> Connection:
        return self._connector(self.config, timeout=timeout or self.config.request_timeout_s)

    def session(self, timeout: float | None = None) -> Session:
        """Ouvre une session persistante (l'appelant doit la fermer)."""
        return Session(self.open(timeout), name="linux-client", logger=self.log).start()

    # -- requêtes ----------------------------------------------------------- #
    def call(self, cmd: str, args: dict | None = None, timeout: float | None = None) -> dict[str, Any]:
        """Requête ponctuelle : ouvre, demande, referme."""
        deadline = timeout or self.config.request_timeout_s
        conn = self.open(deadline)
        try:
            session = Session(conn, name="linux-client", logger=self.log).start()
            try:
                return session.request(cmd, args, timeout=deadline, token=self.config.token or None)
            finally:
                session.close()
        except TransportError:
            raise

    def ping(self, timeout: float | None = None) -> dict[str, Any]:
        return self.call(proto.CMD_PING, timeout=timeout)

    def status(self, timeout: float | None = None) -> dict[str, Any]:
        return self.call(proto.CMD_STATUS, timeout=timeout)

    def whoami(self, timeout: float | None = None) -> dict[str, Any]:
        return self.call(proto.CMD_WHOAMI, timeout=timeout)

    def release(self, timeout: float | None = None) -> dict[str, Any]:
        """Demande au pair de libérer le casque."""
        return self.call(proto.CMD_RELEASE, timeout=timeout)

    def grab(self, timeout: float | None = None) -> dict[str, Any]:
        """Demande au pair de reprendre le casque."""
        return self.call(proto.CMD_GRAB, timeout=timeout)
