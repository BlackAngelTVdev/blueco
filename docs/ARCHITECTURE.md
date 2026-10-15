# blueco — architecture et plan de réalisation

Ce document est le plan détaillé du projet : objectif, contraintes, choix
techniques (avec ce qui a été écarté et pourquoi), découpage des composants,
séquences, gestion d'erreurs, sécurité, feuille de route et critères
d'acceptation.

## 1. Objectif et contraintes

**Objectif.** Basculer des écouteurs Bluetooth entre un PC Linux et un
téléphone Android en un clic, depuis l'un ou l'autre appareil.

**Contraintes imposées (extraits de `project.md`) :**

| # | Contrainte | Traduction dans la conception |
| --- | --- | --- |
| C1 | Uniquement du Bluetooth | Aucun socket réseau, aucun serveur tiers, aucun cloud. Toutes les communications passent par RFCOMM/SPP sur le lien appairé. |
| C2 | Appareils appairés en Bluetooth classique | L'authentification est celle de l'appairage ; pas d'échange de clé supplémentaire obligatoire. |
| C3 | Canal direct pour des ordres courts | Protocole à trames courtes (JSON d'une ligne, ≤ 2 ko), pas de flux binaire. |
| C4 | Un côté reçoit l'ordre et libère les écouteurs | Le démon se déconnecte via BlueZ (`Device1.Disconnect`) ou `bluetoothctl`. |
| C5 | L'appareil demandeur se connecte ensuite | Reprise locale (`Device1.Connect` côté Linux, proxy A2DP côté Android). |
| C6 | Option : verrouillage de session sur RSSI | Implémenté comme *surveillance de présence* du lien, avec lecture RSSI optionnelle si l'outil existe (voir §8). |
| C7 | Minimaliste et multiplateforme | Python 3 sans dépendance exotique côté Linux ; Kotlin sans aucune dépendance tierce côté Android. |

## 2. Choix de transport : RFCOMM/SPP plutôt que BLE

| Critère | RFCOMM/SPP (retenu) | BLE GATT (écarté) |
| --- | --- | --- |
| Rôle serveur côté Android | inutile (le PC est le serveur) | périphérique GATT : `BluetoothGattServer` + advertising |
| Rôle serveur côté Linux | supporté par BlueZ (`ProfileManager1`) | nécessite un serveur GATT D-Bus (`bless`, bluetoothd expérimental) |
| Débit | suffisant (ordres courts) | suffisant |
| Complexité | faible : un socket stream | élevée : services, caractéristiques, notifications, MTU, ré-appairage BLE |
| Maturité des outils Python | `AF_BLUETOOTH` + `BTPROTO_RFCOMM` en stdlib, D-Bus pour le profil | dépendances supplémentaires |

**Conclusion.** RFCOMM/SPP sur le lien Bluetooth classique déjà appairé. BLE est
réservé à un usage futur où l'on voudrait joindre le téléphone sans appairage
classique.

### 2.1 Qui est serveur, qui est client — et pourquoi

Le point délicat est la **résolution du canal RFCOMM**. Un client RFCOMM doit
connaître le canal du serveur ; ce canal est publié dans l'enregistrement SDP.

* Côté Linux, les outils historiques (`sdptool`, `rfcomm`) ne sont **plus livrés**
  par BlueZ 5 sur Arch (`bluez-utils` ne les fournit pas) : on ne peut donc pas
  compter sur eux pour retrouver le canal d'un serveur Android.
* Côté Android, l'API publique `createRfcommSocketToServiceRecord(UUID)` fait la
  résolution SDP à notre place — mais uniquement si le serveur publie un
  enregistrement SDP.

D'où la conception retenue :

```
   Android  ──── RFCOMM client, résolution SDP par UUID ────►  Linux
                                                                serveur SPP
                                                     (BlueZ publie l'enregistrement)
```

Enregistrer un profil SPP auprès de BlueZ est exactement ce que fait
`ProfileManager1.RegisterProfile` : BlueZ crée l'enregistrement SDP, alloue (ou
accepte) le canal RFCOMM, et **remet à notre processus un descripteur de fichier
déjà connecté** à chaque connexion entrante (`NewConnection` sur D-Bus). Aucun
canal en dur, aucune dépendance à `rfcomm`/`sdptool`.

La connexion étant **full-duplex**, l'appli Android est cliente du point de vue
de l'établissement, mais le protocole est symétrique : le PC peut lui envoyer
des requêtes sur la même connexion. C'est ce qui rend la bascule « depuis le PC »
possible sans second canal.

### 2.2 Autres options écartées

| Option | Pourquoi écartée |
| --- | --- |
| `rfcomm bind` + tty série | outil absent de BlueZ 5 sur Arch ; ne sert qu'aux flux « port série ». |
| SDP écrit à la main en Python (client SDP sur L2CAP) | faisable, mais inutile dès lors que le lien est établi par Android, et impossible à tester ici. |
| Passer par le Wi-Fi / `adb` / SSH | interdit par C1. |
| BLE Advertising + GATT | complexité disproportionnée pour un cas appairé (voir §2). |

## 3. Vue d'ensemble des composants

```
                       blueco/  (démon Linux, Python)
   ┌────────────────────────────────────────────────────────────────┐
   │ cli.py        serve | take | give | release | grab | status …  │
   │ server.py     écoute, dispatch PING/STATUS/RELEASE/GRAB, file  │
   │ session.py    requêtes/réponses bidirectionnelles, événements  │
   │ transport.py  unix | rfcomm | profile1 (SPP via BlueZ)         │
   │ bluez.py      D-Bus org.bluez (Device1, Adapter1, ProfileMgr)  │
   │ audio.py      AudioSink : release()/grab()  (bluez | fake)     │
   │ watch.py      présence du pair → verrouillage de session       │
   │ config.py     ~/.config/blueco/config.json                     │
   │ protocol.py   trames JSON d'une ligne (référence normative)     │
   └────────────────────────────────────────────────────────────────┘

                       android/app/src/main/java/dev/blueco/app/
   ┌────────────────────────────────────────────────────────────────┐
   │ BluecoService  service 1er plan : lien + exécution des ordres  │
   │ PeerLink       client RFCOMM, reconnexion avec backoff         │
   │ Session        requêtes/réponses + événements                  │
   │ AudioController A2DP/HFP connect/disconnect (réflexion)        │
   │ Protocol       codec, miroir de protocol.py                    │
   │ MainActivity   réglages, état, boutons, notification           │
   └────────────────────────────────────────────────────────────────┘
```

Rôle de chaque couche Python :

| Module | Responsabilité | Ne fait pas |
| --- | --- | --- |
| `protocol.py` | encodage/décodage, validations, codes d'erreur, lecture de lignes | aucune E/S métier |
| `config.py` | schéma de configuration, validation, fichiers | aucune action Bluetooth |
| `bluez.py` | appareils, connect/disconnect, info adaptateur (D-Bus, repli `bluetoothctl`) | aucune logique de bascule |
| `audio.py` | « libère le casque » / « reprends le casque » | pas de transport |
| `transport.py` | écouteurs/connexions (`unix`, `rfcomm`, `profile1`) | pas de protocole |
| `session.py` | corrélation requête/réponse, événements, fermeture propre | pas de politique |
| `server.py` | dispatch des commandes, file d'attente, historique | pas d'E/S brutes |
| `client.py` | appels ponctuels et session persistante vers le pair | pas de serveur |
| `watch.py` | présence/RSSI → commande de verrouillage | pas de bascule audio |
| `cli.py` | interface utilisateur, sorties lisibles ou JSON | pas de logique métier |

## 4. Séquences

### 4.1 Bascule depuis le téléphone (scénario principal, `project.md` §3-4)

```
Téléphone            PC Linux                Écouteurs
   │                    │                        │
   │ bouton "Prendre"   │                        │
   │─RELEASE───────────►│                        │
   │                    │ Device1.Disconnect() ─►│   (déconnexion A2DP/HFP)
   │                    │◄── confirmed ──────────│
   │◄──ok {released}────│                        │
   │                    │                        │
   │ proxy A2DP connect ────────────────────────►│
   │◄── connecté ───────────────────────────────│
   │  (la notification affiche "écouteurs connectés")
```

Points d'attention implémentés :

* le téléphone **attend la confirmation** du PC avant de connecter ses
  écouteurs (sinon les deux appareils se disputent la connexion) ;
* un `RELEASE` sur un casque déjà libre est un succès idempotent
  (`already_free: true`) : deux clics rapides ne provoquent pas d'erreur ;
* un échec de l'étape locale est rapporté distinctement de l'échec de l'étape
  distante.

### 4.2 Bascule depuis le PC

```
PC Linux                          Téléphone
   │                                   │
   │◄──────── connexion SPP ───────────│   (l'appli est cliente)
   │                                   │
   │─RELEASE (sur la session ouverte)─►│   → le téléphone déconnecte ses écouteurs
   │◄──────── ok ──────────────────────│
   │ Device1.Connect() vers le casque  │
```

Si le téléphone n'est pas joignable : la commande est **mise en file d'attente**
et délivrée dès la reconnexion (`server.py::push(..., allow_queue=True)`).

### 4.3 Établissement et perte du lien (Android)

`PeerLink` boucle : connexion → session → attente de fermeture → nouvelle
tentative, avec backoff exponentiel de `retry_ms` à 60 s, remis à zéro après un
succès. La session s'annonce par un événement `HELLO`. À chaque changement
d'état audio, le serveur émet un événement `STATE` (notification mise à jour,
état affiché dans l'appli).

## 5. Machine à états du lien (Android)

```
 IDLE ──(MAC renseignée)──► CONNECTING ──(socket ok)──► CONNECTED
   ▲                             │                          │
   │                          (échec)                    (fermeture,
   │                             ▼                       erreur socket)
   │◄──(stop)──── WAITING ◄──────┘◄──────────────────────────┘
                    │
              (backoff écoulé) ──► CONNECTING
```

Aucun état partagé mutable hors `@Volatile` : le service peut être détruit à
tout moment, `stop()` ferme la socket ce qui débloque la lecture.

## 6. Gestion d'erreurs

| Situation | Comportement |
| --- | --- |
| Casque introuvable dans BlueZ | `NO_DEVICE`, message indiquant la cible configurée |
| BlueZ refuse connect/disconnect | `AUDIO_UNAVAILABLE` + message BlueZ brut |
| Pair injoignable | `TransportError` côté client, code retour 1, message clair |
| Réponse absente | délai `request_timeout_s` (15 s) puis `TIMEOUT` |
| Trame illisible | journalisée et ignorée : **aucune** réponse d'erreur n'est renvoyée (sinon deux implémentations se répondraient à l'infini) ; au-delà de 5 trames invalides la connexion est fermée |
| Trame trop longue | refusée dès la lecture (> 2 ko), tampon vidé, connexion fermée |
| Jeton invalide | `UNAUTHORIZED`, journalisé, aucune action |
| Panne du backend audio | `AUDIO_UNAVAILABLE`, historique mis à jour, aucune exception non gérée |

Journalisation : `-v` (info) puis `-vv` (debug) côté Linux ; côté Android, un
anneau de 200 lignes consultable dans l'appli (`AppLog`).

## 7. Sécurité

* **Authentification** : appairage Bluetooth (SSP). Le profil SPP est enregistré
  avec `RequireAuthentication: true`.
* **Jeton partagé optionnel** (`token`) : si défini, chaque requête entrante doit
  le porter ; comparaison à temps constant (`hmac.compare_digest`). Utile si
  d'autres services SPP écoutent sur la même machine.
* **Périmètre minimal** : le démon n'écoute que le profil SPP déclaré, n'ouvre
  aucune socket réseau ; en mode `unix` la socket est en `0600` dans
  `$XDG_RUNTIME_DIR`.
* **Pas de privilèges** : aucune opération ne requiert root. La (dé)connexion du
  casque passe par D-Bus : c'est `bluetoothd` (qui tourne déjà en root) qui agit.
  `packaging/blueco.sudoers` est fourni **commenté**, à n'activer que si vous
  choisissez une commande de verrouillage exigeant root.

## 8. Limites connues et réponses apportées

| Limite | Réponse |
| --- | --- |
| Android n'expose pas d'API publique pour (dé)connecter A2DP/HFP | `AudioController` : proxy de profil public + réflexion sur `connect`/`disconnect` ; échec explicite sinon ; alternative Shizuku/root documentée |
| BlueZ peut refuser l'enregistrement d'un profil SPP sans `ServiceRecord` | option `transport.service_record` (XML SDP) et repli `transport.mode = "rfcomm"` |
| RSSI absent en Bluetooth classique (pas de `hcitool` sur Arch) | `blueco watch` se base sur la **présence du lien** (ping), le RSSI n'est utilisé que s'il est disponible |
| Reconnexion automatique des écouteurs par PipeWire/wireplumber | documenté dans `docs/LINUX-SETUP.md` (qui est source de la reprise) |
| Écouteurs à connexion unique (pas de multipoint) | c'est le cas d'usage cible : le « handoff » libère puis reprise |

## 9. Feuille de route et critères d'acceptation

| Phase | Contenu | Critère d'acceptation | État |
| --- | --- | --- | --- |
| P0 | Protocole, configuration, backends audio, transports, serveur, client, CLI, tests | `make test` + `make selftest` verts **sans matériel** | ✅ 94 tests + 14/14 |
| P1 | Appli Android (service, lien, audio, UI) | APK de debug compilé, service qui se reconnecte, bouton « Prendre » complet | ✅ code livré, ⏳ compilation à faire sur votre machine |
| P2 | Validation matérielle : appairage, bout-en-bout réel, latence | bascule complète en < 5 s, aucune intervention manuelle | ⏳ à valider chez vous |
| P3 | Option : verrouillage de session par éloignement | session verrouillée après 3 échecs consécutifs, déverrouillage au retour | ✅ implémenté (`watch`), ⏳ réglage du seuil chez vous |
| P4 | Confort : latence d'éviction, reprise automatique des écouteurs | bascule en un seul clic quel que soit le côté initiateur | ⏳ |

## 10. Vérification

```bash
python3 -m unittest discover -s tests -t .   # 94 tests (protocole, config, BlueZ,
                                             # audio, serveur, intégration unix, veilleur)
python3 -m blueco --fake selftest            # 14 vérifications bout-en-bout
```

Le transport `unix` emprunte **exactement** le même chemin de code que le
transport RFCOMM (mêmes trames, même session, même dispatch) : seuls le socket
et le profil changent. C'est ce qui permet de valider toute la logique sans
adaptateur Bluetooth, et ce que le démon utilise en tests.
