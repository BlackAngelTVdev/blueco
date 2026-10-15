# Protocole blueco v1

Référence normative : `blueco/protocol.py` (Python) et
`android/app/src/main/java/dev/blueco/app/Protocol.kt` (Kotlin). Les deux
implémentations doivent rester alignées.

## Trame

* Un objet JSON par ligne, encodé en UTF-8, terminé par `\n`.
* Taille maximale **2048 octets** (au-delà : trame refusée, connexion fermée).
* Aucun champ superfétatoire n'est exigé, mais `v` et `id` le sont.

Le protocole est **symétrique** : les deux extrémités peuvent envoyer des
requêtes et des réponses sur la même connexion.

### Requête

```json
{"v":1,"id":"9f3c1a7b22d0","cmd":"RELEASE","args":{"mac":"AA:BB:CC:DD:EE:FF"},"token":"secret"}
```

| Champ | Type | Rôle |
| --- | --- | --- |
| `v` | entier | version du protocole (actuellement `1`) |
| `id` | chaîne (12 hex) | identifiant unique de la requête, repris tel quel dans la réponse |
| `cmd` | chaîne | `PING`, `STATUS`, `RELEASE`, `GRAB`, `WHOAMI` (insensible à la casse) |
| `args` | objet | paramètres optionnels |
| `token` | chaîne | obligatoire **uniquement si** le pair a configuré un jeton |
| `from` | chaîne | informationnel (`"pc"`, `"téléphone"`) |

### Réponse

```json
{"v":1,"id":"9f3c1a7b22d0","ok":true,"result":{"released":"AA:BB:CC:DD:EE:FF","was_connected":true,"connected":false,"already_free":false}}
```

```json
{"v":1,"id":"9f3c1a7b22d0","ok":false,"error":{"code":"NO_DEVICE","message":"casque introuvable"}}
```

### Événement (non sollicité, sans `id`)

```json
{"v":1,"evt":"STATE","data":{"headphones_held":false}}
{"v":1,"evt":"HELLO","data":{"device":"téléphone","protocol":1}}
```

Une implémentation qui reçoit une trame illisible **ne répond pas** : sans `id`
fiable, une réponse d'erreur créerait une boucle entre deux implémentations.

## Commandes

### `PING`

Test de liaison. `args` ignoré.

```json
→ {"v":1,"id":"a1","cmd":"PING"}
← {"v":1,"id":"a1","ok":true,"result":{"pong":true,"device":"pc","protocol":1}}
```

### `WHOAMI`

Identité de l'extrémité distante, sans effet de bord.

```json
← {"v":1,"id":"a2","ok":true,"result":{"device":"pc","protocol":1,"adapter":{"address":"11:22:33:44:55:66","name":"hci0","powered":true}}}
```

### `STATUS`

État complet. Aucun effet de bord.

```json
← {"v":1,"id":"a3","ok":true,"result":{
     "device":"pc",
     "protocol":1,
     "audio":{"found":true,"connected":true,"mac":"AA:BB:CC:DD:EE:FF","name":"Mes Écouteurs","backend":"bluez"},
     "headphones_held":true,
     "uptime_s":42.5,
     "listener":"profile1:00001101-0000-1000-8000-00805f9b34fb",
     "peers":1,
     "queued":0,
     "history":[{"event":"release","released":"AA:BB:CC:DD:EE:FF","was_connected":true,"at":"21:04:03"}],
     "adapter":{"address":"11:22:33:44:55:66","name":"hci0","powered":true}}}
```

Côté Android, `result` contient `{"device","protocol","audio","peer"}`.

### `RELEASE` — « libère les écouteurs »

Déconnecte le casque **de l'appareil qui reçoit la commande**.

```json
→ {"v":1,"id":"b1","cmd":"RELEASE"}
← {"v":1,"id":"b1","ok":true,"result":{"released":"AA:BB:CC:DD:EE:FF","name":"Mes Écouteurs","was_connected":true,"connected":false,"already_free":false}}
```

Idempotent : si le casque est déjà déconnecté, `ok:true` avec
`"already_free":true`. Un `RELEASE` ne fait **pas** que l'appareil récepteur
abandonne le casque pour toujours ; il rend simplement la ressource libre.

### `GRAB` — « reprends les écouteurs »

Reconnecte le casque à l'appareil qui reçoit la commande.

```json
→ {"v":1,"id":"b2","cmd":"GRAB"}
← {"v":1,"id":"b2","ok":true,"result":{"connected":true,"mac":"AA:BB:CC:DD:EE:FF","already_connected":false}}
```

## Codes d'erreur

| Code | Sens | Côté |
| --- | --- | --- |
| `BAD_FRAME` | JSON invalide, `id` absent, trame trop longue, version inconnue | les deux |
| `UNSUPPORTED_COMMAND` | `cmd` inconnue de l'implémentation | les deux |
| `UNAUTHORIZED` | `token` absent ou incorrect | les deux |
| `NO_DEVICE` | casque introuvable dans BlueZ (Linux) | Linux |
| `AUDIO_UNAVAILABLE` | BlueZ ou Android a refusé l'opération audio | les deux |
| `NOT_CONNECTED` | socket fermée / session terminée | les deux |
| `TIMEOUT` | pas de réponse dans le délai | client |
| `BUSY` | réservé (opération concurrente) | — |
| `INTERNAL` | erreur inattendue, message joint pour le diagnostic | les deux |

## Flux de bascule complet

```
1.  téléphone → PC   RELEASE
2.  PC → téléphone   ok {released, was_connected}
3.  téléphone        proxy A2DP connect() sur ses propres écouteurs
    (le PC, de son côté : Device1.Connect() vers le casque)
```

Depuis le PC, l'étape 1 passe par la connexion déjà ouverte par le téléphone
(aucune connexion entrante vers Android n'est nécessaire).

## Délais et robustesse

| Paramètre | Valeur | Où |
| --- | --- | --- |
| Délai de réponse par défaut | 15 s (`request_timeout_s`) | `config.json` |
| Attente côté Android avant de connecter ses écouteurs | 20 s | `BluecoService.takeHeadphones()` |
| Reconnexion Android | backoff de `retry_ms` (5 s) à 60 s | `Prefs.retry_ms` |
| Cycle de lecture | interruptions de 1 s pour rester réactif à la fermeture | `session.py`, `Session.kt` |
| Trames invalides tolérées | 5 puis fermeture | `session.py`, `Session.kt` |

## Versionnement

`v` est vérifié à la lecture : une trame d'une autre version est refusée en
`BAD_FRAME`. Toute évolution incompatible incrémente `v` ; les ajouts de
commandes sont signalés par `UNSUPPORTED_COMMAND` et n'interrompent pas la
connexion.

## Exemple minimal en shell

Avec le transport `unix`, sans matériel Bluetooth :

```bash
python3 -m blueco --fake --transport unix --socket /tmp/blueco.sock serve &
python3 -m blueco --fake --transport unix --socket /tmp/blueco.sock status
python3 -m blueco --fake --transport unix --socket /tmp/blueco.sock release --local
```
