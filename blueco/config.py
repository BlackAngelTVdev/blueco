"""Configuration du démon blueco (``~/.config/blueco/config.json``)."""

from __future__ import annotations

import json
import os
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


@dataclass
class HeadphoneConfig:
    """Le casque Bluetooth qu'on veut faire basculer entre les appareils."""

    mac: str = ""
    name: str = ""

    def is_set(self) -> bool:
        return bool(self.mac or self.name)


@dataclass
class PeerConfig:
    """L'autre appareil (le téléphone Android par défaut)."""

    mac: str = ""
    name: str = ""

    def is_set(self) -> bool:
        return bool(self.mac or self.name)


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
    #: exige l'authentification Bluetooth (appairage) pour le profil SPP
    require_authentication: bool = True

    def resolved_socket_path(self) -> str:
        if self.socket_path:
            return self.socket_path
        return str(runtime_dir() / "blueco.sock")


@dataclass
class Config:
    #: nom logique de cet appareil ("pc", "bureau", ...)
    device_name: str = "pc"
    #: secret partagé optionnel ; s'il est défini, toute requête entrante
    #: doit porter le bon champ ``token``.
    token: str = ""
    headphones: HeadphoneConfig = field(default_factory=HeadphoneConfig)
    peer: PeerConfig = field(default_factory=PeerConfig)
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
        known = {"device_name", "token", "headphones", "peer", "transport",
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
        for key, klass in (("headphones", HeadphoneConfig), ("peer", PeerConfig)):
            value = data.get(key)
            if value is None:
                continue
            if not isinstance(value, dict):
                raise ConfigError(f"{key} doit être un objet")
            setattr(cfg, key, klass(**{k: str(v) for k, v in value.items()}))
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
