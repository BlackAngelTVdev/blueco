"""Pilotage de BlueZ.

Deux implémentations interchangeables :

* :class:`BluezClient`        - D-Bus ``org.bluez`` (recommandé, sans sous-processus)
* :class:`BluezCommandClient` - ``bluetoothctl`` en ligne de commande (repli)

Les deux exposent la même interface minimale : ``devices()``, ``find_device()``,
``connect()``, ``disconnect()``, ``adapter_info()``.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Iterable

SERVICE = "org.bluez"
ROOT = "/"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
DEVICE_IFACE = "org.bluez.Device1"
ADAPTER_IFACE = "org.bluez.Adapter1"
PROFILE_MANAGER_IFACE = "org.bluez.ProfileManager1"

ADDRESS_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

log = logging.getLogger(__name__)


class BluezError(RuntimeError):
    """BlueZ injoignable, appareil introuvable ou opération refusée."""


def is_mac(value: str) -> bool:
    return bool(ADDRESS_RE.match(value.strip()))


def normalize_mac(value: str) -> str:
    return value.strip().upper()


@dataclass(frozen=True)
class DeviceInfo:
    path: str
    address: str
    name: str
    connected: bool
    paired: bool
    trusted: bool
    adapter: str = ""
    icon: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "mac": self.address,
            "name": self.name,
            "connected": self.connected,
            "paired": self.paired,
            "trusted": self.trusted,
            "icon": self.icon,
        }


# --------------------------------------------------------------------------- #
# Implémentation D-Bus
# --------------------------------------------------------------------------- #
class BluezClient:
    """Client D-Bus de BlueZ. Import paresseux de ``dbus`` (pas requis pour les tests)."""

    def __init__(self, bus: Any | None = None) -> None:
        import dbus  # import local: évite d'exiger dbus pour les tests unitaires

        self._dbus = dbus
        self._lock = threading.RLock()
        try:
            self._bus = bus if bus is not None else dbus.SystemBus()
        except dbus.exceptions.DBusException as exc:  # pragma: no cover - dépend de l'hôte
            raise BluezError(f"bus système D-Bus indisponible: {exc}") from exc
        try:
            self._om = dbus.Interface(self._bus.get_object(SERVICE, ROOT), OM_IFACE)
        except dbus.exceptions.DBusException as exc:  # pragma: no cover
            raise BluezError(f"service BlueZ absent (bluetoothd ?): {exc}") from exc

    # -- introspection ------------------------------------------------------ #
    def _objects(self) -> dict:
        with self._lock:
            try:
                return self._om.GetManagedObjects()
            except self._dbus.exceptions.DBusException as exc:  # pragma: no cover
                raise BluezError(f"GetManagedObjects a échoué: {exc}") from exc

    def adapters(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for path, interfaces in self._objects().items():
            props = interfaces.get(ADAPTER_IFACE)
            if not props:
                continue
            found.append(
                {
                    "path": str(path),
                    "address": normalize_mac(str(props.get("Address", ""))),
                    "name": str(props.get("Alias") or props.get("Name") or ""),
                    "powered": bool(props.get("Powered", False)),
                    "discoverable": bool(props.get("Discoverable", False)),
                }
            )
        return found

    def adapter_info(self) -> dict[str, Any]:
        adapters = self.adapters()
        if not adapters:
            return {}
        first = adapters[0]
        return {
            "address": first["address"],
            "name": first["name"],
            "powered": first["powered"],
            "adapters": len(adapters),
        }

    def devices(self) -> list[DeviceInfo]:
        found: list[DeviceInfo] = []
        for path, interfaces in self._objects().items():
            props = interfaces.get(DEVICE_IFACE)
            if not props:
                continue
            found.append(
                DeviceInfo(
                    path=str(path),
                    address=normalize_mac(str(props.get("Address", ""))),
                    name=str(props.get("Alias") or props.get("Name") or ""),
                    connected=bool(props.get("Connected", False)),
                    paired=bool(props.get("Paired", False)),
                    trusted=bool(props.get("Trusted", False)),
                    adapter=str(getattr(props.get("Adapter"), "file", "") or ""),
                    icon=str(props.get("Icon", "")),
                )
            )
        found.sort(key=lambda d: (not d.connected, d.name.lower(), d.address))
        return found

    def find_device(self, address: str = "", name: str = "") -> DeviceInfo:
        """Retrouve un appareil appairé par adresse MAC ou par nom (insensible à la casse)."""
        devices = self.devices()
        if address:
            wanted = normalize_mac(address)
            for device in devices:
                if device.address == wanted:
                    return device
            raise BluezError(f"appareil {wanted} inconnu de BlueZ")
        if name:
            needle = name.strip().lower()
            exact = [d for d in devices if d.name.lower() == needle]
            if exact:
                return exact[0]
            partial = [d for d in devices if needle in d.name.lower()]
            if len(partial) == 1:
                return partial[0]
            if len(partial) > 1:
                listed = ", ".join(f"{d.name} ({d.address})" for d in partial)
                raise BluezError(f"plusieurs appareils correspondent à « {name} »: {listed}")
            raise BluezError(f"aucun appareil nommé « {name} »")
        connected = [d for d in devices if d.connected]
        if len(connected) == 1:
            return connected[0]
        raise BluezError("aucun appareil cible: précisez headphones.mac ou headphones.name")

    # -- actions ------------------------------------------------------------ #
    def _device_iface(self, device: DeviceInfo) -> Any:
        obj = self._bus.get_object(SERVICE, device.path)
        return self._dbus.Interface(obj, DEVICE_IFACE)

    def connect(self, address: str) -> DeviceInfo:
        device = self.find_device(address)
        try:
            self._device_iface(device).Connect()
        except self._dbus.exceptions.DBusException as exc:
            raise BluezError(f"connexion à {device.address} refusée: {_dbus_message(exc)}") from exc
        return self.find_device(address)

    def disconnect(self, address: str) -> DeviceInfo:
        device = self.find_device(address)
        try:
            self._device_iface(device).Disconnect()
        except self._dbus.exceptions.DBusException as exc:
            raise BluezError(f"déconnexion de {device.address} refusée: {_dbus_message(exc)}") from exc
        return self.find_device(address)


def _dbus_message(exc: Exception) -> str:
    """Extrait le message BlueZ lisible d'une ``DBusException``."""
    text = getattr(exc, "get_dbus_message", None)
    if callable(text):
        try:
            value = text()
            if value:
                return str(value)
        except Exception:  # pragma: no cover - défensif
            pass
    return str(exc)


# --------------------------------------------------------------------------- #
# Implémentation bluetoothctl (repli)
# --------------------------------------------------------------------------- #
class BluezCommandClient:
    """Repli sans D-Bus, en pilotant ``bluetoothctl``."""

    def __init__(self, binary: str = "bluetoothctl", timeout: float = 20.0) -> None:
        resolved = shutil.which(binary)
        if not resolved:
            raise BluezError(f"« {binary} » introuvable dans le PATH")
        self._binary = resolved
        self._timeout = timeout

    def _run(self, *args: str) -> str:
        try:
            completed = subprocess.run(
                [self._binary, *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BluezError(f"bluetoothctl {' '.join(args)}: délai dépassé") from exc
        except OSError as exc:
            raise BluezError(f"bluetoothctl injoignable: {exc}") from exc
        return completed.stdout or ""

    def devices(self) -> list[DeviceInfo]:
        infos: list[DeviceInfo] = []
        for line in self._run("devices").splitlines():
            parts = line.strip().split(maxsplit=2)
            if len(parts) >= 2 and is_mac(parts[1]):
                name = parts[2] if len(parts) > 2 else ""
                infos.append(
                    DeviceInfo(path="", address=normalize_mac(parts[1]), name=name,
                               connected=False, paired=True, trusted=False)
                )
        return infos

    def _info(self, address: str) -> dict[str, str]:
        props: dict[str, str] = {}
        for line in self._run("info", address).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            props.setdefault(key.strip().lower(), value.strip())
        return props

    def find_device(self, address: str = "", name: str = "") -> DeviceInfo:
        candidates = self.devices()
        target: DeviceInfo | None = None
        if address:
            wanted = normalize_mac(address)
            target = next((d for d in candidates if d.address == wanted), None)
        elif name:
            needle = name.strip().lower()
            matches = [d for d in candidates if needle in d.name.lower()]
            if len(matches) > 1:
                raise BluezError(f"plusieurs appareils correspondent à « {name} »")
            target = matches[0] if matches else None
        else:
            raise BluezError("aucun appareil cible: précisez headphones.mac ou headphones.name")
        if target is None:
            raise BluezError(f"appareil {address or name!r} inconnu de bluetoothctl")
        props = self._info(target.address)
        if not props:
            raise BluezError(f"bluetoothctl info {target.address} n'a rien renvoyé")
        return DeviceInfo(
            path="",
            address=target.address,
            name=props.get("alias") or props.get("name") or target.name,
            connected=props.get("connected", "no").lower() == "yes",
            paired=props.get("paired", "yes").lower() == "yes",
            trusted=props.get("trusted", "no").lower() == "yes",
        )

    def connect(self, address: str) -> DeviceInfo:
        output = self._run("connect", normalize_mac(address))
        if "Connection successful" not in output:
            raise BluezError(f"connexion à {address} refusée: {output.strip() or 'sans message'}")
        return self.find_device(address)

    def disconnect(self, address: str) -> DeviceInfo:
        output = self._run("disconnect", normalize_mac(address))
        if "not connected" in output.lower() and "Successful" not in output:
            return self.find_device(address)
        if "Successful" not in output and "not connected" not in output.lower():
            raise BluezError(f"déconnexion de {address} refusée: {output.strip() or 'sans message'}")
        return self.find_device(address)

    def adapter_info(self) -> dict[str, Any]:
        for line in self._run("show").splitlines():
            if line.strip().startswith("Controller "):
                parts = line.split()
                if len(parts) >= 3:
                    return {"address": normalize_mac(parts[2]), "name": " ".join(parts[3:]), "powered": True}
        return {}


def open_client(prefer: str = "auto") -> Any:
    """Ouvre le meilleur client disponible.

    ``prefer``: ``"dbus"``, ``"command"`` ou ``"auto"`` (D-Bus puis repli).
    """
    if prefer in ("dbus", "auto"):
        try:
            return BluezClient()
        except BluezError as exc:
            if prefer == "dbus":
                raise
            log.warning("BlueZ via D-Bus indisponible (%s), repli sur bluetoothctl", exc)
    return BluezCommandClient()


def connected_devices(devices: Iterable[DeviceInfo]) -> list[DeviceInfo]:
    return [d for d in devices if d.connected]


def resolve_peer_mac(peer: Any, client: Any) -> str:
    """Adresse MAC d'un pair configuré, par son adresse ou en interrogeant BlueZ.

    Les pair sont désignés par un nom lisible dans la configuration : on retrouve
    l'adresse parmi les appareils que BlueZ connaît (donc appairés ou déjà vus).
    """
    mac = getattr(peer, "mac", "") or ""
    if mac:
        if not is_mac(mac):
            raise BluezError(f"adresse MAC invalide dans la configuration: {mac!r}")
        return normalize_mac(mac)
    label = getattr(peer, "label", lambda: "?")()
    candidates = [c for c in (getattr(peer, "bluetooth_name", ""), getattr(peer, "name", "")) if c]
    if not candidates:
        raise BluezError(f"pair sans nom ni adresse: impossible de le résoudre ({label})")
    last: BluezError | None = None
    for candidate in candidates:
        try:
            return client.find_device(name=candidate).address
        except BluezError as exc:
            last = exc
    raise BluezError(
        f"impossible de résoudre « {label} » via BlueZ ({last}). "
        "Appairez l'appareil, précisez peers[].bluetooth_name, ou figez peers[].mac."
    )
