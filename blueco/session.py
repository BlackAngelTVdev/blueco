"""Session bidirectionnelle : requêtes/réponses et événements sur une connexion.

Le protocole est symétrique : les deux extrémités peuvent appeler
``session.request(...)`` et l'autre répond automatiquement via le gestionnaire
``on_request`` fourni. C'est ce qui permet au PC de pousser un ``RELEASE`` vers
le téléphone sur la connexion ouverte par ce dernier.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import protocol as proto
from .transport import Connection, TransportError

log = logging.getLogger(__name__)


class _Pending:
    __slots__ = ("event", "reply")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.reply: dict | None = None


class Session:
    """Enveloppe une :class:`Connection` avec un fil de lecture et des appels synchrones."""

    def __init__(
        self,
        conn: Connection,
        on_request: Callable[[dict], dict] | None = None,
        on_event: Callable[[dict], None] | None = None,
        *,
        name: str = "peer",
        logger: logging.Logger | None = None,
        max_workers: int = 2,
    ) -> None:
        self.conn = conn
        self.name = name
        self.log = logger or log
        self._on_request = on_request
        self._on_event = on_event
        #: informations apprises sur le pair (nom annoncé dans HELLO, adresse…)
        self.metadata: dict[str, Any] = {}
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"blueco-{name}")
        self._reader = threading.Thread(target=self._read_loop, name=f"blueco-read-{name}", daemon=True)

    # -- cycle de vie ------------------------------------------------------- #
    def start(self) -> "Session":
        self._reader.start()
        return self

    def wait_closed(self, timeout: float | None = None) -> bool:
        """Bloque jusqu'à la fermeture de la connexion. ``True`` si fermée."""
        self._reader.join(timeout)
        return not self._reader.is_alive()

    @property
    def alive(self) -> bool:
        return not self._closed.is_set() and not self.conn.closed

    def close(self) -> None:
        self._closed.set()
        self.conn.close()
        self._fail_pending(TransportError(f"session {self.name} fermée"))
        self._pool.shutdown(wait=False)

    def __enter__(self) -> "Session":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- envoi -------------------------------------------------------------- #
    def send(self, message: dict) -> None:
        with self._send_lock:
            self.conn.send(message)

    def notify(self, name: str, data: dict | None = None) -> None:
        """Envoie un événement non sollicité (silencieux si la connexion est morte)."""
        if not self.alive:
            return
        try:
            self.send(proto.event(name, data))
        except TransportError:
            self.log.debug("événement %s non envoyé (connexion fermée)", name)

    def request(self, cmd: str, args: dict | None = None, timeout: float | None = None,
                token: str | None = None) -> dict:
        """Envoie une requête et attend la réponse. Lève :class:`proto.RemoteError` si refus."""
        if not self.alive:
            raise TransportError(f"session {self.name} fermée")
        message = proto.request(cmd, args, token=token)
        request_id = message["id"]
        pending = _Pending()
        with self._lock:
            self._pending[request_id] = pending
        try:
            self.send(message)
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        if not pending.event.wait(timeout if timeout is not None else proto.DEFAULT_TIMEOUT):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"{cmd}: aucune réponse de {self.name}")
        reply = pending.reply or {}
        return proto.result_of(reply)

    # -- réception ---------------------------------------------------------- #
    def _read_loop(self) -> None:
        errors = 0
        try:
            while not self._closed.is_set():
                try:
                    message = self.conn.recv(timeout=1.0)
                except TimeoutError:
                    continue
                except proto.ProtocolError as exc:
                    # Trame illisible : on ne peut pas connaître l'``id``, donc on
                    # ne répond pas (sinon deux implémentations se renverraient
                    # des erreurs à l'infini).
                    if exc.code == proto.E_NOT_CONNECTED:
                        self.log.debug("connexion %s perdue: %s", self.name, exc)
                        break
                    errors += 1
                    self.log.warning("trame invalide de %s (%d): %s", self.name, errors, exc)
                    if errors > 5:
                        self.log.error("trop de trames invalides de %s: fermeture", self.name)
                        break
                    continue
                except TransportError as exc:
                    self.log.debug("connexion perdue avec %s: %s", self.name, exc)
                    break
                if message is None:
                    self.log.info("connexion fermée par %s", self.name)
                    break
                self._dispatch_incoming(message)
        finally:
            self._closed.set()
            self.conn.close()
            self._fail_pending(TransportError(f"connexion {self.name} fermée"))

    def _dispatch_incoming(self, message: dict) -> None:
        if proto.is_reply(message):
            request_id = str(message.get("id", ""))
            with self._lock:
                pending = self._pending.pop(request_id, None)
            if pending is None:
                self.log.debug("réponse orpheline (%s)", request_id)
                return
            pending.reply = message
            pending.event.set()
            return
        if "evt" in message:
            self._learn(message)
            if self._on_event is not None:
                try:
                    self._on_event(message)
                except Exception:  # pragma: no cover - le pair ne doit pas nous tuer
                    self.log.exception("gestionnaire d'événement en échec")
            return
        # requête entrante
        if self._on_request is None:
            self.send_reply(proto.failure(str(message.get("id", "")), proto.E_UNSUPPORTED, "aucun gestionnaire"))
            return
        self._pool.submit(self._run_handler, message)

    def _run_handler(self, message: dict) -> None:
        try:
            reply = self._on_request(message)  # type: ignore[misc]
        except proto.RemoteError as exc:  # pragma: no cover - défensif
            reply = proto.failure(str(message.get("id", "")), exc.code, exc.message)
        except Exception as exc:
            self.log.exception("gestion de %s en échec", message.get("cmd"))
            reply = proto.failure(str(message.get("id", "")), proto.E_INTERNAL, str(exc))
        self.send_reply(reply)

    def _learn(self, message: dict) -> None:
        """Retient ce que le pair annonce de lui (``HELLO``) pour pouvoir le cibler."""
        if message.get("evt") != proto.EVT_HELLO:
            return
        data = message.get("data")
        if isinstance(data, dict):
            device = data.get("device")
            if isinstance(device, str) and device:
                self.metadata["device"] = device
            mac = data.get("mac")
            if isinstance(mac, str) and mac:
                self.metadata["mac"] = mac.upper()

    @property
    def address(self) -> str:
        """Adresse du pair, soit annoncée, soit déduite du socket."""
        announced = self.metadata.get("mac")
        if announced:
            return str(announced)
        candidate = str(getattr(self.conn, "peer", "") or "")
        parts = candidate.split(":")
        if len(parts) == 7 and all(len(part) == 2 for part in parts):
            return ":".join(parts[:6])  # « AA:BB:..:FF:canal » → l'adresse seule
        return candidate

    def matches(self, label: str = "", mac: str = "") -> bool:
        """Ce pair est-il celui décrit par ``label``/``mac`` ?"""
        if mac and self.address.upper() == mac.upper():
            return True
        announced = str(self.metadata.get("device", ""))
        if not label or not announced:
            return False
        return announced.strip().lower() == label.strip().lower()

    def send_reply(self, reply: dict) -> None:
        try:
            self.send(reply)
        except TransportError:
            self.log.debug("réponse non envoyée (connexion fermée)")

    # -- utilitaires -------------------------------------------------------- #
    def _fail_pending(self, exc: Exception) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.reply = proto.failure("", proto.E_NOT_CONNECTED, str(exc))
            item.event.set()
