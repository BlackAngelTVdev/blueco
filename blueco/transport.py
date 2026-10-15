"""Transports de trames.

Trois modes, même interface ``Listener`` / ``Connection`` :

===========================  =======================================================
``unix``                     socket locale. Sert aux tests, à la CI et au dépannage
                             sans Bluetooth (``blueco serve --transport unix``).
``rfcomm``                   socket RFCOMM brute (:data:`socket.BTPROTO_RFCOMM`)
                             sur un canal fixe. Aucun enregistrement SDP n'est
                             publié : le pair doit connaître le canal.
``profile1``  (recommandé)   enregistrement d'un profil SPP auprès de BlueZ
                             (``org.bluez.ProfileManager1``). BlueZ publie
                             l'enregistrement SDP et alloue le canal RFCOMM ; les
                             connexions entrantes arrivent par le signal
                             ``NewConnection`` sous forme de descripteur de
                             fichier. L'appli Android peut alors résoudre le
                             service par UUID, API publique, sans canal en dur.
===========================  =======================================================
"""

from __future__ import annotations

import logging
import os
import queue
import socket
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from . import protocol as proto
from .config import (
    TRANSPORT_PROFILE1,
    TRANSPORT_RFCOMM,
    TRANSPORT_UNIX,
    Config,
    runtime_dir,
)

log = logging.getLogger(__name__)

BLUEZ_SERVICE = "org.bluez"
PROFILE_MANAGER_IFACE = "org.bluez.ProfileManager1"
PROFILE_IFACE = "org.bluez.Profile1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROFILE_PATH = "/org/blueco/profile1"
BLUEZ_ROOT = "/org/bluez"

#: UUID du profil SPP (Serial Port), identique côté Android.
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"

#: Enregistrement SDP du profil SPP, à publier auprès de BlueZ.
#:
#: BlueZ 5.87 **ne publie rien tout seul** : ``RegisterProfile`` réussit, mais
#: aucun enregistrement n'arrive dans la base SDP, et le pair distant repart
#: avec « SDP - No Attributes Returned for target UUID » (constaté dans les
#: journaux Bluetooth d'Android) avant d'échouer sur le canal 1. Fournir cet
#: enregistrement est donc indispensable : c'est lui qui annonce l'UUID SPP et
#: le canal RFCOMM à utiliser.
SPP_SERVICE_RECORD = """<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="0x1101" /></sequence></attribute>
  <attribute id="0x0004">
    <sequence>
      <sequence><uuid value="0x0100" /></sequence>
      <sequence><uuid value="0x0003" /><uint8 value="0x{channel:02x}" /></sequence>
    </sequence>
  </attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002" /></sequence></attribute>
  <attribute id="0x0009"><sequence><sequence><uuid value="0x1101" /><uint16 value="0x0100" /></sequence></sequence></attribute>
  <attribute id="0x0100"><text value="{name}" /></attribute>
</record>
"""

BDADDR_ANY = getattr(socket, "BDADDR_ANY", "00:00:00:00:00:00")
RFCOMM_AVAILABLE = hasattr(socket, "BTPROTO_RFCOMM") and hasattr(socket, "AF_BLUETOOTH")


class TransportError(RuntimeError):
    """Le transport ne peut pas être ouvert ou a échoué."""


# --------------------------------------------------------------------------- #
# Connexions
# --------------------------------------------------------------------------- #
class Connection(ABC):
    """Connexion bidirectionnelle de trames."""

    peer: str = "?"

    def __init__(self, sock: socket.socket, peer: str) -> None:
        self._sock = sock
        self.peer = peer
        self._reader = proto.LineReader(sock)
        self._closed = False

    # -- E/S ---------------------------------------------------------------- #
    def readline(self, timeout: float | None = proto.DEFAULT_TIMEOUT) -> bytes | None:
        return self._reader.readline(timeout)

    def writeline(self, raw: bytes) -> None:
        if self._closed:
            raise TransportError("connexion fermée")
        try:
            self._sock.sendall(raw)
        except OSError as exc:
            raise TransportError(f"envoi impossible: {exc}") from exc

    def send(self, message: dict) -> None:
        self.writeline(proto.encode(message))

    def recv(self, timeout: float | None = proto.DEFAULT_TIMEOUT) -> dict | None:
        raw = self.readline(timeout)
        if raw is None:
            return None
        return proto.decode(raw)

    # -- fermeture ---------------------------------------------------------- #
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed

    def describe(self) -> str:
        return f"{type(self).__name__}({self.peer})"

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class SocketConnection(Connection):
    pass


# --------------------------------------------------------------------------- #
# Écouteurs
# --------------------------------------------------------------------------- #
class Listener(ABC):
    """Source de connexions entrantes."""

    @abstractmethod
    def accept(self, timeout: float | None = None) -> Connection | None:
        """Retourne une connexion, ou ``None`` si le délai expire."""

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def describe(self) -> str: ...

    def __enter__(self) -> "Listener":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class UnixListener(Listener):
    """Socket locale : idéale pour tester toute la logique sans Bluetooth."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        target = Path(path) if path else runtime_dir() / "blueco.sock"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # socket orpheline d'une session précédente
            if _is_socket(target):
                target.unlink()
            else:
                raise TransportError(f"{target} existe et n'est pas une socket")
        self._path = target
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(str(target))
            self._sock.listen(4)
        except OSError as exc:
            raise TransportError(f"bind unix {target}: {exc}") from exc
        os.chmod(target, 0o600)

    def accept(self, timeout: float | None = None) -> Connection | None:
        self._sock.settimeout(timeout)
        try:
            client, _ = self._sock.accept()
        except socket.timeout:
            return None
        except OSError as exc:
            raise TransportError(f"accept unix: {exc}") from exc
        return SocketConnection(client, peer="unix-client")

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        try:
            if self._path.exists() and _is_socket(self._path):
                self._path.unlink()
        except OSError:
            pass

    def describe(self) -> str:
        return f"unix:{self._path}"


def _is_socket(path: Path) -> bool:
    import stat

    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


class RfcommListener(Listener):
    """Écoute RFCOMM brute sur un canal fixe (pas d'enregistrement SDP)."""

    def __init__(self, channel: int = 3, address: str = BDADDR_ANY) -> None:
        if not RFCOMM_AVAILABLE:
            raise TransportError("ce Python n'a pas été compilé avec le support Bluetooth")
        try:
            self._sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        except OSError as exc:
            raise TransportError(f"socket RFCOMM indisponible: {exc}") from exc
        try:
            self._sock.bind((address, channel))
            self._sock.listen(2)
        except OSError as exc:
            raise TransportError(
                f"bind RFCOMM canal {channel} sur {address}: {exc} "
                "(canal déjà pris ? adaptateur absent ?)"
            ) from exc
        self.channel = channel

    def accept(self, timeout: float | None = None) -> Connection | None:
        self._sock.settimeout(timeout)
        try:
            client, info = self._sock.accept()
        except socket.timeout:
            return None
        except OSError as exc:
            raise TransportError(f"accept RFCOMM: {exc}") from exc
        peer = info[0] if isinstance(info, (tuple, list)) and info else "rfcomm-client"
        return SocketConnection(client, peer=str(peer))

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def describe(self) -> str:
        return f"rfcomm:channel={self.channel}"


class Profile1Listener(Listener):
    """Serveur SPP enregistré auprès de BlueZ (mode recommandé).

    BlueZ publie l'enregistrement SDP (UUID SPP + canal RFCOMM alloué) puis nous
    transmet chaque connexion entrante via ``NewConnection`` sur D-Bus, avec un
    descripteur de fichier déjà connecté.
    """

    def __init__(self, config: Config) -> None:
        if not RFCOMM_AVAILABLE:
            raise TransportError("ce Python n'a pas été compilé avec le support Bluetooth")
        self._config = config
        self._queue: queue.Queue[socket.socket | None] = queue.Queue()
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._fallback: RfcommListener | None = None
        self._registered = False
        self._name: Any = None
        self._bus: Any = None
        self._profile: Any = None
        self._manager: Any = None

        self._setup_dbus()

    # -- D-Bus -------------------------------------------------------------- #
    def _setup_dbus(self) -> None:
        try:
            import dbus
            import dbus.mainloop.glib
            import dbus.service
            from gi.repository import GLib
        except ImportError as exc:  # pragma: no cover - dépendances système
            raise TransportError(
                f"mode profile1: modules D-Bus/GLib absents ({exc}); "
                "installez python-dbus et python-gobject, ou utilisez --transport rfcomm"
            ) from exc

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._dbus = dbus
        try:
            self._bus = dbus.SystemBus(private=True)
        except dbus.exceptions.DBusException as exc:  # pragma: no cover
            raise TransportError(f"bus système D-Bus indisponible: {exc}") from exc

        adapter_path = self._find_adapter_path()

        # On n'essaie PAS de posséder un nom bien connu : depuis la version 2.26
        # de D-Bus, ``/usr/share/dbus-1/system.conf`` contient ``<deny own="*"/>``
        # et seul root peut prendre un nom sur le bus système. BlueZ n'en a pas
        # besoin : il identifie le profil par le nom *unique* de la connexion
        # appelante (``:1.42``) et l'objet ``org.bluez.Profile1`` exporté dessus.
        # On utilise donc une connexion privée, sans nom.
        owner = _ProfileOwner(self._bus, PROFILE_PATH, self._queue, log)
        self._profile = owner
        self._name = None

        transport = self._config.transport
        options = {
            "Name": transport.service_name,
            "Role": "server",
            "RequireAuthentication": bool(transport.require_authentication),
            "RequireAuthorization": False,
        }
        if transport.service_record:
            record_path = Path(transport.service_record).expanduser()
            if not record_path.is_file():
                raise TransportError(f"transport.service_record introuvable: {record_path}")
            options["ServiceRecord"] = record_path.read_text(encoding="utf-8")
            log.info("enregistrement SDP fourni par %s", record_path)
        elif transport.uuid.lower() == SPP_UUID:
            # Sans cet enregistrement, BlueZ accepte le profil mais ne publie
            # aucun service : le téléphone ne trouve pas l'UUID et échoue.
            options["ServiceRecord"] = SPP_SERVICE_RECORD.format(
                channel=transport.channel or 3,
                name=transport.service_name or "blueco",
            )
            log.info("enregistrement SDP SPP généré (canal RFCOMM %s)", transport.channel or 3)
        else:
            log.warning(
                "UUID %s sans enregistrement SDP : BlueZ ne publiera aucun service "
                "(renseignez transport.service_record)",
                transport.uuid,
            )
        # ``Channel`` n'est volontairement pas transmis quand un enregistrement
        # est fourni : BlueZ lit le canal RFCOMM dans l'enregistrement lui-même,
        # les deux valeurs ne peuvent donc pas diverger.

        # ``RegisterProfile`` vit sur ``/org/bluez`` (gestionnaire global), et non
        # sur le chemin de l'adaptateur, qui n'expose que Adapter1/Media1/...
        self._manager = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, BLUEZ_ROOT), PROFILE_MANAGER_IFACE)
        try:
            self._manager.RegisterProfile(
                dbus.ObjectPath(PROFILE_PATH),
                transport.uuid,
                dbus.Dictionary(options, signature="sv"),
            )
            self._registered = True
        except dbus.exceptions.DBusException as exc:
            message = str(getattr(exc, "get_dbus_message", lambda: "")() or exc)
            raise TransportError(
                f"enregistrement du profil SPP refusé par BlueZ: {message}. "
                "Essayez transport.service_record (XML SDP) ou le mode rfcomm."
            ) from exc

        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, name="blueco-dbus", daemon=True)
        self._thread.start()
        log.info("profil SPP enregistré (%s) sur %s", transport.uuid, adapter_path)

    def _find_adapter_path(self) -> str:
        try:
            om = self._dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, "/"), OBJECT_MANAGER_IFACE)
            objects = om.GetManagedObjects()
        except self._dbus.exceptions.DBusException as exc:  # pragma: no cover
            raise TransportError(f"BlueZ injoignable (bluetoothd démarré ?): {exc}") from exc
        for path, interfaces in objects.items():
            if "org.bluez.Adapter1" in interfaces:
                return str(path)
        raise TransportError("aucun adaptateur Bluetooth trouvé (adaptateur désactivé ?)")

    # -- API Listener ------------------------------------------------------- #
    def accept(self, timeout: float | None = None) -> Connection | None:
        try:
            sock = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if sock is None:
            raise TransportError("écoute SPP interrompue")
        peer = "spp-client"
        try:
            address = sock.getpeername()
            if isinstance(address, tuple) and address:
                peer = str(address[0])
        except OSError:
            pass
        log.info("connexion SPP entrante de %s", peer)
        return SocketConnection(sock, peer=peer)

    def close(self) -> None:
        if self._registered and self._manager is not None:
            try:
                self._manager.UnregisterProfile(self._dbus.ObjectPath(PROFILE_PATH))
            except Exception:  # pragma: no cover - meilleur effort
                log.debug("UnregisterProfile a échoué", exc_info=True)
            self._registered = False
        if self._loop is not None:
            try:
                self._loop.quit()
            except Exception:  # pragma: no cover
                pass
        if self._fallback is not None:
            self._fallback.close()

    def describe(self) -> str:
        return f"profile1:{self._config.transport.uuid}"


class _ProfileOwner:  # pragma: no cover - nécessite un bus D-Bus réel
    """Objet D-Bus exposant ``org.bluez.Profile1``.

    Construit dynamiquement pour ne pas dépendre de ``dbus.service`` à l'import.
    """

    def __new__(cls, bus: Any, path: str, incoming: "queue.Queue[socket.socket | None]", logger: Any) -> Any:
        import dbus.service

        class _ProfileObject(dbus.service.Object):
            def __init__(self) -> None:
                super().__init__(bus, path)
                self._incoming = incoming
                self._log = logger

            @dbus.service.method(PROFILE_IFACE, in_signature="oha{sv}", out_signature="")
            def NewConnection(self, device: str, fd: Any, properties: dict) -> None:
                try:
                    raw_fd = fd.take()
                except Exception:  # pragma: no cover - selon la version de dbus-python
                    raw_fd = int(fd)
                try:
                    sock = socket.socket(fileno=raw_fd)
                except OSError as exc:
                    self._log.error("descripteur de fichier invalide: %s", exc)
                    return
                self._log.debug("NewConnection %s (%s)", device, properties)
                if not self._incoming.empty():
                    self._log.warning("connexion ignorée: une session est déjà en file")
                self._incoming.put(sock)

            @dbus.service.method(PROFILE_IFACE, in_signature="o", out_signature="")
            def RequestDisconnection(self, device: str) -> None:
                self._log.info("déconnexion demandée par BlueZ pour %s", device)
                self._incoming.put(None)

            @dbus.service.method(PROFILE_IFACE, in_signature="", out_signature="")
            def Release(self) -> None:
                self._log.info("profil libéré par BlueZ")
                self._incoming.put(None)

        return _ProfileObject()


# --------------------------------------------------------------------------- #
# Fabriques
# --------------------------------------------------------------------------- #
def create_listener(config: Config, mode: str | None = None,
                    socket_path: str | None = None, channel: int | None = None) -> Listener:
    """Construit l'écouteur décrit par la configuration (les arguments écrasent la config)."""
    selected = mode or config.transport.mode
    if selected == TRANSPORT_UNIX:
        return UnixListener(socket_path or config.transport.resolved_socket_path())
    if selected == TRANSPORT_RFCOMM:
        return RfcommListener(channel if channel is not None else config.transport.channel)
    if selected == TRANSPORT_PROFILE1:
        try:
            return Profile1Listener(config)
        except TransportError as exc:
            log.warning("mode profile1 indisponible (%s) - repli sur RFCOMM brut canal %s",
                        exc, config.transport.channel)
            return RfcommListener(channel if channel is not None else config.transport.channel)
    raise TransportError(f"transport inconnu: {selected!r}")


def connect_to(config: Config, *, timeout: float = 15.0,
               mode: str | None = None, channel: int | None = None) -> Connection:
    """Ouvre une connexion vers le pair (Linux client).

    En mode ``unix`` on parle à la socket locale ; sinon on ouvre un socket RFCOMM
    vers ``peer.mac`` sur le canal demandé (le pair doit publier ce canal).
    """
    selected = mode or config.transport.mode
    if selected == TRANSPORT_UNIX:
        path = config.transport.resolved_socket_path()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(path)
        except OSError as exc:
            sock.close()
            raise TransportError(f"connexion à {path} impossible: {exc}") from exc
        return SocketConnection(sock, peer=f"unix:{path}")

    peer = config.primary_peer()
    if peer is None:
        raise TransportError(
            "connexion impossible: "
            + ("aucun pair configuré" if not config.peers
               else f"plusieurs pairs configurés ({config.peer_labels()}): visez-en un avec Config.for_peer()")
        )
    if not peer.mac:
        raise TransportError(
            f"pair « {peer.label()} » sans adresse MAC: impossible d'ouvrir un canal RFCOMM"
        )
    if not RFCOMM_AVAILABLE:
        raise TransportError("ce Python n'a pas été compilé avec le support Bluetooth")
    target_channel = channel if channel is not None else (peer.channel or config.transport.channel)
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    sock.settimeout(timeout)
    try:
        sock.connect((peer.mac, target_channel))
    except OSError as exc:
        sock.close()
        raise TransportError(
            f"connexion RFCOMM vers {peer.mac} canal {target_channel} impossible: {exc}"
        ) from exc
    return SocketConnection(sock, peer=f"{peer.mac}:{target_channel}")
