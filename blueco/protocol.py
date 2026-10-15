"""Protocole d'échange blueco (version 1).

Trame = un objet JSON UTF-8 par ligne, terminé par ``\\n`` (JSON Lines).
Ce module est la référence normative ; ``docs/PROTOCOL.md`` le décrit et
l'implémentation Kotlin (``android/.../Protocol.kt``) doit rester alignée.

Trois types de trames circulent sur la même connexion, dans les deux sens :

* **requête**  : ``{"v":1,"id":"<12 hex>","cmd":"RELEASE","args":{...},"token":"..."}``
* **réponse**  : ``{"v":1,"id":"<id de la requête>","ok":true,"result":{...}}``
  ou ``{"v":1,"id":"...","ok":false,"error":{"code":"...","message":"..."}}``
* **événement**: ``{"v":1,"evt":"STATE","data":{...}}`` (non sollicité, sans ``id``)
"""

from __future__ import annotations

import json
import select
import socket
import uuid
from typing import Any, Mapping

PROTOCOL_VERSION = 1
VERSION = PROTOCOL_VERSION

MAX_FRAME = 2048
DEFAULT_TIMEOUT = 15.0

#: UUID du profil SPP (Serial Port Profile) - identique côté Android.
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"

CMD_PING = "PING"
CMD_STATUS = "STATUS"
CMD_RELEASE = "RELEASE"
CMD_GRAB = "GRAB"
CMD_WHOAMI = "WHOAMI"

COMMANDS = frozenset({CMD_PING, CMD_STATUS, CMD_RELEASE, CMD_GRAB, CMD_WHOAMI})

#: Commandes qu'un pair est autorisé à nous demander.
INBOUND_COMMANDS = COMMANDS

#: Événements non sollicités émis par le démon Linux.
EVT_STATE = "STATE"
EVT_HELLO = "HELLO"

# Codes d'erreur
E_BAD_FRAME = "BAD_FRAME"
E_UNSUPPORTED = "UNSUPPORTED_COMMAND"
E_UNAUTHORIZED = "UNAUTHORIZED"
E_NO_DEVICE = "NO_DEVICE"
E_AUDIO_UNAVAILABLE = "AUDIO_UNAVAILABLE"
E_NOT_CONNECTED = "NOT_CONNECTED"
E_TIMEOUT = "TIMEOUT"
E_BUSY = "BUSY"
E_INTERNAL = "INTERNAL"


class ProtocolError(Exception):
    """Trame illisible ou non conforme."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class RemoteError(Exception):
    """Le pair a répondu ``ok:false``."""

    def __init__(self, code: str, message: str = "", command: str | None = None) -> None:
        super().__init__(f"{command or 'remote'}: {code}: {message}".rstrip(": "))
        self.code = code
        self.message = message
        self.command = command


def new_id() -> str:
    """Identifiant de requête court, unique par processus."""
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- #
# Encodage / décodage
# --------------------------------------------------------------------------- #
def encode(message: Mapping[str, Any]) -> bytes:
    """Sérialise une trame en une ligne terminée par ``\\n``."""
    try:
        payload = json.dumps(dict(message), separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - erreur programmeur
        raise ProtocolError(E_BAD_FRAME, f"non sérialisable: {exc}") from exc
    raw = payload.encode("utf-8")
    if len(raw) > MAX_FRAME:
        raise ProtocolError(E_BAD_FRAME, f"trame trop longue ({len(raw)} > {MAX_FRAME} octets)")
    return raw + b"\n"


def decode(raw: bytes | str) -> dict:
    """Valide et désérialise une ligne. Lève :class:`ProtocolError`."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    raw = raw.strip()
    if not raw:
        raise ProtocolError(E_BAD_FRAME, "trame vide")
    if len(raw) > MAX_FRAME:
        raise ProtocolError(E_BAD_FRAME, f"trame trop longue ({len(raw)} > {MAX_FRAME} octets)")
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(E_BAD_FRAME, f"JSON invalide: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError(E_BAD_FRAME, "la trame doit être un objet JSON")

    version = message.get("v", PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        raise ProtocolError(E_BAD_FRAME, f"version de protocole non supportée: {version!r}")

    if "evt" in message:
        if not isinstance(message["evt"], str) or not message["evt"]:
            raise ProtocolError(E_BAD_FRAME, "event sans nom")
        return message

    if "ok" in message:  # réponse à une requête
        if not isinstance(message.get("id"), str):
            raise ProtocolError(E_BAD_FRAME, "réponse sans 'id'")
        if not isinstance(message["ok"], bool):
            raise ProtocolError(E_BAD_FRAME, "'ok' doit être un booléen")
        if message["ok"] and "result" in message and not isinstance(message["result"], dict):
            raise ProtocolError(E_BAD_FRAME, "'result' doit être un objet")
        if not message["ok"] and "error" in message and not isinstance(message["error"], dict):
            raise ProtocolError(E_BAD_FRAME, "'error' doit être un objet")
        return message

    if "cmd" not in message:
        raise ProtocolError(E_BAD_FRAME, "ni 'cmd' (requête), ni 'ok' (réponse), ni 'evt' (événement)")
    command = message["cmd"]
    if not isinstance(command, str):
        raise ProtocolError(E_BAD_FRAME, "cmd doit être une chaîne")
    command = command.upper()
    if command not in COMMANDS:
        raise ProtocolError(E_UNSUPPORTED, f"commande inconnue: {command!r}")
    message["cmd"] = command

    request_id = message.get("id")
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolError(E_BAD_FRAME, "requête sans 'id'")
    if "args" in message and not isinstance(message["args"], dict):
        raise ProtocolError(E_BAD_FRAME, "args doit être un objet")
    return message


def is_reply(message: Mapping[str, Any]) -> bool:
    return "id" in message and "ok" in message


def is_request(message: Mapping[str, Any]) -> bool:
    return "cmd" in message and "ok" not in message


# --------------------------------------------------------------------------- #
# Constructeurs de trames
# --------------------------------------------------------------------------- #
def request(
    cmd: str,
    args: Mapping[str, Any] | None = None,
    *,
    token: str | None = None,
    request_id: str | None = None,
    origin: str | None = None,
) -> dict:
    msg: dict[str, Any] = {"v": PROTOCOL_VERSION, "id": request_id or new_id(), "cmd": cmd.upper()}
    if args:
        msg["args"] = dict(args)
    if token:
        msg["token"] = token
    if origin:
        msg["from"] = origin
    return msg


def ok(request_id: str, result: Mapping[str, Any] | None = None, **extra: Any) -> dict:
    payload = dict(result or {})
    payload.update(extra)
    return {"v": PROTOCOL_VERSION, "id": request_id, "ok": True, "result": payload}


def failure(request_id: str, code: str, message: str = "", **extra: Any) -> dict:
    error: dict[str, Any] = {"code": code}
    if message:
        error["message"] = message
    error.update(extra)
    return {"v": PROTOCOL_VERSION, "id": request_id, "ok": False, "error": error}


def event(name: str, data: Mapping[str, Any] | None = None, **extra: Any) -> dict:
    payload = dict(data or {})
    payload.update(extra)
    return {"v": PROTOCOL_VERSION, "evt": name, "data": payload}


def result_of(message: Mapping[str, Any]) -> dict:
    """Retourne ``result`` ou lève :class:`RemoteError` si ``ok`` est faux."""
    if message.get("ok"):
        result = message.get("result")
        return dict(result) if isinstance(result, dict) else {}
    error = message.get("error") or {}
    if not isinstance(error, dict):
        error = {}
    raise RemoteError(str(error.get("code", E_INTERNAL)), str(error.get("message", "")))


# --------------------------------------------------------------------------- #
# Lecture de lignes sur socket
# --------------------------------------------------------------------------- #
class LineReader:
    """Lecteur de trames bufferisé sur une socket, avec délai maximal."""

    def __init__(self, sock: socket.socket, max_frame: int = MAX_FRAME) -> None:
        self._sock = sock
        self._max = max_frame
        self._buffer = bytearray()

    def has_buffered_line(self) -> bool:
        return b"\n" in self._buffer

    def readline(self, timeout: float | None = DEFAULT_TIMEOUT) -> bytes | None:
        """Retourne la ligne brute (sans ``\\n``) ou ``None`` si EOF.

        Lève :class:`TimeoutError` si rien n'arrive dans ``timeout`` secondes.
        """
        while True:
            index = self._buffer.find(b"\n")
            if index != -1:
                line = bytes(self._buffer[:index])
                del self._buffer[: index + 1]
                return line
            if len(self._buffer) > self._max:
                # On vide le tampon pour ne pas boucler sur la même erreur ;
                # le reste de la ligne sera traité comme des trames invalides.
                oversized = len(self._buffer)
                self._buffer.clear()
                raise ProtocolError(E_BAD_FRAME, f"trame trop longue ({oversized} > {self._max} octets)")
            try:
                ready, _, _ = select.select([self._sock], [], [], timeout)
            except (OSError, ValueError) as exc:
                raise ProtocolError(E_NOT_CONNECTED, f"socket fermée: {exc}") from exc
            if not ready:
                raise TimeoutError("aucune trame reçue dans le délai imparti")
            try:
                chunk = self._sock.recv(4096)
            except (OSError, ValueError) as exc:
                raise ProtocolError(E_NOT_CONNECTED, f"lecture impossible: {exc}") from exc
            if not chunk:
                if self._buffer:
                    line = bytes(self._buffer)
                    self._buffer.clear()
                    return line
                return None
            self._buffer.extend(chunk)
