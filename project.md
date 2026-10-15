Je veux concevoir une application minimaliste multiplateforme (Mobile Android + Desktop Linux) pour basculer rapidement des écouteurs Bluetooth entre mes appareils, en utilisant *exclusivement le Bluetooth* comme moyen de communication et de contrôle, sans aucun Wi-Fi, réseau local ou serveur tiers.

Le fonctionnement visé :
1. Les appareils (mon téléphone Android et mon PC Linux) sont appairés en Bluetooth classique.
2. Je veux utiliser un canal Bluetooth direct (comme un socket RFCOMM / profil SPP - Serial Port Profile, ou du BLE Advertising/GATT) pour que les appareils s'envoient des ordres courts.
3. Scénario de switch : Quand je clique sur un bouton sur mon téléphone (ou l'inverse sur le PC), l'appareil envoie une trame de commande via le lien Bluetooth direct vers l'autre appareil pour lui dire : "Libère les écouteurs".
4. L'appareil distant reçoit l'ordre via son script/démon en arrière-plan, exécute la déconnexion immédiate des écouteurs (ex: `bluetoothctl disconnect <MAC>`), puis l'appareil demandeur initie sa propre connexion vers les écouteurs.
5. Optionnel : un script de verrouillage de session sur le PC Linux basé sur la puissance du signal (RSSI) du Bluetooth du téléphone quand je m'éloigne.

Peux-tu me concevoir l'architecture et me fournir :
1. Le code d'un démon en Python sous Linux (utilisant BlueZ / dbus / sockets RFCOMM ou BLE) qui écoute les commandes Bluetooth entrantes et gère la déconnexion des écouteurs.
2. Le code minimaliste pour l'application ou le script Android (en Python/Kivy ou Kotlin/Flutter via les APIs Bluetooth natives) capable de se connecter en tant que client/serveur RFCOMM ou BLE pour envoyer cet ordre.
3. Les étapes de configuration indispensables sous Linux (permissions, appairage, bind des canaux RFCOMM) pour que la communication Bluetooth inter-appareils fonctionne sans broncher.