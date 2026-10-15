"""Backends audio : libérer / reprendre le casque Bluetooth.

``AudioSink`` est l'abstraction utilisée par le serveur. Deux implémentations :

* :class:`BluezAudioSink` - vraies opérations via BlueZ
* :class:`FakeAudioSink`   - état en mémoire, sans effet de bord (tests, démo)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from . import protocol as proto
from .bluez import BluezError
from .config import AUDIO_BLUEZ, AUDIO_FAKE, Config

log = logging.getLogger(__name__)


class AudioError(Exception):
    """Opération audio impossible ; ``code`` est un code d'erreur du protocole."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class AudioSink(ABC):
    """Ce que le démon sait faire du casque."""

    label = "?"

    @abstractmethod
    def status(self) -> dict[str, Any]:
        """État courant : ``{"found": bool, "connected": bool, "mac": ..., "name": ...}``."""

    @abstractmethod
    def release(self) -> dict[str, Any]:
        """Déconnecte le casque de *cet* appareil (il devient libre)."""

    @abstractmethod
    def grab(self) -> dict[str, Any]:
        """Reconnecte le casque à cet appareil."""

    def adapter_info(self) -> dict[str, Any]:
        return {}


class BluezAudioSink(AudioSink):
    label = "bluez"

    def __init__(self, client: Any, mac: str = "", name: str = "") -> None:
        self._client = client
        self._mac = mac
        self._name = name

    def _lookup(self) -> Any | None:
        try:
            return self._client.find_device(self._mac, self._name)
        except BluezError as exc:
            log.debug("casque introuvable: %s", exc)
            return None

    def status(self) -> dict[str, Any]:
        device = self._lookup()
        if device is None:
            return {
                "found": False,
                "connected": False,
                "mac": self._mac,
                "name": self._name,
                "backend": self.label,
            }
        payload = device.as_dict()
        payload.update({"found": True, "backend": self.label})
        return payload

    def release(self) -> dict[str, Any]:
        device = self._lookup()
        if device is None:
            raise AudioError(
                proto.E_NO_DEVICE,
                f"casque introuvable ({self._mac or self._name or 'aucune cible configurée'})",
            )
        if not device.connected:
            return {
                "released": device.address,
                "name": device.name,
                "was_connected": False,
                "connected": False,
                "already_free": True,
            }
        try:
            self._client.disconnect(device.address)
        except BluezError as exc:
            raise AudioError(proto.E_AUDIO_UNAVAILABLE, str(exc)) from exc
        return {
            "released": device.address,
            "name": device.name,
            "was_connected": True,
            "connected": False,
            "already_free": False,
        }

    def grab(self) -> dict[str, Any]:
        device = self._lookup()
        if device is None:
            raise AudioError(
                proto.E_NO_DEVICE,
                f"casque introuvable ({self._mac or self._name or 'aucune cible configurée'})",
            )
        if device.connected:
            return {"connected": True, "mac": device.address, "already_connected": True}
        try:
            self._client.connect(device.address)
        except BluezError as exc:
            raise AudioError(proto.E_AUDIO_UNAVAILABLE, str(exc)) from exc
        return {"connected": True, "mac": device.address, "already_connected": False}

    def adapter_info(self) -> dict[str, Any]:
        try:
            return self._client.adapter_info()
        except BluezError:
            return {}


class FakeAudioSink(AudioSink):
    """Backend de test : joue le rôle du casque sans toucher au Bluetooth."""

    label = "fake"

    def __init__(
        self,
        mac: str = "AA:BB:CC:DD:EE:FF",
        name: str = "Fake Buds",
        connected: bool = True,
        fail_on: set[str] | None = None,
    ) -> None:
        self.mac = mac
        self.name = name
        self.connected = connected
        self.fail_on = set(fail_on or ())
        #: journal des appels, pratique pour les assertions de test
        self.calls: list[str] = []

    def status(self) -> dict[str, Any]:
        return {
            "found": True,
            "connected": self.connected,
            "mac": self.mac,
            "name": self.name,
            "backend": self.label,
        }

    def _fail(self, action: str) -> None:
        if action in self.fail_on:
            raise AudioError(proto.E_AUDIO_UNAVAILABLE, f"échec simulé sur {action}")

    def release(self) -> dict[str, Any]:
        self.calls.append("release")
        self._fail("release")
        was = self.connected
        self.connected = False
        return {
            "released": self.mac,
            "name": self.name,
            "was_connected": was,
            "connected": False,
            "already_free": not was,
        }

    def grab(self) -> dict[str, Any]:
        self.calls.append("grab")
        self._fail("grab")
        was = self.connected
        self.connected = True
        return {"connected": True, "mac": self.mac, "already_connected": was}

    def adapter_info(self) -> dict[str, Any]:
        return {"address": "11:22:33:44:55:66", "name": "fake-adapter", "powered": True,
                "backend": self.label}


def create_sink(config: Config, client: Any | None = None) -> AudioSink:
    """Construit le backend décrit par la configuration."""
    if config.audio_backend == AUDIO_FAKE or client is None:
        if config.audio_backend != AUDIO_FAKE and client is None:
            log.warning("aucun client BlueZ fourni: backend « fake » utilisé")
        return FakeAudioSink(
            mac=config.headphones.mac or "AA:BB:CC:DD:EE:FF",
            name=config.headphones.name or "Fake Buds",
        )
    if config.audio_backend != AUDIO_BLUEZ:  # pragma: no cover - validé par Config
        raise AudioError(proto.E_INTERNAL, f"backend inconnu: {config.audio_backend}")
    return BluezAudioSink(client, mac=config.headphones.mac, name=config.headphones.name)
