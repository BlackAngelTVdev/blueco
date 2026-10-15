"""Le démon : écoute les trames entrantes et exécute les ordres audio.

Responsabilités :

1. accepter les connexions du pair (téléphone) et tenir une :class:`Session` ;
2. répondre aux requêtes ``PING | STATUS | RELEASE | GRAB | WHOAMI`` ;
3. pousser des requêtes vers le pair (cas « je bascule depuis le PC »),
   avec file d'attente si le téléphone n'est pas connecté ;
4. journaliser chaque bascule dans un anneau d'historique consultable via ``STATUS``.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from collections import deque
from typing import Any, Callable

from . import protocol as proto
from .audio import AudioError, AudioSink
from .config import Config, ConfigError
from .session import Session
from .transport import Connection, Listener, TransportError, create_listener

log = logging.getLogger(__name__)


class BluecoServer:
    def __init__(
        self,
        config: Config,
        sink: AudioSink,
        listener: Listener | None = None,
        *,
        logger: logging.Logger | None = None,
        identity: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.sink = sink
        self.log = logger or log
        self.listener = listener or create_listener(config)
        self._identity = identity
        self._sessions: list[Session] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._started_at = time.time()
        self._queue: deque[dict] = deque()
        self._history: deque[dict] = deque(maxlen=20)
        self.error: str = ""
        self._switchboard: Any = None

    # ------------------------------------------------------------------ #
    # État
    # ------------------------------------------------------------------ #
    def status_payload(self) -> dict[str, Any]:
        audio = self.sink.status()
        payload: dict[str, Any] = {
            "device": self.config.device_name,
            "protocol": proto.PROTOCOL_VERSION,
            "audio": audio,
            "headphones_held": bool(audio.get("connected")),
            "uptime_s": round(time.time() - self._started_at, 1),
            "listener": self.listener.describe(),
            "peers": len([s for s in self._sessions if s.alive]),
            "queued": len(self._queue),
            "history": list(self._history)[-5:],
        }
        adapter = self.sink.adapter_info()
        if adapter:
            payload["adapter"] = adapter
        if self._identity is not None:
            try:
                payload["identity"] = self._identity()
            except Exception:  # pragma: no cover - défensif
                self.log.debug("identity() en échec", exc_info=True)
        return payload

    def _record(self, entry: dict[str, Any]) -> None:
        entry = dict(entry)
        entry.setdefault("at", time.strftime("%H:%M:%S"))
        self._history.append(entry)

    # ------------------------------------------------------------------ #
    # Exécution des ordres
    # ------------------------------------------------------------------ #
    def do_release(self) -> dict[str, Any]:
        result = self.sink.release()
        self._record({"event": "release", **{k: result[k] for k in ("released", "was_connected") if k in result}})
        self.broadcast_state()
        return result

    def do_grab(self) -> dict[str, Any]:
        result = self.sink.grab()
        self._record({"event": "grab", **{k: result[k] for k in ("mac", "already_connected") if k in result}})
        self.broadcast_state()
        return result

    def sessions(self) -> list[Session]:
        with self._lock:
            return [session for session in self._sessions if session.alive]

    def session_for(self, peer: Any) -> Session | None:
        """La session ouverte par ce pair (l'appareil qui a pris le lien), s'il y en a une."""
        mac = getattr(peer, "mac", "") or ""
        for candidate in (getattr(peer, "label", lambda: "")(), getattr(peer, "name", ""),
                          getattr(peer, "bluetooth_name", "")):
            for session in self.sessions():
                if session.matches(label=candidate, mac=mac):
                    return session
        return None

    def switchboard(self) -> Any:
        """Coordinateur multi-pairs, construit à la demande (résolution des noms incluse)."""
        if self._switchboard is None:
            from .switch import Switchboard

            resolver = None
            try:
                from . import bluez

                client = bluez.open_client("auto")
                resolver = lambda peer: bluez.resolve_peer_mac(peer, client)  # noqa: E731
            except Exception as exc:  # BlueZ absent : on se rabattra sur les adresses figées
                self.log.info("résolution des noms de pairs indisponible: %s", exc)
            self._switchboard = Switchboard(self.config, self.sink, resolver=resolver,
                                            server=self, logger=self.log)
        return self._switchboard

    def broadcast_state(self) -> None:
        payload = self.status_payload()
        for session in list(self._sessions):
            session.notify(proto.EVT_STATE, payload)

    # ------------------------------------------------------------------ #
    # Réception
    # ------------------------------------------------------------------ #
    def _authorized(self, message: dict) -> bool:
        expected = self.config.token
        if not expected:
            return True
        provided = message.get("token") or ""
        return hmac.compare_digest(str(provided), expected)

    def dispatch(self, message: dict, control: bool = False) -> dict:
        """Traite une requête entrante et renvoie la trame de réponse.

        ``control=True`` uniquement pour le canal local (socket unix) : c'est le
        seul endroit où ``TAKE``/``GIVE`` sont acceptés, car eux seuls ont accès
        aux sessions ouvertes par les pairs.
        """
        request_id = str(message.get("id", ""))
        command = str(message.get("cmd", "")).upper()
        args = message.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if not self._authorized(message):
            self.log.warning("requête %s refusée: jeton invalide", command)
            return proto.failure(request_id, proto.E_UNAUTHORIZED, "jeton invalide")
        if not control and command not in proto.PEER_COMMANDS:
            self.log.warning("commande locale %s refusée sur une connexion distante", command)
            return proto.failure(
                request_id, proto.E_UNSUPPORTED,
                f"{command} n'est acceptée que sur le canal de contrôle local",
            )

        self.log.info("requête reçue%s: %s %s", " (locale)" if control else "", command,
                      args if args else "")
        try:
            if command == proto.CMD_TAKE:
                result = self.switchboard().take(source=args.get("from") or None,
                                                 skip_release=bool(args.get("skip_release")))
                return proto.ok(request_id, **result)
            if command == proto.CMD_GIVE:
                result = self.switchboard().give(target=args.get("to") or None,
                                                 skip_grab=bool(args.get("skip_grab")))
                return proto.ok(request_id, **result)
            if command == proto.CMD_PING:
                return proto.ok(request_id, pong=True, device=self.config.device_name,
                                protocol=proto.PROTOCOL_VERSION)
            if command == proto.CMD_WHOAMI:
                payload = self.status_payload()
                return proto.ok(
                    request_id,
                    device=self.config.device_name,
                    protocol=proto.PROTOCOL_VERSION,
                    adapter=payload.get("adapter", {}),
                )
            if command == proto.CMD_STATUS:
                return proto.ok(request_id, **self.status_payload())
            if command == proto.CMD_RELEASE:
                return proto.ok(request_id, **self.do_release())
            if command == proto.CMD_GRAB:
                return proto.ok(request_id, **self.do_grab())
            return proto.failure(request_id, proto.E_UNSUPPORTED, f"commande non gérée: {command}")
        except AudioError as exc:
            self._record({"event": "error", "cmd": command, "code": exc.code, "message": exc.message})
            return proto.failure(request_id, exc.code, exc.message)
        except ConfigError as exc:
            # configuration incomplète : erreur attendue, pas un plantage
            return proto.failure(request_id, proto.E_CONFIG, str(exc))
        except Exception as exc:  # pragma: no cover - filet de sécurité
            self.log.exception("échec de %s", command)
            return proto.failure(request_id, proto.E_INTERNAL, str(exc))

    def handle(self, conn: Connection, control: bool = False) -> None:
        session = Session(
            conn,
            on_request=lambda message, flag=control: self.dispatch(message, control=flag),
            on_event=self._on_peer_event,
            name="control" if control else "peer",
            logger=self.log,
        ).start()
        with self._lock:
            self._sessions.append(session)
        session.notify(proto.EVT_HELLO, {
            "device": self.config.device_name,
            "protocol": proto.PROTOCOL_VERSION,
            "audio": self.sink.status(),
        })
        self._flush_queue(session)
        session.wait_closed()
        with self._lock:
            if session in self._sessions:
                self._sessions.remove(session)

    def _on_peer_event(self, message: dict) -> None:
        self.log.info("événement du pair: %s %s", message.get("evt"), message.get("data", {}))

    # ------------------------------------------------------------------ #
    # Émission vers le pair
    # ------------------------------------------------------------------ #
    def live_session(self) -> Session | None:
        with self._lock:
            for session in reversed(self._sessions):
                if session.alive:
                    return session
        return None

    def push(self, cmd: str, args: dict | None = None, timeout: float | None = None,
             allow_queue: bool = False) -> dict[str, Any]:
        """Demande une action au pair. Lève :class:`TransportError` si injoignable."""
        session = self.live_session()
        if session is None:
            if allow_queue:
                self._queue.append(proto.request(cmd, args))
                self.log.info("pair non connecté: %s mis en file d'attente", cmd)
                return {"queued": True, "cmd": cmd, "queued_count": len(self._queue)}
            raise TransportError("aucun pair connecté (lancez l'appli Android)")
        result = session.request(cmd, args, timeout=timeout or self.config.request_timeout_s,
                                 token=self.config.token or None)
        return result

    def _flush_queue(self, session: Session) -> None:
        if not self._queue:
            return

        def worker() -> None:
            while self._queue and session.alive:
                message = self._queue[0]
                try:
                    result = session.request(
                        str(message["cmd"]),
                        message.get("args"),
                        timeout=self.config.request_timeout_s,
                        token=self.config.token or None,
                    )
                except (proto.RemoteError, TimeoutError, TransportError) as exc:
                    self.log.warning("commande en file non exécutée (%s): %s", message.get("cmd"), exc)
                    break
                self._queue.popleft()
                self.log.info("commande en file exécutée: %s -> %s", message.get("cmd"), result)
                self._record({"event": "queued", "cmd": message.get("cmd"), "result": result})

        threading.Thread(target=worker, name="blueco-queue", daemon=True).start()

    # ------------------------------------------------------------------ #
    # Boucle principale
    # ------------------------------------------------------------------ #
    def serve_forever(self, stop: threading.Event | None = None, poll: float = 0.5) -> None:
        stop = stop or self._stop
        self.log.info("blueco « %s » écoute sur %s", self.config.device_name, self.listener.describe())
        try:
            while not stop.is_set():
                try:
                    conn = self.listener.accept(timeout=poll)
                except TransportError as exc:
                    if stop.is_set():
                        self.log.debug("écoute interrompue: %s", exc)
                    else:
                        self.log.error("erreur d'écoute: %s", exc)
                    break
                if conn is None:
                    continue
                threading.Thread(target=self.handle, args=(conn,),
                                 name="blueco-conn", daemon=True).start()
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()
        try:
            self.listener.close()
        except Exception:  # pragma: no cover
            self.log.debug("fermeture de l'écouteur en échec", exc_info=True)
        self.log.info("démon arrêté")
