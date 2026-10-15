# blueco

**Basculer les écouteurs Bluetooth entre un PC Linux et un téléphone Android, en
un clic, uniquement en Bluetooth.** Pas de Wi-Fi, pas de réseau local, pas de
serveur tiers : tout passe par un canal Bluetooth direct (RFCOMM / profil SPP).

```
        ┌──────────────┐        RFCOMM / SPP        ┌──────────────────┐
        │   PC Linux   │◄──── appareils appairés ───►│  Android blueco  │
        │  démon blueco│      (aucun réseau)        │ service 1er plan │
        └──────┬───────┘                            └────────┬─────────┘
               │              ┌────────────────┐              │
               └─────────────►│  🎧 écouteurs  │◄─────────────┘
                     A2DP/HFP │   Bluetooth    │  A2DP/HFP
                              └────────────────┘
```

## Comment ça marche

1. Les appareils (PC Linux, téléphone Android, autres PC) sont appairés en
   Bluetooth classique.
2. Chaque démon Linux enregistre un profil **SPP** auprès de BlueZ
   (`org.bluez.ProfileManager1`) : BlueZ publie l'enregistrement SDP et alloue
   le canal RFCOMM. L'appli Android s'y connecte par UUID, avec l'API publique
   `createRfcommSocketToServiceRecord()` ; entre PC, on se joint par le canal
   RFCOMM fixe déclaré dans la configuration.
3. Un protocole JSON minimal circule dans les deux sens sur cette connexion.
4. Comme un casque n'est tenu que par **un seul** appareil à la fois, toute
   bascule commence par la même question : **qui le détient ?** `blueco take`
   interroge chaque pair (`STATUS`), fait lâcher celui qui l'a (`RELEASE`), puis
   se connecte. Aucun ordre nouveau : on réutilise le même protocole, que
   l'appareil soit un PC ou un téléphone.
5. Si le pair visé n'est pas joignable, l'ordre est mis en file d'attente et
   délivré à la reconnexion (cas du téléphone, qui ouvre la liaison).

Détail de conception et justification des choix : [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

### Trois appareils, dans les deux sens

```
  PC1 ──(RELEASE demandé)──► PC2 ──(RELEASE demandé)──► téléphone ──► PC1
   ▲                                                                   │
   └── n'importe quel appareil peut prendre les écouteurs à n'importe quel autre
```

```bash
# sur chaque PC : configurer les pairs (résolution BlueZ par leur nom)
blueco peers add pc2 --bt-name "BUREAU-DAMIEN"
blueco peers add telephone --bt-name "Galaxy"
blueco peers                      # état : adresses résolues, canal, mode

# prendre les écouteurs à qui les détient (le téléphone par exemple)
blueco take                       # interroge tous les pairs, libère le détenteur, se connecte
blueco take --from telephone      # ou ne s'adresse qu'à un appareil précis
blueco give --to telephone        # rendre les écouteurs au téléphone
blueco status --all               # qui détient quoi, en ce moment
```

## Démarrage rapide

### Une seule commande

Branche le téléphone en USB (débogage USB activé) et lance :

```bash
./scripts/setup-blueco.sh --test
```

Il fait tout : dépendances, compilation et installation de l'appli, réglages de
l'appli, appairage du téléphone au PC, configuration du PC, installation du
lanceur et du service, puis vérifie la liaison et, avec `--test`, une vraie
bascule dans les deux sens. Le téléphone ne demande qu'un geste, une fois, s'il
n'est pas encore appairé : toucher « Associer » quand le PC apparaît.

Options utiles : `--no-build` (ne pas recompiler), `--power` (autoriser sudo
pour installer les paquets Arch manquants), `--dry-run` (montrer sans rien faire).

### À la main, étape par étape

Sur le PC Linux (Arch) :

```bash
./scripts/build-linux.sh --deps      # python-dbus, python-gobject, bluez, bluez-utils
./scripts/build-linux.sh             # lanceur ~/.local/bin/blueco + unités systemd
./scripts/build-linux.sh --systemd   # (option) service utilisateur au démarrage
```

Puis l'appairage, la configuration et le lancement :

```bash
bluetoothctl         # scan on / pair <MAC> / trust <MAC> / scan off
blueco devices       # liste les adresses connues de BlueZ
blueco init --peer-mac <MAC_TELEPHONE> --headphones-mac <MAC_CASQUE>
blueco serve -v      # démarre le démon (serveur SPP)
```

Détail complet, cas particuliers et dépannage : [`docs/LINUX-SETUP.md`](docs/LINUX-SETUP.md).

Sur le téléphone Android :

```bash
./scripts/build-android.sh --adb     # compile l'APK et l'installe
```

Il n'y a **rien à configurer dans l'appli** : elle retrouve toute seule le PC et
les écouteurs parmi les appareils déjà appairés, et un seul bouton suffit à
basculer. Elle se relance d'elle-même au démarrage du téléphone. Voir
[`android/README.md`](android/README.md) et [`docs/ANDROID.md`](docs/ANDROID.md).

### Tester sans matériel Bluetooth

```bash
make test        # tests unitaires + intégration (socket locale)
make selftest    # scénario complet de bascule, backend audio simulé
```

## Ligne de commande (PC)

| Commande | Effet |
| --- | --- |
| `blueco serve` | démarre le démon : écoute les ordres des autres appareils |
| `blueco take [--from X]` | **je prends les écouteurs** : le détenteur les libère, je me connecte |
| `blueco give --to X` | **je rends les écouteurs** : je libère, l'appareil X se connecte |
| `blueco status [--all]` | état local du casque + qui détient quoi chez les pairs |
| `blueco peers [add|rm]` | liste les pairs (adresses résolues) ou les modifie |
| `blueco release [--local] [--from X]` | demander à un (ou tous les) pair(s) de libérer le casque |
| `blueco grab [--remote] [--to X]` | connecter le casque localement (ou demander à un pair) |
| `blueco ping` | test de liaison avec un pair |
| `blueco watch` | verrouille la session quand le téléphone s'éloigne |
| `blueco devices` | liste les appareils connus de BlueZ |
| `blueco init` | écrit `~/.config/blueco/config.json` |
| `blueco selftest` | auto-test bout-en-bout sans Bluetooth |

Options globales : `--config`, `--transport profile1|rfcomm|unix`, `--fake`
(backend simulé), `--json`, `-v` / `-vv`. `blueco` est aussi utilisable sans
installation : `python3 -m blueco …` depuis la racine du dépôt.

## Configuration

`~/.config/blueco/config.json` (droits `600`), voir
[`packaging/config.example.json`](packaging/config.example.json) :

```json
{
  "device_name": "pc",
  "token": "",
  "headphones": { "mac": "AA:BB:CC:DD:EE:FF", "name": "" },
  "peers": [
    { "name": "pc2",       "bluetooth_name": "BUREAU-DAMIEN", "mode": "rfcomm",   "channel": 3 },
    { "name": "téléphone", "bluetooth_name": "Galaxy",        "mode": "profile1", "channel": 3 }
  ],
  "transport":  { "mode": "profile1", "channel": 3, "require_authentication": true },
  "audio_backend": "bluez",
  "request_timeout_s": 15.0,
  "lock_command": "loginctl lock-session"
}
```

| Clé | Rôle |
| --- | --- |
| `peers[].name` | étiquette utilisée dans les commandes (`take --from bureau`) |
| `peers[].bluetooth_name` | nom de l'appareil dans BlueZ, si différent de l'étiquette |
| `peers[].mac` | adresse figée (facultatif : sinon résolue par BlueZ à l'exécution) |
| `peers[].mode` / `channel` | comment joindre ce pair (`rfcomm` entre PC, `profile1` pour un PC joignable par le téléphone) |
| `peers[].token` | jeton propre à ce pair (sinon celui de la configuration) |
| `headphones.mac` | adresse des écouteurs à basculer (ou `headphones.name`) |
| `token` | secret partagé optionnel ; toute requête entrante doit le porter |
| `transport.mode` | `profile1` (recommandé), `rfcomm` (canal fixe), `unix` (diagnostic) |
| `audio_backend` | `bluez` ou `fake` (aucune action réelle) |
| `lock_command` | commande exécutée par `blueco watch` à l'éloignement |

> L'ancienne clé `peer` (au singulier) reste acceptée et équivaut à un `peers`
> d'un seul élément.

## Protocole

Une trame = un objet JSON par ligne. Requêtes (`cmd`), réponses (`ok`),
événements (`evt`) circulent dans les deux sens. Spécification complète et
exemples : [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

```json
{"v":1,"id":"9f3c1a7b22d0","cmd":"RELEASE"}
{"v":1,"id":"9f3c1a7b22d0","ok":true,"result":{"released":"AA:BB:CC:DD:EE:FF","was_connected":true}}
```

## Structure du dépôt

```
blueco/            démon Linux (Python) : protocole, transports, serveur, client
tests/             tests unitaires et d'intégration (unittest, 144 tests)
android/           appli Android (Kotlin, sans dépendance externe)
packaging/         unités systemd, PKGBUILD Arch, sudoers facultatif, config exemple
scripts/           setup-blueco.sh (tout-en-un), builds Linux et Android
docs/              architecture, protocole, installation Linux, Android
LICENSE            licence MIT
```

## Ce qui est vérifié, ce qui ne l'est pas

Vérifié en conditions réelles (PC Linux + Samsung SM-A175F sous Android 16,
BlueZ 5.87) :

* `python3 -m unittest discover -s tests -t .` → **144 tests OK** ;
* `python3 -m blueco --fake selftest` → **14/14 vérifications** ;
* **la bascule complète, dans les deux sens** : `blueco take` fait lâcher le
  téléphone et le PC prend les écouteurs (`Connected: yes` côté BlueZ, sortie
  A2DP active), `blueco give --to "…"` les lui rend ; l'état tient dans le temps
  (vérifié 100 s après la bascule) ;
* **la liaison SPP réelle** : le téléphone ouvre le lien tout seul, se reconnecte
  après une coupure, et le PC répond aux ordres ;
* **l'appli Android** : compilée, installée par USB, configurée à distance, et
  le gros bouton bascule dans les deux sens.

Ce que ce dépôt ne peut pas prouver à ta place :

* le comportement sur un autre couple téléphone/adaptateur (versions d'Android
  et de BlueZ différentes) ;
* la reprise après une coupure du casque lui-même (il doit accepter la
  connexion entrante du PC : c'est lui qui décide, et certains modèles très
  bon marché n'acceptent qu'un appareil à la fois) ;
* le verrouillage de session par proximité (`blueco watch`) : il dépend de
  `loginctl` et de la présence de `hcitool` pour le RSSI.

## Sécurité et limites

* Le canal RFCOMM est authentifié par l'appairage Bluetooth, mais un appareil
  appairé peut parler à tout processus à l'écoute : si vous voulez séparer les
  usages, définissez `token` (comparaison en temps constant) — la socket unix est
  en `0600`.
* Android n'expose pas d'API publique pour (dé)connecter A2DP/HFP : l'appli
  passe par le proxy de profil public puis par réflexion (détails et solutions de
  repli dans [`docs/ANDROID.md`](docs/ANDROID.md)).
* Le RSSI en Bluetooth classique n'est pas exposé par BlueZ : `blueco watch`
  utilise la **présence du lien** (ping périodique) et, si `hcitool` est présent,
  le RSSI en complément. Voir [`docs/LINUX-SETUP.md`](docs/LINUX-SETUP.md).
* Beaucoup d'écouteurs se reconnectent automatiquement à leur dernier appareil :
  si PipeWire/wireplumber ou le téléphone « reprennent » le casque, c'est un
  comportement des piles audio, pas du lien blueco (pistes dans la doc Linux).

## Licence

MIT — voir [`LICENSE`](LICENSE). Le glyphe « bluetooth » de l'icône Android
provient du jeu d'icônes Material Design (Apache-2.0).
