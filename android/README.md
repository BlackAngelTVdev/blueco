# blueco — appli Android

Compagnon du démon Linux : elle se connecte au PC en Bluetooth classique
(RFCOMM/SPP) et exécute les bascules d'écouteurs.

## Compiler

Depuis la racine du dépôt :

```bash
./scripts/build-android.sh --release      # APK dans dist/
./scripts/build-android.sh --adb          # compile puis installe sur le téléphone
```

Le script détecte le JDK et le SDK, écrit `local.properties`, télécharge Gradle
si nécessaire. Prérequis :

```bash
sudo pacman -S jdk17-openjdk android-sdk android-sdk-platform-tools
# puis, si les build-tools manquent :
sdkmanager "platforms;android-35" "build-tools;35.0.0"
```

Android Studio fonctionne aussi : ouvrez le dossier `android/` et lancez
`assembleDebug`. **Aucune dépendance tierce** n'est utilisée (ni AndroidX ni
Compose) : uniquement les API de la plateforme, donc rien à télécharger hors
du SDK et de Gradle.

## Permissions

| Permission | Pourquoi |
| --- | --- |
| `BLUETOOTH` / `BLUETOOTH_ADMIN` (≤ Android 11) | compatibilité avec les anciennes versions |
| `BLUETOOTH_CONNECT` (Android 12+) | ouvrir le socket RFCOMM, lire les appareils appairés |
| `BLUETOOTH_SCAN` (Android 12+, `neverForLocation`) | requise pour manipuler le Bluetooth ; l'appli ne fait aucune découverte |
| `FOREGROUND_SERVICE` + `..._CONNECTED_DEVICE` | maintenir la liaison écran éteint (Android 14+) |
| `POST_NOTIFICATIONS` (Android 13+) | afficher la notification du service |

L'appli demande ces autorisations au premier lancement.

## Architecture

| Fichier | Rôle |
| --- | --- |
| `Protocol.kt` | codec des trames, miroir de `blueco/protocol.py` |
| `Session.kt` | requêtes/réponses et événements bidirectionnels sur un flux |
| `PeerLink.kt` | lien RFCOMM vers le PC, reconnexion automatique avec backoff |
| `AudioController.kt` | connexion/déconnexion A2DP et HFP des écouteurs |
| `BluecoService.kt` | service de premier plan : écoute les ordres du PC |
| `MainActivity.kt` | réglages, état, boutons « Prendre » / « Rendre » |
| `Prefs.kt` | réglages persistants (MAC du PC, MAC des écouteurs, jeton) |

L'appli est **cliente RFCOMM** : elle résout le service SPP par UUID via
`createRfcommSocketToServiceRecord()`, donc elle a besoin que le PC publie
l'enregistrement SDP (mode `profile1`, le défaut). Sur la connexion établie,
les échanges sont bidirectionnels : c'est aussi par là que le PC pousse un
`RELEASE` quand vous basculez depuis l'ordinateur.

## Utilisation

1. Appairez le téléphone avec le PC **et** avec les écouteurs (une fois).
2. Sur le PC : `blueco init --peer-mac <MAC_TELEPHONE> --headphones-mac <MAC_CASQUE>` puis `blueco serve -v`.
3. Dans l'appli : « Choisir le PC dans la liste » → « Démarrer le service ».
4. Le bouton « Prendre les écouteurs » envoie `RELEASE` au PC, attend sa
   confirmation, puis connecte les écouteurs au téléphone. Le bouton est aussi
   disponible dans la notification.

## Point sensible : connecter/déconnecter l'audio sur Android

Android n'expose **aucune API publique** pour connecter ou déconnecter un
profil audio. `BluetoothA2dp.connect()/disconnect()` et
`BluetoothHeadset.connect()/disconnect()` sont marquées `@hide`.

`AudioController` obtient donc le proxy de profil via l'API publique
`getProfileProxy()` puis appelle ces méthodes par réflexion. C'est le mécanisme
utilisé par la plupart des applis qui font cela. Conséquences pratiques :

* sur la majorité des téléphones (Android 8 → 15) cela fonctionne ;
* si la politique d'API masquée de votre ROM bloque la réflexion, l'appli
  renvoie une erreur explicite (`AUDIO_UNAVAILABLE`) et vous pouvez
  déconnecter/reconnecter les écouteurs depuis les réglages Bluetooth ;
* alternative plus fiable sans root : [Shizuku](https://shizuku.rikka.app/)
  (le processus est alors exécuté sous `shell`, ce qui lève la restriction).
  La bascule PC → téléphone fonctionne de toute façon sans cette API : le PC
  libère les écouteurs, et Android se reconnecte habituellement tout seul aux
  derniers écouteurs utilisés.

## Dépannage

| Symptôme | Cause probable |
| --- | --- |
| « échec: connexion SPP refusée » | le démon tourne en mode `rfcomm` (pas d'enregistrement SDP) ou bluetoothd n'a pas accepté l'enregistrement du profil |
| « adresse MAC invalide » | format `AA:BB:CC:DD:EE:FF` attendu, avec les deux-points |
| Rien ne se passe au bouton | vérifiez le journal dans l'appli ; le PC doit être joignable (`blueco status --remote` côté PC) |
| Le service s'arrête seul | autorisations manquantes ou batterie en optimisation agressive : autorisez blueco à tourner en arrière-plan |
