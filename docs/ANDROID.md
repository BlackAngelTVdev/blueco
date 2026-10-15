# Android — guide détaillé

L'appli est le pendant du démon Linux : elle ouvre la connexion Bluetooth,
exécute les ordres reçus et déclenche la bascule depuis le téléphone.

## 1. Compilation

```bash
./scripts/build-android.sh                       # APK de debug → dist/
./scripts/build-android.sh --release             # APK de release
./scripts/build-android.sh --adb                 # compile puis installe
./scripts/build-android.sh --install-sdk         # installe platforms/build-tools manquants
```

Ce que fait le script :

1. localise un **JDK 17+** (`JAVA_HOME`, sinon `java` du `PATH`) ;
2. localise le **SDK** (`ANDROID_SDK_ROOT`, `ANDROID_HOME`, `~/Android/Sdk`,
   `/opt/android-sdk`, `/usr/lib/android-sdk`) et écrit `android/local.properties` ;
3. vérifie `platforms;android-35` et `build-tools;35.0.0` (installe avec
   `--install-sdk` si `sdkmanager` est présent) ;
4. utilise `gradlew`, sinon `gradle`, sinon **télécharge Gradle 8.9** dans
   `~/.cache/blueco/` ;
5. compile et copie l'APK dans `dist/blueco-<version>-<type>.apk`.

Sans JDK ni SDK :

```bash
sudo pacman -S jdk17-openjdk
# SDK via les command-line-tools officiels :
mkdir -p ~/Android/Sdk/cmdline-tools && cd ~/Android/Sdk/cmdline-tools
unzip commandlinetools-linux-*.zip && mv cmdline-tools latest
export ANDROID_SDK_ROOT=~/Android/Sdk
sdkmanager "platform-tools" "platforms;android-35" "build-tools;35.0.0"
```

Android Studio : ouvrez `android/` et lancez `assembleDebug`.

## 2. Internes

```
MainActivity  ──(réglages, boutons)──►  BluecoService (premier plan)
                                             │
                                   ┌─────────┴──────────┐
                                   ▼                    ▼
                              PeerLink             AudioController
                          (socket RFCOMM,       (A2DP/HFP via
                           reconnexion)          proxy de profil)
                                   │
                                   ▼
                              Session (Protocol)
```

* **`BluecoService`** reste en avant-plan (notification permanente, actions
  « Prendre » / « Rendre ») : c'est la seule façon fiable de garder une socket
  ouverte avec l'écran éteint sur les versions récentes d'Android.
* **`PeerLink`** est un client RFCOMM : `createRfcommSocketToServiceRecord()`
  avec l'UUID SPP, reconnexion automatique avec backoff.
* **`Session`** est le miroir de `blueco/session.py` : corrélation
  `id` → réponse, exécution des requêtes entrantes sur un pool de 2 fils.
* **`AudioController`** libère/reprend les écouteurs (voir §3).

## 3. Le point délicat : (dé)connecter l'audio

Android **ne fournit aucune API publique** pour connecter ou déconnecter un
profil audio : `BluetoothA2dp.connect()`, `BluetoothA2dp.disconnect()`,
`BluetoothHeadset.connect()`, `BluetoothHeadset.disconnect()` sont `@hide`
(et `@RequiresPermission(BLUETOOTH_PRIVILEGED)` pour certaines ROM).

Stratégie implémentée :

1. obtenir le **proxy de profil** par l'API **publique**
   `BluetoothAdapter.getProfileProxy()` sur `A2DP` (et `HEADSET` en secours) ;
2. appeler `connect`/`disconnect` par **réflexion** sur ce proxy ;
3. si `NoSuchMethodException`, `IllegalAccessException` ou
   `InvocationTargetException` survient (politique d'API masquée de la ROM),
   renvoyer une erreur claire :

   ```
   AUDIO_UNAVAILABLE: Android a refusé la déconnexion de AA:… :
   déconnectez les écouteurs depuis les réglages Bluetooth
   ```

### Empêcher la reprise automatique

Après un `disconnect()`, Android **reconnecte** souvent le casque de lui-même
quelques dizaines de secondes plus tard (constaté sur le matériel :
déconnexion à 21:26:15, reconnexion spontanée à 21:26:44). Le remède habituel est
`setConnectionPolicy(device, CONNECTION_POLICY_FORBIDDEN)` avant de déconnecter
— mais sur Android 16 cet appel est refusé aux applications ordinaires :

```
profil BluetoothA2dp: setConnectionPolicy() refusée
  (Neither user … nor current process has android.permission.BLUETOOTH_PRIVILEGED.)
```

L'appli essaie quand même (et le journalise sans échouer), et la bascule
fonctionne parce que **le PC prend les écouteurs tout de suite** : une fois le
casque occupé par le PC, la reprise automatique du téléphone échoue. C'est
pourquoi `blueco take` réessaie la connexion plusieurs fois et que l'appli
attend jusqu'à 30 s la réponse du PC avant de se déclarer en échec.

Reste à savoir : certains casques acceptent plusieurs appareils à la fois
(multipoint) ; ils peuvent alors garder le téléphone connecté malgré tout.

Replis possibles, dans l'ordre de préférence :

| Repli | Comment |
| --- | --- |
| Action manuelle | déconnecter/connecter depuis **Réglages → Bluetooth** (2 gestes) |
| Reconnexion automatique | dans de nombreux cas, Android se reconnecte seul aux derniers écouteurs dès que le PC les libère : poussez alors simplement un `RELEASE` au PC |
| Shizuku | exécute le même appel sous l'utilisateur `shell`, ce qui lève la restriction d'API masquée, sans root |
| Root | `adb shell` / `su -c "cmd bluetooth_manager …"` ou l'API masquée complète |

## 4. Configuration : rien à saisir

L'appli se débrouille seule (`AutoConfig.kt`) : si une adresse manque, elle la
déduit des **appareils déjà appairés** — le PC se reconnaît à sa classe
Bluetooth (`COMPUTER`) et à l'UUID SPP de son cache SDP, les écouteurs à leurs
profils A2DP/HFP ou au fait qu'ils jouent le son en ce moment. Aucun scan n'est
lancé, donc aucune autorisation de localisation n'est requise.

Le script `scripts/setup-blueco.sh` pousse en plus les adresses exactes par USB
(`adb` + `run-as` sur la variante debug), ce qui évite tout tâtonnement.

| Champ | Rôle |
| --- | --- |
| Adresse MAC du PC | appareil qui publie le service SPP (bouton « Choisir le PC dans la liste ») |
| Adresse MAC des écouteurs | cible de la bascule (bouton « Choisir les écouteurs ») |
| Nom de ce téléphone | informationnel, visible côté PC |
| Jeton partagé | doit être **identique** à `token` dans la config Linux |
| Relancer la liaison à l'ouverture | relance le service au démarrage de l'appli |

Ces champs sont **repliés** derrière « Réglages avancés » : en usage normal, un
seul gros bouton suffit, et il affiche toujours où sont les écouteurs (l'appli
interroge le PC en `STATUS` toutes les 2 s pour savoir s'il les détient).

Le service démarre aussi **au démarrage du téléphone** et après une mise à jour
(`BootReceiver`), pour que la bascule déclenchée depuis le PC fonctionne même si
l'appli n'a pas été ouverte depuis le dernier redémarrage.

### Piloter l'appli depuis le PC (variante debug)

La variante debug expose un récepteur qui rejoue les actions du bouton, ce qui
permet à `scripts/setup-blueco.sh` et à `adb` de tester sans toucher l'écran :

```bash
adb shell am broadcast -a dev.blueco.app.GIVE \
  -n dev.blueco.app.debug/dev.blueco.app.TestReceiver
```

Il n'existe **pas** dans l'APK de production (voir `src/debug/AndroidManifest.xml`).

## 5. Scénarios

**Depuis le téléphone** — bouton « Prendre les écouteurs » (ou action dans la
notification) :

1. `RELEASE` au PC, attente de confirmation (20 s) ;
2. connexion locale des écouteurs via le proxy A2DP ;
3. la notification affiche le bilan (`PC a libéré … · écouteurs connectés`).

**Depuis le PC** — `blueco take` envoie `RELEASE` sur la connexion déjà ouverte :
le téléphone déconnecte ses écouteurs (`AudioController.release`), répond `ok`,
et le PC se connecte.

## 6. Dépannage

| Symptôme | Cause probable / action |
| --- | --- |
| « échec: connexion SPP refusée » | le PC n'est pas appairé, ou tourne en mode `rfcomm` (aucun SDP publié) |
| « adresse du PC non configurée » | renseignez la MAC du PC puis « Enregistrer » |
| « adresse MAC invalide » | format `AA:BB:CC:DD:EE:FF` attendu |
| Absence de reconnexion | autorisez blueco à tourner en arrière-plan (batterie sans optimisation) |
| `AUDIO_UNAVAILABLE` | API masquée bloquée : voir §3 |
| Aucun ordre reçu depuis le PC | vérifiez `blueco status --remote` côté PC : s'il répond « injoignable », l'appli n'est pas connectée |
| La notification disparaît | le système a tué le service : `FOREGROUND_SERVICE_CONNECTED_DEVICE` + autorisation de batterie nécessaires |

## 7. Détails d'implémentation

* **Zéro dépendance** : ni AndroidX ni Compose — uniquement la plateforme
  (`org.json` fournit le codec). Le champ `dependencies {}` du module est vide,
  donc rien à télécharger en dehors du SDK et de Gradle.
* `minSdk 26`, `targetSdk 35`, thème plateforme `Theme.Material.*`.
* Le service est déclaré `foregroundServiceType="connectedDevice"` et appelle
  `startForeground()` immédiatement (obligatoire depuis Android 14).
* `AppLog` conserve les 200 dernières lignes et l'activité affiche les 12
  dernières : suffisant pour diagnostiquer sans `adb logcat`.
* `ApplicationId` : `dev.blueco.app` (`.debug` en build de debug, ce qui permet
  d'installer les deux variantes côte à côte).
