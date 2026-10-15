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
from .control import ControlServer, ControlUnavailable, call_control
from .config import (
    AUDIO_FAKE,
    Config,
    ConfigError,
    PeerConfig,
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
        # cible unique imposée par la ligne de commande
        cfg.peers = [PeerConfig(name=getattr(args, "peer_name", "") or "",
                                mac=args.peer_mac, channel=cfg.transport.channel)]
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


#: Attente maximale d'une bascule confiée au démon (secondes).
SWITCH_TIMEOUT_S = 45.0


def _via_daemon(cfg: Config, args: argparse.Namespace, command: str,
                payload: dict[str, Any] | None, action: str) -> int | None:
    """Confie la bascule au démon local s'il tourne (il voit les liens ouverts).

    Renvoie le code de sortie s'il a pris la main, ``None`` pour continuer en
    local (mode ``--direct``, ou aucun démon à l'écoute).
    """
    if getattr(args, "direct", False):
        return None
    try:
        # Une bascule prend du temps : le démon refait plusieurs tentatives de
        # connexion au casque (page timeout fréquent après une libération).
        result = call_control(cfg, command, payload, timeout=args.timeout or SWITCH_TIMEOUT_S)
    except ControlUnavailable as exc:
        log.debug("pas de démon local (%s): la bascule est pilotée par le CLI", exc)
        return None
    except (ConfigError, proto.RemoteError, TimeoutError) as exc:
        return _fail(str(exc))
    _emit(result, args.json, _switch_summary(action, result))
    return EXIT_OK if result.get("ok") else EXIT_FAILURE


def _build_switchboard(cfg: Config, sink: AudioSink, args: argparse.Namespace):
    """Construit le coordinateur multi-pairs (résolution des noms via BlueZ)."""
    from .switch import Switchboard

    resolver = None
    if not getattr(args, "no_resolve", False):
        try:
            client = bluez.open_client("auto")
            resolver = lambda peer: bluez.resolve_peer_mac(peer, client)  # noqa: E731
        except bluez.BluezError as exc:
            def resolver(peer: PeerConfig, _exc: Exception = exc) -> str:  # type: ignore[misc]
                if peer.mac:
                    return peer.mac
                raise bluez.BluezError(
                    f"{_exc} : impossible de résoudre « {peer.label()} » par son nom, "
                    "renseignez peers[].mac"
                )
    return Switchboard(cfg, sink, resolver=resolver, logger=log)


def _failures_text(result: dict[str, Any]) -> str:
    return "; ".join(
        f"{item.get('peer')}: {item.get('error')}" for item in result.get("failures") or []
    )


def _switch_summary(action: str, result: dict[str, Any]) -> str:
    """Résumé lisible d'une bascule (take/give)."""
    holders = ", ".join(result.get("holders") or [])
    if action == "take":
        if not result.get("ok"):
            return f"bascule incomplète — {_failures_text(result) or 'erreur inconnue'}"
        grabbed = result.get("grabbed") or {}
        local = "connectés" if grabbed.get("connected") else "connexion incertaine"
        return (f"écouteurs récupérés — libérés par: {holders or 'personne'}; "
                f"côté local: {local}")
    if not result.get("ok"):
        return f"restitution incomplète — {_failures_text(result) or 'erreur inconnue'}"
    grabbed = result.get("grabbed") or {}
    return (f"écouteurs rendus à « {result.get('peer')} » "
            f"({'connectés là-bas' if grabbed.get('connected') else 'reconnexion à confirmer'})")


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
    if args.peer_mac or args.peer_name:
        cfg.peers = [PeerConfig(name=args.peer_name or "", mac=args.peer_mac or "")]
    cfg.transport.mode = args.transport or cfg.transport.mode
    if args.channel:
        cfg.transport.channel = args.channel
    if args.token:
        cfg.token = args.token
    cfg.validate()
    path = cfg.save(args.config)
    print(f"configuration écrite dans {path}")
    print("renseignez au minimum les pairs (blueco peers add <nom>) et le casque")
    print("(`blueco devices` liste les adresses connues de BlueZ).")
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
    query_peers = args.remote or args.all
    if query_peers:
        board = _build_switchboard(cfg, sink, args)
        names = [args.from_peer] if args.from_peer else None
        payload.update(board.snapshot(names))
    if args.json:
        _emit(payload, True)
        return EXIT_OK
    print(f"appareil      : {cfg.device_name} ({cfg.transport.mode})")
    adapter = local["adapter"] or {}
    if adapter:
        print(f"adaptateur    : {adapter.get('address', '?')} {adapter.get('name', '')}".rstrip())
    print(f"{'casque':<13} : {_audio_human(local['audio'])}")
    if query_peers:
        reports = payload.get("peers", [])
        if not reports:
            print("pairs         : aucun pair configuré")
        for report in reports:
            if report.get("reachable"):
                marker = "← détient les écouteurs" if report.get("holds_headphones") else ""
                print(f"{report['peer']:<13} : joignable "
                      f"[{report.get('device', '?')}] {_audio_human(report.get('audio', {}))} {marker}".rstrip())
            else:
                print(f"{report['peer']:<13} : injoignable ({report.get('error', '')})")
    return EXIT_OK


def cmd_release(args: argparse.Namespace) -> int:
    """Demande à un (ou tous les) pair(s) de libérer le casque."""
    cfg = _load_config(args)
    timeout = args.timeout or cfg.request_timeout_s
    if args.local or not cfg.peers:
        sink = _build_sink(cfg)
        if args.local:
            result = sink.release()
            _emit(result, args.json, f"casque libéré localement: {result.get('released', '?')}")
            return EXIT_OK
        result = BluecoClient(cfg).release(timeout=timeout)
        _emit(result, args.json,
              f"le pair a libéré le casque: {result.get('released', '?')} "
              f"({'déconnecté' if result.get('was_connected') else 'déjà libre'})")
        return EXIT_OK

    board = _build_switchboard(cfg, _build_sink(cfg), args)
    targets = board.selected([args.from_peer] if args.from_peer else None)
    results: list[dict[str, Any]] = []
    for peer in targets:
        try:
            outcome = board.client(peer).release(timeout=timeout)
            results.append({"peer": peer.label(), "ok": True, **outcome})
        except Exception as exc:  # pair éteint, refus, délai…
            results.append({"peer": peer.label(), "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    if args.json:
        _emit(results, True)
        return EXIT_OK if any(item["ok"] for item in results) else EXIT_FAILURE
    for item in results:
        if not item["ok"]:
            print(f"{item['peer']:<13} : échec ({item['error']})")
        elif item.get("was_connected"):
            print(f"{item['peer']:<13} : a libéré les écouteurs")
        else:
            print(f"{item['peer']:<13} : ne les détenait pas (déjà libre)")
    return EXIT_OK if any(item["ok"] for item in results) else EXIT_FAILURE


def cmd_grab(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    timeout = args.timeout or cfg.request_timeout_s
    if args.remote:
        if args.to_peer or args.from_peer:
            board = _build_switchboard(cfg, _build_sink(cfg), args)
            peer = cfg.require_peer(args.to_peer or args.from_peer)
            result = board.client(peer).grab(timeout=timeout)
            _emit(result, args.json, f"« {peer.label()} » a repris le casque: {result.get('mac', '?')}")
            return EXIT_OK
        result = BluecoClient(cfg).grab(timeout=timeout)
        _emit(result, args.json, f"le pair a repris le casque: {result.get('mac', '?')}")
        return EXIT_OK
    sink = _build_sink(cfg)
    result = sink.grab()
    _emit(result, args.json, f"casque connecté localement: {result.get('mac', '?')}")
    return EXIT_OK


def cmd_take(args: argparse.Namespace) -> int:
    """Bouton « je prends les écouteurs » : qui les détient doit les libérer."""
    cfg = _load_config(args)
    sink = _build_sink(cfg)
    handled = _via_daemon(cfg, args, proto.CMD_TAKE,
                          {"from": args.from_peer} if args.from_peer else None,
                          "take")
    if handled is not None:
        return handled

    if not cfg.peers:
        # Aucun pair configuré (ou mode unix de diagnostic) : un seul interlocuteur.
        client = BluecoClient(cfg)
        steps: list[tuple[str, Any]] = []
        if not args.skip_release:
            steps.append(("pair.release", client.release(timeout=args.timeout or cfg.request_timeout_s)))
        steps.append(("local.grab", sink.grab()))
        if args.json:
            _emit(dict(steps), True)
        else:
            for name, result in steps:
                print(f"{name:<14} ok {result}")
        return EXIT_OK

    board = _build_switchboard(cfg, sink, args)
    result = board.take(source=args.from_peer, skip_release=args.skip_release)
    _emit(result, args.json, _switch_summary("take", result))
    return EXIT_OK if result.get("ok") else EXIT_FAILURE


def cmd_give(args: argparse.Namespace) -> int:
    """Bouton « je rends les écouteurs » : je les libère, le pair visé se connecte."""
    cfg = _load_config(args)
    sink = _build_sink(cfg)
    args_payload: dict[str, Any] = {}
    if args.to_peer:
        args_payload["to"] = args.to_peer
    if args.skip_grab:
        args_payload["skip_grab"] = True
    handled = _via_daemon(cfg, args, proto.CMD_GIVE, args_payload or None, "give")
    if handled is not None:
        return handled

    if not cfg.peers:
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

    board = _build_switchboard(cfg, sink, args)
    result = board.give(target=args.to_peer, skip_grab=args.skip_grab)
    _emit(result, args.json, _switch_summary("give", result))
    return EXIT_OK if result.get("ok") else EXIT_FAILURE


def cmd_peers(args: argparse.Namespace) -> int:
    """Lister, ajouter ou retirer les appareils avec qui basculer."""
    cfg = _load_config(args)
    action = args.action or "list"

    if action == "add":
        if not args.name:
            return _fail("précisez un nom de pair: blueco peers add bureau [--mac AA:..]")
        peer = PeerConfig(
            name=args.name,
            mac=args.mac or "",
            bluetooth_name=args.bt_name or "",
            mode=args.transport or "",
            channel=args.channel or cfg.transport.channel,
        )
        try:
            cfg.validate()
            cfg.add_peer(peer)
            cfg.validate()
        except ConfigError as exc:
            return _fail(str(exc))
        path = cfg.save(args.config)
        print(f"pair « {peer.label()} » enregistré dans {path}")
        print("vérifiez la résolution de son adresse : blueco peers")
        return EXIT_OK

    if action == "rm":
        if not args.name:
            return _fail("précisez le nom du pair à retirer: blueco peers rm bureau")
        if not cfg.remove_peer(args.name):
            return _fail(f"pair inconnu: « {args.name} » (pairs: {cfg.peer_labels() or 'aucun'})")
        path = cfg.save(args.config)
        print(f"pair « {args.name} » retiré ({path})")
        return EXIT_OK

    # list
    client = None
    try:
        client = bluez.open_client("auto")
    except bluez.BluezError as exc:
        log.info("BlueZ indisponible (%s): les adresses ne seront pas résolues", exc)
    rows: list[dict[str, Any]] = []
    for peer in cfg.peers:
        mac = peer.mac
        note = ""
        if not mac and client is not None:
            try:
                mac = bluez.resolve_peer_mac(peer, client)
                note = "résolu via BlueZ"
            except bluez.BluezError as exc:
                note = str(exc)
        elif mac:
            note = "figée dans la configuration"
        rows.append({"peer": peer.label(), "name": peer.name, "mac": mac,
                     "channel": peer.channel, "mode": peer.mode or cfg.transport.mode,
                     "token": bool(peer.token or cfg.token), "note": note})
    if args.json:
        _emit(rows, True)
        return EXIT_OK
    if not rows:
        print("aucun pair configuré")
        print("ajoutez-en un : blueco peers add bureau     # puis vérifiez la résolution")
        return EXIT_OK
    width = max(len(str(row["peer"])) for row in rows)
    for row in rows:
        state = row["mac"] or "(non résolu)"
        print(f"{str(row['peer']).ljust(width)}  {state:<17}  "
              f"canal {row['channel']}  {row['mode']}  {row['note']}".rstrip())
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
    control: ControlServer | None = None
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

    control = ControlServer(server)
    control.start()
    print(f"blueco {__version__} écoute sur {listener.describe()} (Ctrl-C pour arrêter)")
    print(f"canal de contrôle local : {cfg.transport.resolved_control_socket()}")
    print("  → `blueco take` et `blueco give` s'en servent pour agir sur les liens ouverts")
    try:
        server.serve_forever(stop)
    except KeyboardInterrupt:  # pragma: no cover
        server.stop()
    finally:
        if control is not None:
            control.stop()
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
    "peer_mac": None,
    "no_resolve": False,
    "direct": False,
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
    common.add_argument("--peer-mac", default=argparse.SUPPRESS,
                        help="viser un seul pair par son adresse MAC (écrase « peers »)")
    common.add_argument("--no-resolve", action="store_true", default=argparse.SUPPRESS,
                        help="ne pas résoudre les noms de pairs via BlueZ")
    common.add_argument("--direct", action="store_true", default=argparse.SUPPRESS,
                        help="ignorer le démon local et agir depuis ce processus")
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
    p_init.add_argument("--peer-name", help="étiquette du pair à enregistrer (ex. « téléphone »)")
    p_init.add_argument("--headphones-mac")
    p_init.add_argument("--headphones-name")
    p_init.add_argument("--token")
    p_init.set_defaults(func=cmd_init)

    p_devices = sub.add_parser("devices", parents=[common], help="lister les appareils connus de BlueZ")
    p_devices.set_defaults(func=cmd_devices)

    p_status = sub.add_parser("status", parents=[common], help="état local et, avec --remote/--all, état des pairs")
    p_status.add_argument("--remote", action="store_true", help="interroger aussi les pairs")
    p_status.add_argument("--all", action="store_true", help="interroger tous les pairs configurés")
    p_status.add_argument("--from", dest="from_peer", help="interroger seulement ce pair")
    p_status.set_defaults(func=cmd_status)

    p_release = sub.add_parser("release", parents=[common], help="libérer le casque (--local sinon côté pair)")
    p_release.add_argument("--local", action="store_true", help="déconnecter localement au lieu du pair")
    p_release.add_argument("--from", dest="from_peer", help="ne demander qu'à ce pair")
    p_release.add_argument("--timeout", type=float)
    p_release.set_defaults(func=cmd_release)

    p_grab = sub.add_parser("grab", parents=[common], help="connecter le casque (--remote pour le pair)")
    p_grab.add_argument("--remote", action="store_true", help="demander au pair de connecter")
    p_grab.add_argument("--to", dest="to_peer", help="le pair qui doit reprendre les écouteurs")
    p_grab.add_argument("--timeout", type=float)
    p_grab.set_defaults(func=cmd_grab)

    p_take = sub.add_parser("take", parents=[common], help="je prends le casque: le pair le libère puis je me connecte")
    p_take.add_argument("--from", dest="from_peer", help="ne s'adresser qu'à ce pair (défaut: tous)")
    p_take.add_argument("--skip-release", action="store_true")
    p_take.add_argument("--timeout", type=float)
    p_take.set_defaults(func=cmd_take)

    p_give = sub.add_parser("give", parents=[common], help="je rends le casque: je le libère puis le pair se connecte")
    p_give.add_argument("--to", dest="to_peer", help="le pair qui doit reprendre les écouteurs")
    p_give.add_argument("--skip-grab", action="store_true")
    p_give.add_argument("--timeout", type=float)
    p_give.set_defaults(func=cmd_give)

    p_peers = sub.add_parser("peers", parents=[common], help="lister, ajouter ou retirer les pairs")
    p_peers.add_argument("action", nargs="?", choices=("list", "add", "rm"), default="list")
    p_peers.add_argument("name", nargs="?", help="étiquette du pair")
    p_peers.add_argument("--mac", help="adresse MAC (sinon résolue via BlueZ à l'exécution)")
    p_peers.add_argument("--bt-name", help="nom Bluetooth si différent de l'étiquette")
    p_peers.set_defaults(func=cmd_peers)

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
