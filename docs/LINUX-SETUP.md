# Installation Linux (Arch Linux)

L'installation est volontairement déclarative : le démon **n'a besoin d'aucun
privilège** et ne modifie aucune configuration système. L'appairage Bluetooth
est fait une fois pour toutes avec `bluetoothctl`.

## 1. Paquets

```bash
./scripts/build-linux.sh --deps
# équivaut à :
sudo pacman -S --needed python python-dbus python-gobject bluez bluez-utils
```

| Paquet | Pourquoi |
| --- | --- |
| `python` | ≥ 3.10, sockets `AF_BLUETOOTH`/`BTPROTO_RFCOMM` |
| `python-dbus` | pilotage de BlueZ (`org.bluez`) et enregistrement du profil SPP |
| `python-gobject` | boucle GLib qui sert les appels D-Bus entrants du profil |
| `bluez`, `bluez-utils` | `bluetoothd`, `bluetoothctl`, `btmon` |

> Sur Arch, `bluez-utils` ne fournit **plus** `rfcomm` ni `sdptool`. blueco n'en
> a pas besoin : il ouvre des sockets `AF_BLUETOOTH` directement et laisse BlueZ
> publier l'enregistrement SDP. Le mode `rfcomm` de blueco ne dépend pas non plus
> de l'outil `rfcomm` du même nom.

Vérification de l'environnement :

```bash
python3 -c "import socket; print(socket.AF_BLUETOOTH, socket.BTPROTO_RFCOMM)"
python3 -c "import dbus, gi; print('ok')"
systemctl status bluetooth
```

## 2. Service Bluetooth

```bash
sudo systemctl enable --now bluetooth
rfkill list                     # vérifiez qu'aucun blocage n'est actif
sudo rfkill unblock bluetooth   # si nécessaire
bluetoothctl show               # adaptateur allumé, adresse affichée
```

Le mode `profile1` a besoin que `bluetoothd` tourne (il l'est dès que
`bluetooth` est actif) et que l'adaptateur soit *powered*.

## 3. Appairage (une seule fois)

```bash
bluetoothctl
> power on
> agent on
> default-agent
> scan on
> pair AA:BB:CC:DD:EE:FF        # casque
> trust AA:BB:CC:DD:EE:FF       # ★ indispensable pour la reprise depuis le PC
> pair 11:22:33:44:55:66        # téléphone Android — confirmez le code des deux côtés
> trust 11:22:33:44:55:66
> scan off
> quit
```

`trust` est important : sans lui, BlueZ refuse parfois les connexions entrantes,
et l'appli Android ne peut pas se connecter au PC.

Vérification côté BlueZ :

```bash
btmon &            # journal HCI si vous voulez voir les trames
bluetoothctl devices
blueco devices     # même liste, avec l'état connecté/appairé
```

## 4. Configuration de blueco

```bash
blueco init --peer-mac 11:22:33:44:55:66 --headphones-mac AA:BB:CC:DD:EE:FF
# ou à la main :
$EDITOR ~/.config/blueco/config.json
```

`~/.config/blueco/config.json` :

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

Passer `"token": "un secret"` active le contrôle d'accès : il faut alors
renseigner le **même** jeton dans l'appli Android.

## 5. Lancement

```bash
blueco serve -v            # premier plan, journal détaillé
blueco status              # état local du casque
blueco status --remote     # + état du téléphone (si l'appli est lancée)
blueco take                # je prends les écouteurs depuis le PC
blueco give                # je les rends au téléphone
```

En service utilisateur :

```bash
./scripts/build-linux.sh --systemd
systemctl --user status blueco
journalctl --user -u blueco -f
```

Le service est *utilisateur* : il démarre après votre session (`default.target`)
et n'a pas besoin de root.

## 6. Verrouillage de session à l'éloignement (option)

```bash
blueco watch --interval 10 --failures 3 --lock-command "loginctl lock-session"
```

* **Principe** : un `PING` toutes les 10 s ; après 3 échecs consécutifs, la
  session est verrouillée ; au retour du téléphone, `--unlock-command` (si
  fourni) est exécuté et `--grab-on-return` peut reprendre le casque.
* **RSSI** : `--rssi-threshold -60` n'a d'effet que si `hcitool` est présent.
  La plupart des distributions récentes ne le livrent plus (`bluetoothctl` a
  remplacé `hciconfig`/`hcitool`), et BlueZ n'expose pas le RSSI BR/EDR par
  D-Bus. Sans l'outil, blueco retombe automatiquement sur la présence du lien —
  ce qui suffit pour « le téléphone s'est éloigné ».
* Le service correspondant : `systemctl --user enable --now blueco-watch`.

## 7. Si BlueZ refuse l'enregistrement du profil SPP

Symptôme : au démarrage, `enregistrement du profil SPP refusé par BlueZ: …`.

1. **Vérifiez d'abord** : `bluetoothctl show` doit afficher une adresse et
   `Powered: yes`. Un adaptateur éteint provoque exactement ce message.
2. **Piste 1 — enregistrement SDP explicite.** Renseignez
   `transport.service_record` avec un fichier XML décrivant le service SPP ;
   BlueZ l'utilisera tel quel (le canal du XML doit correspondre à
   `transport.channel`) :

   ```xml
   <?xml version="1.0" encoding="UTF-8" ?>
   <record>
     <attribute id="0x0001"><sequence><uuid value="0x1101" /></sequence></attribute>
     <attribute id="0x0004">
       <sequence>
         <sequence><uuid value="0x0100" /></sequence>
         <sequence><uuid value="0x0003" /><uint8 value="0x03" /></sequence>
       </sequence>
     </attribute>
     <attribute id="0x0005"><sequence><uuid value="0x1002" /></sequence></attribute>
     <attribute id="0x0009">
       <sequence><sequence><uuid value="0x1101" /><uint16 value="0x0102" /></sequence></sequence>
     </attribute>
   </record>
   ```

   ```bash
   blueco serve --transport profile1   # avec transport.service_record renseigné
   ```

3. **Piste 2 — mode RFCOMM brut** (aucun SDP publié) :

   ```bash
   blueco serve --transport rfcomm --channel 3
   ```

   ⚠️ Dans ce mode, l'appli Android **ne peut pas se connecter** (elle résout le
   service par UUID, donc par SDP). Il sert au diagnostic, aux échanges entre
   deux machines Linux (`blueco --transport rfcomm …` des deux côtés) et à tout
   client SPP qui connaît déjà le canal.

## 8. Dépannage

| Symptôme | Diagnostic |
| --- | --- |
| `service BlueZ absent (bluetoothd ?)` | `sudo systemctl start bluetooth` |
| `aucun adaptateur Bluetooth trouvé` | adaptateur désactivé / `rfkill` bloqué |
| `appareil AA:… inconnu de BlueZ` | mauvaise adresse, ou appareil jamais appairé |
| `connexion à AA:… refusée: org.bluez.Error.Failed` | casque déjà connecté ailleurs (téléphone) : libérez-le d'abord |
| `aucun pair connecté (lancez l'appli Android)` | l'appli n'est pas démarrée ou le PC n'est pas appairé ; `blueco serve -v` doit journaliser « connexion SPP entrante de … » |
| Le casque se reconnecte tout seul au PC après un `RELEASE` | PipeWire/wireplumber relance la connexion : coupez le profil sur cette machine (`wpctl status`, ou désactivez la reconnexion automatique du nœud) ou c'est le casque qui reprend son dernier appareil |
| Le téléphone ne se connecte jamais au PC | appairage manquant ou non `trust`é : `bluetoothctl trust <MAC_TELEPHONE>` |
| Rien dans le journal du démon | lancez avec `-v` (info) ou `-vv` (debug) |

## 9. Désinstallation

```bash
./scripts/build-linux.sh --uninstall     # lanceur + unités systemd
rm -rf ~/.config/blueco                  # configuration (si vous le souhaitez)
```

## 10. Vérifier sans matériel

Utile avant même d'appairer quoi que ce soit :

```bash
python3 -m blueco --fake --transport unix --socket /tmp/blueco.sock serve &
python3 -m blueco --fake --transport unix --socket /tmp/blueco.sock selftest
python3 -m blueco --fake --transport unix --socket /tmp/blueco.sock status
```
