"""Interface en ligne de commande de blueco.

    blueco init            # écrit ~/.config/blueco/config.json
    blueco devices         # liste les appareils appairés
    blueco serve           # démarre le démon (serveur RFCOMM/SPP)
    blueco status --remote # état local + état du téléphone
    blueco take            # « je prends le casque » : le pair libère, je connecte
    blueco give            # « je rends le casque » : je libère, le pair connecte
    blueco watch           # verrouille la session quand le téléphone s'éloigne
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from typing import Any, Sequence

from . import __version__, bluez, protocol as proto
from .audio import AudioError, AudioSink, FakeAudioSink, create_sink
from .client import BluecoClient
from .config import (
    AUDIO_FAKE,
    Config,
    ConfigError,
    default_config_path,
)
from .server import BluecoServer
from .transport import TransportError, create_listener
from .watch import PresenceWatcher, peer_state_event

log = logging.getLogger("blueco")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def _emit(payload: Any, as_json: bool, human: str | None = None) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif human is not None:
        print(human)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))


def _load_config(args: argparse.Namespace) -> Config:
    try:
        cfg = Config.load(getattr(args, "config", None))
    except ConfigError as exc:
        raise SystemExit(f"configuration invalide: {exc}") from exc
    if getattr(args, "transport", None):
        cfg.transport.mode = args.transport
    if getattr(args, "channel", None):
        cfg.transport.channel = args.channel
    if getattr(args, "socket", None):
        cfg.transport.socket_path = args.socket
    if getattr(args, "fake", False):
        cfg.audio_backend = AUDIO_FAKE
    if getattr(args, "peer_mac", None):
        cfg.peer.mac = args.peer_mac
    if getattr(args, "headphones_mac", None):
        cfg.headphones.mac = args.headphones_mac
    if getattr(args, "device_name", None):
        cfg.device_name = args.device_name
    cfg.validate()
    return cfg


def _build_sink(cfg: Config) -> AudioSink:
    if cfg.audio_backend == AUDIO_FAKE:
        log.info("backend audio simulé (--fake): aucune action Bluetooth réelle")
        return FakeAudioSink(mac=cfg.headphones.mac or "AA:BB:CC:DD:EE:FF",
                             name=cfg.headphones.name or "Fake Buds")
    try:
        client = bluez.open_client("auto")
    except bluez.BluezError as exc:
        raise SystemExit(f"BlueZ indisponible: {exc} (utilisez --fake pour simuler)") from exc
    return create_sink(cfg, client)


def _audio_human(state: dict) -> str:
    name = state.get("name") or state.get("mac") or "casque"
    if not state.get("found", True):
        return f"{name}: introuvable"
    return f"{name}: {'connecté' if state.get('connected') else 'libre'}"


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace) -> int:
    cfg = Config()
    cfg.device_name = args.device_name or cfg.device_name
    cfg.headphones.mac = args.headphones_mac or ""
    cfg.headphones.name = args.headphones_name or ""
    cfg.peer.mac = args.peer_mac or ""
    cfg.peer.name = args.peer_name or ""
    cfg.transport.mode = args.transport or cfg.transport.mode
    if args.channel:
        cfg.transport.channel = args.channel
    if args.token:
        cfg.token = args.token
    path = cfg.save(args.config)
    print(f"configuration écrite dans {path}")
    print("comprenez au minimum peer.mac (le téléphone) et headphones.mac (le casque).")
    return EXIT_OK


def cmd_devices(args: argparse.Namespace) -> int:
    try:
        client = bluez.open_client("auto")
    except bluez.BluezError as exc:
        return _fail(f"BlueZ indisponible: {exc}")
    devices = client.devices()
    if args.json:
        _emit([d.as_dict() for d in devices], True)
        return EXIT_OK
    if not devices:
        print("aucun appareil connu de BlueZ (appairez d'abord le téléphone et le casque)")
        return EXIT_OK
    width = max(len(d.name) for d in devices)
    for device in devices:
        flags = []
        if device.connected:
            flags.append("connecté")
        if device.paired:
            flags.append("appairé")
        if device.trusted:
            flags.append("de confiance")
        print(f"{device.address}  {device.name.ljust(width)}  {', '.join(flags)}")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    sink = _build_sink(cfg)
    local = {
        "device": cfg.device_name,
        "audio": sink.status(),
        "adapter": sink.adapter_info(),
        "transport": cfg.transport.mode,
    }
    payload: dict[str, Any] = {"local": local}
    if args.remote:
        payload["peer"] = peer_state_event(BluecoClient(cfg), timeout=cfg.request_timeout_s)
    if args.json:
        _emit(payload, True)
        return EXIT_OK
    print(f"appareil      : {cfg.device_name} ({cfg.transport.mode})")
    adapter = local["adapter"] or {}
    if adapter:
        print(f"adaptateur    : {adapter.get('address', '?')} {adapter.get('name', '')}".rstrip())
    print(f"{'casque':<13} : {_audio_human(local['audio'])}")
    if args.remote:
        peer = payload["peer"]
        if peer.get("reachable"):
            remote = peer["status"]
            print(f"pair          : joignable ({remote.get('device', '?')})")
            print(f"{'casque pair':<13} : {_audio_human(remote.get('audio', {}))}")
        else:
            print(f"pair          : injoignable ({peer.get('error', '')})")
    return EXIT_OK


def cmd_release(args: argparse.Namespace) -> int:
    """Demande au pair de libérer le casque (déconnexion côté pair)."""
    cfg = _load_config(args)
    if args.local:
        sink = _build_sink(cfg)
        result = sink.release()
        _emit(result, args.json, f"casque libéré localement: {result.get('released', '?')}")
        return EXIT_OK
    result = BluecoClient(cfg).release(timeout=args.timeout or cfg.request_timeout_s)
    _emit(result, args.json,
          f"le pair a libéré le casque: {result.get('released', '?')} "
          f"({'déconnecté' if result.get('was_connected') else 'déjà libre'})")
    return EXIT_OK


def cmd_grab(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    if args.remote:
        result = BluecoClient(cfg).grab(timeout=args.timeout or cfg.request_timeout_s)
        _emit(result, args.json, f"le pair a repris le casque: {result.get('mac', '?')}")
        return EXIT_OK
    sink = _build_sink(cfg)
    result = sink.grab()
    _emit(result, args.json, f"casque connecté localement: {result.get('mac', '?')}")
    return EXIT_OK


def cmd_take(args: argparse.Namespace) -> int:
    """Bouton « je prends le casque » depuis le PC."""
    cfg = _load_config(args)
    sink = _build_sink(cfg)
    client = BluecoClient(cfg)
    steps: list[tuple[str, Any]] = []
    if not args.skip_release:
        result = client.release(timeout=args.timeout or cfg.request_timeout_s)
        steps.append(("pair.release", result))
    steps.append(("local.grab", sink.grab()))
    if args.json:
        _emit(dict(steps), True)
    else:
        for name, result in steps:
            print(f"{name:<14} ok {result}")
    return EXIT_OK


def cmd_give(args: argparse.Namespace) -> int:
    """Bouton « je rends le casque » depuis le PC."""
    cfg = _load_config(args)
    sink = _build_sink(cfg)
    client = BluecoClient(cfg)
    steps: list[tuple[str, Any]] = [("local.release", sink.release())]
    if not args.skip_grab:
        steps.append(("pair.grab", client.grab(timeout=args.timeout or cfg.request_timeout_s)))
    if args.json:
        _emit(dict(steps), True)
    else:
        for name, result in steps:
            print(f"{name:<14} ok {result}")
    return EXIT_OK


def cmd_ping(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    result = BluecoClient(cfg).ping(timeout=args.timeout or cfg.request_timeout_s)
    _emit(result, args.json, f"pong de {result.get('device', '?')}")
    return EXIT_OK


def cmd_selftest(args: argparse.Namespace) -> int:
    """Vérifie de bout en bout le protocole sur une socket locale (sans Bluetooth)."""
    from .selftest import run_selftest

    return run_selftest(as_json=args.json)


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    sink = _build_sink(cfg)
    try:
        listener = create_listener(cfg, mode=args.transport, socket_path=args.socket,
                                   channel=args.channel)
    except TransportError as exc:
        raise SystemExit(f"transport indisponible: {exc}") from exc
    server = BluecoServer(cfg, sink, listener)
    stop = threading.Event()

    def _stop(signum: int, _frame: object) -> None:
        log.info("signal %s reçu: arrêt", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):  # pragma: no cover - fil non principal
            pass

    if args.once:
        log.info("mode --once: une connexion puis sortie")
        conn = listener.accept(timeout=args.timeout or None)
        if conn is None:
            _fail("aucune connexion reçue dans le délai")
            return EXIT_FAILURE
        try:
            server.handle(conn)
        finally:
            server.stop()
        return EXIT_OK

    print(f"blueco {__version__} écoute sur {listener.describe()} (Ctrl-C pour arrêter)")
    try:
        server.serve_forever(stop)
    except KeyboardInterrupt:  # pragma: no cover
        server.stop()
    return EXIT_OK


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    watcher = PresenceWatcher(
        cfg,
        interval=args.interval,
        failures=args.failures,
        lock_command=args.lock_command if args.lock_command is not None else cfg.lock_command,
        unlock_command=args.unlock_command or "",
        rssi_threshold=args.rssi_threshold,
        grab_on_return=args.grab_on_return,
        on_change=lambda present: log.info("pair %s", "présent" if present else "absent"),
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda s, f: stop.set())
        except (ValueError, OSError):  # pragma: no cover
            pass
    if args.once:
        _emit(watcher.tick(), args.json)
        return EXIT_OK
    watcher.run(stop)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Analyse des arguments
# --------------------------------------------------------------------------- #
#: Valeurs utilisées quand une option globale n'est pas fournie (voir plus bas :
#: les options communes sont déclarées avec ``SUPPRESS`` sur les sous-commandes,
#: pour qu'une option donnée avant *ou* après la sous-commande soit prise en compte.
OPTION_DEFAULTS: dict[str, Any] = {
    "config": None,
    "transport": None,
    "channel": None,
    "socket": None,
    "fake": False,
    "json": False,
    "device_name": None,
    "verbose": 0,
}


def _common_options() -> argparse.ArgumentParser:
    """Options acceptées avant *et* après la sous-commande (`blueco serve -v`)."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help=f"fichier de configuration (défaut: {default_config_path()})")
    common.add_argument("--transport", choices=("profile1", "rfcomm", "unix"),
                        default=argparse.SUPPRESS,
                        help="transport à utiliser (écrase la configuration)")
    common.add_argument("--channel", type=int, default=argparse.SUPPRESS,
                        help="canal RFCOMM (mode rfcomm)")
    common.add_argument("--socket", default=argparse.SUPPRESS,
                        help="chemin de socket (mode unix)")
    common.add_argument("--fake", action="store_true", default=argparse.SUPPRESS,
                        help="backend audio simulé (aucune action Bluetooth)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="sortie JSON")
    common.add_argument("--device-name", default=argparse.SUPPRESS,
                        help="nom logique de cet appareil")
    common.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS,
                        help="-v: info, -v -v: debug")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="blueco",
        parents=[common],
        description="Bascule d'écouteurs Bluetooth entre Linux et Android, uniquement en Bluetooth.",
    )
    parser.add_argument("--version", action="version", version=f"blueco {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", parents=[common], help="écrire un fichier de configuration")
    p_init.add_argument("--peer-mac")
    p_init.add_argument("--peer-name")
    p_init.add_argument("--headphones-mac")
    p_init.add_argument("--headphones-name")
    p_init.add_argument("--token")
    p_init.set_defaults(func=cmd_init)

    p_devices = sub.add_parser("devices", parents=[common], help="lister les appareils connus de BlueZ")
    p_devices.set_defaults(func=cmd_devices)

    p_status = sub.add_parser("status", parents=[common], help="état local et, avec --remote, état du pair")
    p_status.add_argument("--remote", action="store_true", help="interroger aussi le pair")
    p_status.set_defaults(func=cmd_status)

    p_release = sub.add_parser("release", parents=[common], help="libérer le casque (--local sinon côté pair)")
    p_release.add_argument("--local", action="store_true", help="déconnecter localement au lieu du pair")
    p_release.add_argument("--timeout", type=float)
    p_release.set_defaults(func=cmd_release)

    p_grab = sub.add_parser("grab", parents=[common], help="connecter le casque (--remote pour le pair)")
    p_grab.add_argument("--remote", action="store_true", help="demander au pair de connecter")
    p_grab.add_argument("--timeout", type=float)
    p_grab.set_defaults(func=cmd_grab)

    p_take = sub.add_parser("take", parents=[common], help="je prends le casque: le pair le libère puis je me connecte")
    p_take.add_argument("--skip-release", action="store_true")
    p_take.add_argument("--timeout", type=float)
    p_take.set_defaults(func=cmd_take)

    p_give = sub.add_parser("give", parents=[common], help="je rends le casque: je le libère puis le pair se connecte")
    p_give.add_argument("--skip-grab", action="store_true")
    p_give.add_argument("--timeout", type=float)
    p_give.set_defaults(func=cmd_give)

    p_ping = sub.add_parser("ping", parents=[common], help="tester la liaison avec le pair")
    p_ping.add_argument("--timeout", type=float)
    p_ping.set_defaults(func=cmd_ping)

    p_self = sub.add_parser("selftest", parents=[common], help="auto-test du protocole sur socket locale")
    p_self.set_defaults(func=cmd_selftest)

    p_serve = sub.add_parser("serve", parents=[common], help="démarrer le démon (serveur SPP)")
    p_serve.add_argument("--once", action="store_true", help="servir une seule connexion puis sortir")
    p_serve.add_argument("--timeout", type=float, help="délai d'attente en mode --once")
    p_serve.set_defaults(func=cmd_serve)

    p_watch = sub.add_parser("watch", parents=[common], help="surveiller la proximité du pair")
    p_watch.add_argument("--interval", type=float, default=10.0)
    p_watch.add_argument("--failures", type=int, default=3, help="échecs consécutifs avant verrouillage")
    p_watch.add_argument("--lock-command", default=None, help="commande de verrouillage")
    p_watch.add_argument("--unlock-command", default="", help="commande de déverrouillage")
    p_watch.add_argument("--rssi-threshold", type=int, default=None, help="seuil RSSI en dBm (optionnel)")
    p_watch.add_argument("--grab-on-return", action="store_true")
    p_watch.add_argument("--once", action="store_true")
    p_watch.set_defaults(func=cmd_watch)

    return parser


def _fail(message: str) -> int:
    print(f"erreur: {message}", file=sys.stderr)
    return EXIT_FAILURE


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Les options communes sont déclarées avec SUPPRESS : on rétablit les défauts
    # pour que les commandes puissent les lire sans garde ``hasattr``.
    for name, value in OPTION_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    level = logging.WARNING if args.verbose == 0 else (logging.INFO if args.verbose == 1 else logging.DEBUG)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    try:
        return int(args.func(args) or EXIT_OK)
    except ConfigError as exc:
        return _fail(str(exc))
    except AudioError as exc:
        return _fail(exc.message or exc.code)
    except TransportError as exc:
        return _fail(str(exc))
    except proto.RemoteError as exc:
        return _fail(f"le pair a refusé: {exc.code} {exc.message}".rstrip())
    except TimeoutError as exc:
        return _fail(f"délai dépassé: {exc}")
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
