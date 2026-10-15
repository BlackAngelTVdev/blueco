"""Configuration du démon blueco (``~/.config/blueco/config.json``)."""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "blueco"

TRANSPORT_PROFILE1 = "profile1"
TRANSPORT_RFCOMM = "rfcomm"
TRANSPORT_UNIX = "unix"
TRANSPORTS = (TRANSPORT_PROFILE1, TRANSPORT_RFCOMM, TRANSPORT_UNIX)

AUDIO_BLUEZ = "bluez"
AUDIO_FAKE = "fake"


class ConfigError(Exception):
    """Config illisible ou invalide."""


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / APP_NAME


def default_config_path() -> Path:
    return config_dir() / "config.json"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / APP_NAME


def _peer_from_dict(prefix: str, entry: Any) -> "PeerConfig":
    if not isinstance(entry, dict):
        raise ConfigError(f"{prefix} doit être un objet")
    unknown = set(entry) - PEER_KEYS
    if unknown:
        raise ConfigError(f"{prefix}: clés inconnues: {', '.join(sorted(unknown))}")
    peer = PeerConfig()
    for key, raw in entry.items():
        current = getattr(peer, key)
        if isinstance(current, bool):
            peer.__setattr__(key, _as_bool(raw, f"{prefix}.{key}"))
        elif isinstance(current, int):
            peer.__setattr__(key, int(raw))
        else:
            peer.__setattr__(key, str(raw))
    return peer


def _as_bool(raw: Any, where: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in {"true", "oui", "yes", "1"}:
        return True
    if isinstance(raw, str) and raw.strip().lower() in {"false", "non", "no", "0"}:
        return False
    raise ConfigError(f"{where} doit être un booléen")


@dataclass
class HeadphoneConfig:
    """Le casque Bluetooth qu'on veut faire basculer entre les appareils."""

    mac: str = ""
    name: str = ""

    def is_set(self) -> bool:
        return bool(self.mac or self.name)


PEER_KEYS = frozenset({"name", "mac", "bluetooth_name", "mode", "channel", "token", "outbound"})

_MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


@dataclass
class PeerConfig:
    """Un appareil avec qui on peut basculer les écouteurs (PC ou téléphone).

    Le plus simple est de ne renseigner que ``name`` : l'adresse est alors
    résolue à l'exécution via BlueZ (appareils connus/appairés). ``mac`` sert à
    figer l'adresse, ``bluetooth_name`` à préciser le nom Bluetooth quand il
    diffère de l'étiquette utilisée dans les commandes.
    """

    #: étiquette utilisée dans les commandes (`blueco take --from bureau`)
    name: str = ""
    mac: str = ""
    #: nom de l'appareil tel que BlueZ le connaît, si différent de ``name``
    bluetooth_name: str = ""
    #: transport pour joindre ce pair ("" = celui de la configuration)
    mode: str = ""
    #: canal RFCOMM utilisé pour joindre ce pair
    channel: int = 3
    #: jeton attendu par ce pair ("" = celui de la configuration)
    token: str = ""
    #: ce pair peut-il être appelé par nous ?
    #:
    #: Le téléphone, non : l'appli Android ouvre toujours le lien elle-même et
    #: n'écoute aucun canal. Le mettre à ``false`` évite de le rappeler dans le
    #: vide (le PC tombait sinon sur un autre service RFCOMM du téléphone, ce
    #: qui produisait des « échecs de connexion » sans intérêt).
    outbound: bool = True

    def is_set(self) -> bool:
        return bool(self.name or self.mac or self.bluetooth_name)

    def label(self) -> str:
        """Nom à afficher : l'étiquette, à défaut le nom BlueZ puis l'adresse."""
        return self.name or self.bluetooth_name or self.mac

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TransportConfig:
    #: profile1 = serveur SPP enregistré auprès de BlueZ (SDP publié, recommandé)
    #: rfcomm   = socket RFCOMM brut sur un canal fixe (aucun enregistrement SDP)
    #: unix     = socket locale, pour les tests et la mise au point sans Bluetooth
    mode: str = TRANSPORT_PROFILE1
    #: canal RFCOMM utilisé en mode ``rfcomm``
    channel: int = 3
    #: chemin de la socket en mode ``unix``
    socket_path: str = ""
    #: nom annoncé dans l'enregistrement SDP
    service_name: str = "blueco"
    #: UUID du profil SPP ; à garder aligné avec l'appli Android
    uuid: str = "00001101-0000-1000-8000-00805f9b34fb"
    #: enregistrement SDP brut (XML) si BlueZ refuse d'en générer un
    service_record: str = ""
    #: socket locale par laquelle `blueco take` pilote le démon en cours
    #: d'exécution (le démon est le seul à voir la connexion ouverte par le
    #: téléphone). Vide = $XDG_RUNTIME_DIR/blueco/control.sock
    control_socket: str = ""
    #: exige l'authentification Bluetooth (appairage) pour le profil SPP
    require_authentication: bool = True

    def resolved_socket_path(self) -> str:
        if self.socket_path:
            return self.socket_path
        return str(runtime_dir() / "blueco.sock")

    def resolved_control_socket(self) -> str:
        if self.control_socket:
            return self.control_socket
        return str(runtime_dir() / "control.sock")


@dataclass
class Config:
    #: nom logique de cet appareil ("pc", "bureau", ...)
    device_name: str = "pc"
    #: secret partagé optionnel ; s'il est défini, toute requête entrante
    #: doit porter le bon champ ``token``.
    token: str = ""
    headphones: HeadphoneConfig = field(default_factory=HeadphoneConfig)
    #: tous les appareils avec qui on peut basculer (PC voisins, téléphone...)
    peers: list[PeerConfig] = field(default_factory=list)
    transport: TransportConfig = field(default_factory=TransportConfig)
    #: backend audio: "bluez" (D-Bus/bluetoothctl) ou "fake" (tests, aucune action)
    audio_backend: str = AUDIO_BLUEZ
    #: délai accordé au pair pour se déconnecter du casque après un RELEASE
    request_timeout_s: float = 15.0
    #: verrouillage de session quand le téléphone s'éloigne (voir `blueco watch`)
    lock_command: str = "loginctl lock-session"

    # -- (dé)sérialisation -------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        if not isinstance(data, dict):
            raise ConfigError("la configuration doit être un objet JSON")
        known = {"device_name", "token", "headphones", "peers", "peer", "transport",
                 "audio_backend", "request_timeout_s", "lock_command"}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"clés inconnues: {', '.join(sorted(unknown))}")

        cfg = cls()
        for key in ("device_name", "token", "audio_backend", "lock_command"):
            if key in data:
                setattr(cfg, key, str(data[key]))
        if "request_timeout_s" in data:
            cfg.request_timeout_s = float(data["request_timeout_s"])
        value = data.get("headphones")
        if value is not None:
            if not isinstance(value, dict):
                raise ConfigError("headphones doit être un objet")
            cfg.headphones = HeadphoneConfig(**{k: str(v) for k, v in value.items()})
        value = data.get("peers")
        if value is not None:
            if not isinstance(value, list):
                raise ConfigError("peers doit être une liste d'objets")
            cfg.peers = [_peer_from_dict(f"peers[{index}]", entry)
                         for index, entry in enumerate(value)]
        legacy = data.get("peer")
        if legacy is not None:
            if cfg.peers:
                raise ConfigError(
                    "« peer » (singulier) et « peers » (liste) ne peuvent pas coexister : "
                    "utilisez « peers »"
                )
            cfg.peers = [_peer_from_dict("peer", legacy)]
        value = data.get("transport")
        if value is not None:
            if not isinstance(value, dict):
                raise ConfigError("transport doit être un objet")
            transport = TransportConfig()
            for key, raw in value.items():
                if not hasattr(transport, key):
                    raise ConfigError(f"transport.{key} inconnu")
                current = getattr(transport, key)
                if isinstance(current, bool):
                    setattr(transport, key, bool(raw))
                elif isinstance(current, int):
                    setattr(transport, key, int(raw))
                else:
                    setattr(transport, key, str(raw))
            cfg.transport = transport
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.transport.mode not in TRANSPORTS:
            raise ConfigError(
                f"transport.mode doit être l'un de {', '.join(TRANSPORTS)}"
            )
        if not 1 <= int(self.transport.channel) <= 30:
            raise ConfigError("transport.channel doit être entre 1 et 30")
        if self.audio_backend not in (AUDIO_BLUEZ, AUDIO_FAKE):
            raise ConfigError(f"audio_backend doit être {AUDIO_BLUEZ} ou {AUDIO_FAKE}")
        seen: set[str] = set()
        for index, peer in enumerate(self.peers):
            if not peer.is_set():
                raise ConfigError(
                    f"peers[{index}]: renseignez au moins name, bluetooth_name ou mac"
                )
            key = peer.label().lower()
            if key in seen:
                raise ConfigError(f"peers[{index}]: pair « {peer.label()} » en double")
            seen.add(key)
            if peer.mode and peer.mode not in TRANSPORTS:
                raise ConfigError(
                    f"peers[{index}].mode doit être l'un de {', '.join(TRANSPORTS)}"
                )
            if not 1 <= int(peer.channel) <= 30:
                raise ConfigError(f"peers[{index}].channel doit être entre 1 et 30")
            if peer.mac and not _MAC_RE.match(peer.mac):
                raise ConfigError(
                    f"peers[{index}].mac « {peer.mac} » n'est pas une adresse MAC "
                    "(format attendu AA:BB:CC:DD:EE:FF ou renseignez « name » à la place)"
                )

    # -- pairs -------------------------------------------------------------- #
    def peer_labels(self) -> str:
        return ", ".join(peer.label() for peer in self.peers)

    def primary_peer(self) -> PeerConfig | None:
        """Le pair unique, ou ``None`` s'il n'y en a aucun ou plusieurs."""
        return self.peers[0] if len(self.peers) == 1 else None

    def peer_by_name(self, name: str) -> PeerConfig | None:
        needle = name.strip().lower()
        if not needle:
            return None
        for peer in self.peers:
            if needle in (peer.label().lower(), peer.name.lower(), peer.mac.lower()):
                return peer
        partial = [peer for peer in self.peers if needle in peer.label().lower()]
        return partial[0] if len(partial) == 1 else None

    def require_peer(self, name: str | None = None) -> PeerConfig:
        """Résout le pair visé par une commande, avec un message clair si ambigu."""
        if name:
            peer = self.peer_by_name(name)
            if peer is None:
                raise ConfigError(
                    f"pair inconnu: « {name} » (pairs configurés: {self.peer_labels() or 'aucun'})"
                )
            return peer
        if len(self.peers) == 1:
            return self.peers[0]
        if not self.peers:
            raise ConfigError("aucun pair configuré: renseignez « peers » dans la configuration")
        raise ConfigError(
            f"plusieurs pairs configurés ({self.peer_labels()}): précisez lequel"
        )

    def for_peer(self, peer: PeerConfig) -> "Config":
        """Copie de la configuration visant un seul pair (mode, canal, jeton)."""
        clone = copy.deepcopy(self)
        clone.peers = [copy.deepcopy(peer)]
        if peer.mode:
            clone.transport.mode = peer.mode
        if peer.channel:
            clone.transport.channel = peer.channel
        if peer.token:
            clone.token = peer.token
        return clone

    def add_peer(self, peer: PeerConfig) -> None:
        """Ajoute un pair, ou remplace celui qui porte la même étiquette."""
        existing = self.peer_by_name(peer.label()) if peer.is_set() else None
        if existing is not None:
            self.peers[self.peers.index(existing)] = peer
        else:
            self.peers.append(peer)

    def remove_peer(self, name: str) -> bool:
        peer = self.peer_by_name(name)
        if peer is None:
            return False
        self.peers.remove(peer)
        return True

    # -- disque ------------------------------------------------------------- #
    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        target = Path(path) if path else default_config_path()
        if not target.exists():
            cfg = cls()
            return cfg
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"{target}: {exc}") from exc
        return cls.from_dict(data)

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        target = Path(path) if path else default_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.chmod(target, 0o600)
        return target
