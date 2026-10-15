package dev.blueco.app

import android.content.Context

/** Réglages persistants de l'appli. */
class Prefs(context: Context) {

    private val prefs = context.applicationContext
        .getSharedPreferences("blueco", Context.MODE_PRIVATE)

    /** Adresse MAC du PC Linux qui publie le service SPP. */
    var peerMac: String
        get() = prefs.getString(KEY_PEER, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_PEER, value.trim().uppercase()).apply()

    /** Adresse MAC des écouteurs à basculer. */
    var headphonesMac: String
        get() = prefs.getString(KEY_HEADPHONES, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_HEADPHONES, value.trim().uppercase()).apply()

    /** Jeton partagé optionnel (doit être identique dans la config Linux). */
    var token: String
        get() = prefs.getString(KEY_TOKEN, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_TOKEN, value.trim()).apply()

    /** Délai entre deux tentatives de reconnexion, en millisecondes. */
    var retryMs: Long
        get() = prefs.getLong(KEY_RETRY, 5_000L)
        set(value) = prefs.edit().putLong(KEY_RETRY, value.coerceIn(1_000L, 120_000L)).apply()

    /** Nom logique annoncé au PC. */
    var deviceName: String
        get() = prefs.getString(KEY_NAME, "téléphone").orEmpty()
        set(value) = prefs.edit().putString(KEY_NAME, value.trim()).apply()

    /** Démarrage automatique du service au lancement. */
    var autoStart: Boolean
        get() = prefs.getBoolean(KEY_AUTOSTART, true)
        set(value) = prefs.edit().putBoolean(KEY_AUTOSTART, value).apply()

    companion object {
        private const val KEY_PEER = "peer_mac"
        private const val KEY_HEADPHONES = "headphones_mac"
        private const val KEY_TOKEN = "token"
        private const val KEY_RETRY = "retry_ms"
        private const val KEY_NAME = "device_name"
        private const val KEY_AUTOSTART = "auto_start"
    }
}
