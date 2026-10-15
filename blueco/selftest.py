"""Auto-test de bout en bout, sans matériel Bluetooth.

Démarre un vrai :class:`~blueco.server.BluecoServer` sur une socket locale avec
un backend audio simulé, joue le rôle du téléphone, et vérifie le protocole :
ping, état, libération, reprise, poussée d'ordre vers le pair, file d'attente et
contrôle du jeton. C'est ce que lance ``blueco selftest``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import protocol as proto
from .audio import FakeAudioSink
from .client import BluecoClient
from .config import AUDIO_FAKE, TRANSPORT_UNIX, Config
from .server import BluecoServer
from .session import Session
from .transport import UnixListener, connect_to


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class _Phone:
    """Simule l'appli Android : répond aux requêtes et mémorise les ordres reçus."""

    def __init__(self, config: Config) -> None:
        self.received: list[str] = []
        self.connected = False
        self._conn = connect_to(config, timeout=5.0)
        self.session = Session(self._conn, on_request=self._handle, name="phone").start()
        self.connected = True

    def _handle(self, message: dict) -> dict:
        command = str(message.get("cmd", ""))
        self.received.append(command)
        return proto.ok(str(message.get("id", "")), accepted=True, cmd=command)

    def close(self) -> None:
        self.connected = False
        self.session.close()


def run_selftest(as_json: bool = False) -> int:
    results: list[dict[str, Any]] = []
    tmp = Path(tempfile.mkdtemp(prefix="blueco-selftest-"))

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append({"check": name, "ok": bool(ok), "detail": detail})

    config = Config()
    config.device_name = "pc-selftest"
    config.token = ""
    config.audio_backend = AUDIO_FAKE
    config.transport.mode = TRANSPORT_UNIX
    config.transport.socket_path = str(tmp / "selftest.sock")
    config.request_timeout_s = 5.0

    sink = FakeAudioSink()
    listener = UnixListener(config.transport.resolved_socket_path())
    server = BluecoServer(config, sink, listener)
    stop = threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(stop,), daemon=True)
    thread.start()

    phone: _Phone | None = None
    client = BluecoClient(config)
    try:
        # 1. ping / whoami
        pong = client.ping(timeout=5.0)
        check("ping", pong.get("pong") is True and pong.get("device") == "pc-selftest", str(pong))

        # 2. état initial : le casque est « tenu » par le PC
        state = client.status(timeout=5.0)
        check("status.held", state.get("headphones_held") is True,
              json.dumps(state.get("audio", {}), ensure_ascii=False))
        check("status.protocol", state.get("protocol") == proto.PROTOCOL_VERSION, str(state.get("protocol")))

        # 3. le téléphone demande la libération du casque
        released = client.release(timeout=5.0)
        check("release", released.get("was_connected") is True and sink.connected is False, str(released))

        # 4. libération idempotente
        again = client.release(timeout=5.0)
        check("release.idempotent", again.get("already_free") is True, str(again))

        # 5. reprise locale
        grabbed = client.grab(timeout=5.0)
        check("grab", grabbed.get("connected") is True and sink.connected is True, str(grabbed))

        # 6. le PC pousse un ordre vers le téléphone (cas « bascule depuis le PC »)
        sink.release()  # le PC lâche le casque, c'est au téléphone de le prendre
        phone = _Phone(config)
        ok_live = _wait_for(lambda: server.live_session() is not None, timeout=5.0)
        check("session.pair", ok_live, f"{len(server._sessions)} session(s)")
        pushed = server.push(proto.CMD_RELEASE, timeout=5.0)
        check("push.release", pushed.get("accepted") is True, str(pushed))

        # 7. file d'attente quand le pair n'est pas connecté
        phone.close()
        _wait_for(lambda: server.live_session() is None, timeout=5.0)
        queued = server.push(proto.CMD_GRAB, allow_queue=True, timeout=5.0)
        check("push.queue", queued.get("queued") is True, str(queued))
        phone = _Phone(config)
        delivered = _wait_for(lambda: proto.CMD_GRAB in phone.received, timeout=8.0)
        check("push.queue.delivered", delivered, str(phone.received))

        # 8. jeton partagé
        protected = Config.from_dict({**config.to_dict(), "token": "s3cret"})
        guarded = BluecoServer(protected, FakeAudioSink(), UnixListener(str(tmp / "guarded.sock")))
        denied = guarded.dispatch(proto.request(proto.CMD_RELEASE))
        allowed = guarded.dispatch(proto.request(proto.CMD_RELEASE, token="s3cret"))
        guarded.stop()
        check("auth.denied", denied.get("ok") is False
              and denied["error"]["code"] == proto.E_UNAUTHORIZED, str(denied))
        check("auth.allowed", allowed.get("ok") is True, str(allowed))

        # 9. commande inconnue
        bad = server.dispatch({"v": 1, "id": "x1", "cmd": "DESTROY"})
        check("dispatch.unknown", bad.get("ok") is False, str(bad))

        # 10. backend en panne
        broken = FakeAudioSink(fail_on={"release"})
        failing = BluecoServer(config, broken, UnixListener(str(tmp / "failing.sock")))
        failure = failing.dispatch(proto.request(proto.CMD_RELEASE))
        failing.stop()
        check("audio.error", failure.get("ok") is False
              and failure["error"]["code"] == proto.E_AUDIO_UNAVAILABLE, str(failure))
    finally:
        if phone is not None:
            phone.close()
        stop.set()
        server.stop()
        thread.join(timeout=5.0)
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [r for r in results if not r["ok"]]
    if as_json:
        print(json.dumps({"ok": not failures, "checks": results}, indent=2, ensure_ascii=False))
    else:
        for item in results:
            mark = "OK  " if item["ok"] else "ÉCHEC"
            print(f"[{mark}] {item['check']:<22} {item['detail']}")
        print(f"\n{len(results) - len(failures)}/{len(results)} vérifications réussies")
    return 0 if not failures else 1
