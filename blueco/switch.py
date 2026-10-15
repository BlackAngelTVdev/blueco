"""Orchestration multi-appareils.

Avec plusieurs appareils, la question n'est plus « envoyer une commande à *mon*
pair » mais « **qui** tient les écouteurs en ce moment ? ». Ce module répond à
cette question en interrogeant chaque pair configuré, puis fait lâcher celui qui
les tient avant de se connecter localement.

Aucune commande nouvelle n'est nécessaire : on réutilise ``STATUS`` (qui tient
les écouteurs ?) et ``RELEASE``/``GRAB``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from . import protocol as proto
from .audio import AudioError, AudioSink
from .client import BluecoClient
from .config import Config, ConfigError, PeerConfig
from .session import Session
from .transport import TransportError

log = logging.getLogger(__name__)

Resolver = Callable[[PeerConfig], str]
ClientFactory = Callable[[Config], BluecoClient]


@dataclass
class PeerReport:
    """État d'un pair au moment de l'interrogation."""

    peer: PeerConfig
    reachable: bool = False
    mac: str = ""
    status: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_code: str = ""

    @property
    def label(self) -> str:
        return self.peer.label()

    @property
    def holds_headphones(self) -> bool:
        return bool(self.status.get("headphones_held"))

    @property
    def audio(self) -> dict[str, Any]:
        audio = self.status.get("audio")
        return audio if isinstance(audio, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "peer": self.label,
            "reachable": self.reachable,
            "mac": self.mac,
            "holds_headphones": self.holds_headphones,
            "device": self.status.get("device", ""),
            "audio": self.audio,
            "error": self.error,
            "error_code": self.error_code,
        }


class SessionClient:
    """Client qui parle sur une connexion **déjà ouverte** par le pair.

    C'est le cas du téléphone : il ouvre lui-même le lien SPP, donc c'est par
    cette connexion (et non en le rappelant) qu'on lui demande de libérer les
    écouteurs. Sans cela, le PC ne pourrait faire lâcher que les appareils qu'il
    sait rappeler par un canal connu.
    """

    def __init__(self, config: Config, session: Session, timeout: float) -> None:
        self.config = config
        self.session = session
        self._timeout = timeout

    @property
    def label(self) -> str:
        return self.config.peers[0].label()

    def ping(self, timeout: float | None = None) -> dict[str, Any]:
        return self._call(proto.CMD_PING, timeout)

    def status(self, timeout: float | None = None) -> dict[str, Any]:
        return self._call(proto.CMD_STATUS, timeout)

    def whoami(self, timeout: float | None = None) -> dict[str, Any]:
        return self._call(proto.CMD_WHOAMI, timeout)

    def release(self, timeout: float | None = None) -> dict[str, Any]:
        return self._call(proto.CMD_RELEASE, timeout)

    def grab(self, timeout: float | None = None) -> dict[str, Any]:
        return self._call(proto.CMD_GRAB, timeout)

    def _call(self, command: str, timeout: float | None = None) -> dict[str, Any]:
        return self.session.request(command, timeout=timeout or self._timeout,
                                    token=self.config.token or None)


class Switchboard:
    """Coordonne la bascule entre plusieurs appareils.

    ``resolver`` traduit un nom de pair en adresse MAC (BlueZ par défaut),
    ``client_factory`` construit le client de chaque pair et ``server`` fournit
    les connexions déjà ouvertes par les pairs : les trois sont injectables, ce
    qui rend toute la logique testable sans matériel.
    """

    def __init__(
        self,
        config: Config,
        sink: AudioSink,
        *,
        resolver: Resolver | None = None,
        client_factory: ClientFactory | None = None,
        server: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.sink = sink
        self.log = logger or log
        self._resolver = resolver
        self._server = server
        self._client_factory = client_factory or (lambda cfg: BluecoClient(cfg, logger=self.log))
        self._clients: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Pairs
    # ------------------------------------------------------------------ #
    def peer_config(self, peer: PeerConfig) -> Config:
        """Configuration visant uniquement ce pair, adresse résolue si besoin."""
        clone = self.config.for_peer(peer)
        if self._resolver is not None:
            clone.peers[0].mac = self._resolver(peer)
        elif not clone.peers[0].mac:
            raise ConfigError(
                f"pair « {peer.label()} » sans adresse: impossible de le joindre "
                "(renseignez peers[].mac ou lancez via le CLI qui résout les noms)"
            )
        return clone

    def _opened_session(self, peer: PeerConfig) -> Any | None:
        lookup = getattr(self._server, "session_for", None)
        return lookup(peer) if callable(lookup) else None

    def client(self, peer: PeerConfig) -> Any:
        """Client pour ce pair : sa connexion ouverte s'il y en a une, sinon on le rappelle."""
        session = self._opened_session(peer)
        if session is not None:
            self.log.info("« %s » est déjà connecté : je passe par son lien", peer.label())
            # pas de mise en cache : la session peut se fermer entre deux commandes
            return SessionClient(self._session_config(peer), session,
                                 self.config.request_timeout_s)
        key = peer.label()
        if key not in self._clients:
            self._clients[key] = self._client_factory(self.peer_config(peer))
        return self._clients[key]

    def _session_config(self, peer: PeerConfig) -> Config:
        """Configuration pour parler à un pair par un lien déjà établi (adresse facultative)."""
        clone = self.config.for_peer(peer)
        if not clone.peers[0].mac and self._resolver is not None:
            try:
                clone.peers[0].mac = self._resolver(peer)
            except Exception as exc:  # pas bloquant : on a déjà la connexion
                self.log.debug("adresse de « %s » non résolue: %s", peer.label(), exc)
        return clone

    def selected(self, names: list[str] | None = None) -> list[PeerConfig]:
        if names:
            return [self.config.require_peer(name) for name in names]
        if not self.config.peers:
            raise ConfigError("aucun pair configuré: renseignez « peers » dans la configuration")
        return list(self.config.peers)

    # ------------------------------------------------------------------ #
    # Interrogation
    # ------------------------------------------------------------------ #
    def probe(self, peer: PeerConfig) -> PeerReport:
        report = PeerReport(peer=peer)
        if not peer.outbound and self._opened_session(peer) is None:
            # Pair uniquement *client* (le téléphone) : c'est lui qui ouvre le
            # lien. Sans lien ouvert, il n'y a rien à appeler — et le rappeler
            # aboutirait à frapper un autre service RFCOMM du téléphone.
            report.error = "aucun lien ouvert par ce pair (il se connecte à nous)"
            report.error_code = "NOT_CONNECTED"
            return report
        try:
            client = self.client(peer)
            report.mac = client.config.peers[0].mac
        except Exception as exc:  # résolution impossible (BlueZ, nom inconnu…)
            report.error = f"{type(exc).__name__}: {exc}"
            report.error_code = getattr(exc, "code", "") or "UNRESOLVED"
            return report
        try:
            report.status = client.status(timeout=self.config.request_timeout_s)
            report.reachable = True
        except Exception as exc:
            report.error = f"{type(exc).__name__}: {exc}"
            report.error_code = getattr(exc, "code", "") or ""
        return report

    def reports(self, names: list[str] | None = None) -> list[PeerReport]:
        return [self.probe(peer) for peer in self.selected(names)]

    def holders(self, names: list[str] | None = None) -> list[PeerReport]:
        return [report for report in self.reports(names) if report.holds_headphones]

    # ------------------------------------------------------------------ #
    # Bascule
    # ------------------------------------------------------------------ #
    def take(self, source: str | None = None, skip_release: bool = False) -> dict[str, Any]:
        """Je prends les écouteurs : le détenteur les libère, puis je me connecte.

        Sans pair configuré, il n'y a personne à interroger : on se contente de
        se connecter localement (utile en mode ``unix`` de diagnostic, ou quand
        le casque est simplement libre).
        """
        peers = self.selected([source] if source else None) if (source or self.config.peers) else []
        if not peers:
            self.log.info("aucun pair configuré: connexion directe aux écouteurs")
        reports = [self.probe(peer) for peer in peers]
        reachable = [r for r in reports if r.reachable]
        holders = [r for r in reports if r.holds_headphones]
        released: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        if not reachable:
            self.log.warning(
                "aucun pair joignable (%s): je me connecte directement aux écouteurs",
                ", ".join(f"{r.label}: {r.error}" for r in reports) or "aucun pair",
            )
        elif not holders:
            self.log.info("aucun pair ne détient les écouteurs: connexion directe")

        if not skip_release:
            for report in holders:
                try:
                    result = self.client(report.peer).release(timeout=self.config.request_timeout_s)
                    released.append({"peer": report.label, **result})
                    self.log.info("« %s » a libéré les écouteurs", report.label)
                except Exception as exc:
                    failures.append({
                        "peer": report.label,
                        "error": f"{type(exc).__name__}: {exc}",
                        "code": getattr(exc, "code", "") or "",
                    })
                    self.log.error("« %s » n'a pas libéré les écouteurs: %s", report.label, exc)

        if failures:
            # Inutile de tenter la connexion locale : un autre appareil tient
            # toujours les écouteurs, la bataille serait perdue d'avance.
            return {
                "ok": False,
                "probed": [r.as_dict() for r in reports],
                "holders": [r.label for r in holders],
                "released": released,
                "failures": failures,
                "grabbed": None,
            }

        try:
            grabbed = self.sink.grab()
        except AudioError as exc:
            return {
                "ok": False,
                "probed": [r.as_dict() for r in reports],
                "holders": [r.label for r in holders],
                "released": released,
                "failures": [{"peer": self.config.device_name, "error": exc.message or exc.code,
                              "code": exc.code}],
                "grabbed": None,
            }
        return {
            "ok": True,
            "probed": [r.as_dict() for r in reports],
            "holders": [r.label for r in holders],
            "released": released,
            "failures": [],
            "grabbed": grabbed,
        }

    def give(self, target: str | None = None, skip_grab: bool = False) -> dict[str, Any]:
        """Je rends les écouteurs : je les libère, puis le pair visé se connecte."""
        peer = self.config.require_peer(target)
        try:
            released = self.sink.release()
        except AudioError as exc:
            return {
                "ok": False,
                "peer": peer.label(),
                "released": None,
                "failures": [{"peer": self.config.device_name, "error": exc.message or exc.code,
                              "code": exc.code}],
                "grabbed": None,
            }
        if skip_grab:
            return {"ok": True, "peer": peer.label(), "released": released,
                    "failures": [], "grabbed": None}
        try:
            grabbed = self.client(peer).grab(timeout=self.config.request_timeout_s)
        except Exception as exc:
            return {
                "ok": False,
                "peer": peer.label(),
                "released": released,
                "failures": [{"peer": peer.label(), "error": f"{type(exc).__name__}: {exc}",
                              "code": getattr(exc, "code", "") or ""}],
                "grabbed": None,
            }
        return {"ok": True, "peer": peer.label(), "released": released,
                "failures": [], "grabbed": grabbed}

    def snapshot(self, names: list[str] | None = None) -> dict[str, Any]:
        """Vue complète : ce que je détiens, et l'état de chaque pair."""
        local_audio = self.sink.status()
        reports = self.reports(names)
        return {
            "device": self.config.device_name,
            "audio": local_audio,
            "headphones_held": bool(local_audio.get("connected")),
            "adapter": self.sink.adapter_info(),
            "peers": [report.as_dict() for report in reports],
        }
