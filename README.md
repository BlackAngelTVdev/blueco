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

1. Le PC Linux et le téléphone sont appairés en Bluetooth classique.
2. Le démon Linux enregistre un profil **SPP** auprès de BlueZ
   (`org.bluez.ProfileManager1`) : BlueZ publie l'enregistrement SDP et alloue
   le canal RFCOMM. L'appli Android s'y connecte par UUID, avec l'API publique
   `createRfcommSocketToServiceRecord()`.
3. Un protocole JSON minimal circule dans les deux sens sur cette connexion.
4. Bascule **depuis le téléphone** : le téléphone envoie `RELEASE` au PC → le PC
   déconnecte les écouteurs → le téléphone les connecte.
5. Bascule **depuis le PC** : `blueco take` pousse un `RELEASE` au téléphone sur
   la connexion déjà ouverte → le téléphone lâche les écouteurs → le PC les
   connecte. Si le téléphone n'est pas joignable, l'ordre est mis en file
   d'attente et délivré à la reconnexion.

Détail de conception et justification des choix : [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Démarrage rapide

### 1. PC Linux (Arch Linux)

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

### 2. Téléphone Android

```bash
./scripts/build-android.sh --adb     # compile l'APK et l'installe
```

Dans l'appli : renseignez l'adresse MAC du PC (« Choisir le PC dans la liste »),
puis « Démarrer le service ». Voir [`android/README.md`](android/README.md) et
[`docs/ANDROID.md`](docs/ANDROID.md).

### 3. Tester sans matériel Bluetooth

```bash
make test        # 94 tests unitaires + intégration (socket locale)
make selftest    # scénario complet de bascule, backend audio simulé
```

## Ligne de commande (PC)

| Commande | Effet |
| --- | --- |
| `blueco serve` | démarre le démon : écoute les ordres du téléphone |
| `blueco status [--remote]` | état local du casque, et état du téléphone avec `--remote` |
| `blueco take` | **je prends les écouteurs** : le téléphone libère, je me connecte |
| `blueco give` | **je rends les écouteurs** : je libère, le téléphone se connecte |
| `blueco release [--local]` | demander au pair de libérer le casque (ou libérer localement) |
| `blueco grab [--remote]` | connecter le casque localement (ou demander au pair) |
| `blueco ping` | test de liaison avec le téléphone |
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
  "peer":       { "mac": "11:22:33:44:55:66", "name": "" },
  "transport":  { "mode": "profile1", "channel": 3, "require_authentication": true },
  "audio_backend": "bluez",
  "request_timeout_s": 15.0,
  "lock_command": "loginctl lock-session"
}
```

| Clé | Rôle |
| --- | --- |
| `peer.mac` | adresse du téléphone Android |
| `headphones.mac` | adresse des écouteurs à basculer (ou `headphones.name`) |
| `token` | secret partagé optionnel ; toute requête entrante doit le porter |
| `transport.mode` | `profile1` (recommandé), `rfcomm` (canal fixe), `unix` (diagnostic) |
| `audio_backend` | `bluez` ou `fake` (aucune action réelle) |
| `lock_command` | commande exécutée par `blueco watch` à l'éloignement |

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
tests/             tests unitaires et d'intégration (unittest, 94 tests)
android/           appli Android (Kotlin, sans dépendance externe)
packaging/         unités systemd, PKGBUILD Arch, sudoers facultatif, config exemple
scripts/           scripts de build (Linux Arch et Android)
docs/              architecture, protocole, installation Linux, Android
LICENSE            licence MIT
```

## Ce qui est vérifié, ce qui ne l'est pas

Vérifié ici, sur ce dépôt :

* `python3 -m unittest discover -s tests -t .` → **94 tests OK** ;
* `python3 -m blueco --fake selftest` → **14/14 vérifications** : ping, état,
  libération, reprise, poussée d'ordre vers le pair, file d'attente, jeton,
  commande inconnue, panne du backend audio.

Non vérifiable dans cet environnement (à valider sur votre matériel) :

* le trajet Bluetooth réel (aucun adaptateur appairé ni `bluetoothd` actif ici) ;
* l'enregistrement du profil SPP par BlueZ (le mode `rfcomm` et l'échappement
  `transport.service_record` sont prévus si BlueZ le refuse) ;
* la compilation de l'APK (aucun JDK ni SDK Android sur cette machine).

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
