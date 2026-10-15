package dev.blueco.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import org.json.JSONObject
import java.io.IOException

/**
 * Service de premier plan qui maintient la liaison Bluetooth avec le PC.
 *
 * * écoute les ordres du PC (`RELEASE` → je lâche les écouteurs, `GRAB` → je les reprends) ;
 * * expose `prendreEcouteurs()` : demander au PC de lâcher les écouteurs puis s'y connecter ;
 * * publie son état dans la notification et vers l'interface.
 */
class BluecoService : Service() {

    private lateinit var prefs: Prefs
    private lateinit var audio: AudioController
    private var link: PeerLink? = null

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        audio = AudioController(this)
        createChannel()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopEverything()
            return START_NOT_STICKY
        }
        // Toujours passer en premier plan : un service démarré par
        // startForegroundService() doit appeler startForeground() sans tarder.
        startForegroundCompat(getString(R.string.state_idle))
        startLink()
        when (intent?.action) {
            ACTION_TAKE -> takeHeadphones()
            ACTION_GIVE -> giveHeadphones()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        link?.stop()
        link = null
        super.onDestroy()
    }

    // ------------------------------------------------------------------ //
    // Lien Bluetooth
    // ------------------------------------------------------------------ //
    private fun startForegroundCompat(text: String) {
        val notification = buildNotification(text)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        publish(text)
    }

    private fun startLink() {
        val current = link
        if (current != null) {
            current.peerMac = prefs.peerMac
            current.token = prefs.token
            current.deviceName = prefs.deviceName
            current.retryMs = prefs.retryMs
            return
        }
        val created = PeerLink(
            context = this,
            handler = ::handleRequest,
            onEvent = ::handleEvent,
            onState = { _, message ->
                AppLog.i("lien: $message")
                updateNotification(message)
            },
        )
        created.peerMac = prefs.peerMac
        created.token = prefs.token
        created.deviceName = prefs.deviceName
        created.retryMs = prefs.retryMs
        link = created
        created.start()
    }

    private fun stopEverything() {
        link?.stop()
        link = null
        publish("service arrêté")
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    // ------------------------------------------------------------------ //
    // Ordres reçus du PC
    // ------------------------------------------------------------------ //
    private fun handleRequest(message: JSONObject): JSONObject {
        val id = message.optString("id")
        val command = message.optString("cmd").uppercase()
        if (!authorized(message)) {
            AppLog.w("requête $command refusée: jeton invalide")
            return Protocol.failure(id, Protocol.E_UNAUTHORIZED, "jeton invalide")
        }
        AppLog.i("ordre reçu: $command")
        return try {
            when (command) {
                Protocol.CMD_PING -> Protocol.ok(id, JSONObject()
                    .put("pong", true)
                    .put("device", prefs.deviceName)
                    .put("protocol", Protocol.VERSION))
                Protocol.CMD_WHOAMI -> Protocol.ok(id, JSONObject()
                    .put("device", prefs.deviceName)
                    .put("protocol", Protocol.VERSION))
                Protocol.CMD_STATUS -> Protocol.ok(id, statusPayload())
                Protocol.CMD_RELEASE -> Protocol.ok(id, audio.release(prefs.headphonesMac))
                Protocol.CMD_GRAB -> Protocol.ok(id, audio.grab(prefs.headphonesMac))
                else -> Protocol.failure(id, Protocol.E_UNSUPPORTED, "commande non gérée: $command")
            }
        } catch (e: IOException) {
            Protocol.failure(id, Protocol.E_AUDIO_UNAVAILABLE, e.message ?: "opération audio impossible")
        } catch (e: Exception) {
            AppLog.e("échec de $command", e)
            Protocol.failure(id, Protocol.E_INTERNAL, e.message ?: "erreur interne")
        }
    }

    private fun authorized(message: JSONObject): Boolean {
        val expected = prefs.token
        if (expected.isEmpty()) return true
        return expected == message.optString("token")
    }

    private fun handleEvent(message: JSONObject) {
        AppLog.i("événement du PC: ${message.optString("evt")}")
    }

    // ------------------------------------------------------------------ //
    // Boutons de l'appli
    // ------------------------------------------------------------------ //
    /** Je prends les écouteurs : le PC les libère, puis je me connecte. */
    fun takeHeadphones() {
        publish("bascule en cours…")
        Thread({
            val steps = mutableListOf<String>()
            var failure: String? = null
            try {
                val result = requireLink().request(Protocol.CMD_RELEASE, timeoutMs = 20_000L)
                steps.add("PC a libéré ${result.optString("released")}")
            } catch (e: Exception) {
                failure = "le PC n'a pas pu libérer les écouteurs: ${e.message}"
            }
            if (failure == null) {
                try {
                    val result = audio.grab(prefs.headphonesMac)
                    steps.add(if (result.optBoolean("already_connected")) "déjà connectés" else "écouteurs connectés")
                } catch (e: Exception) {
                    failure = "connexion locale impossible: ${e.message}"
                }
            }
            val summary = failure ?: steps.joinToString(" · ")
            AppLog.i("prendre: $summary")
            publish(summary)
        }, "blueco-take").start()
    }

    /** Je rends les écouteurs : je libère, puis le PC se connecte. */
    fun giveHeadphones() {
        publish("restitution en cours…")
        Thread({
            var summary: String
            try {
                audio.release(prefs.headphonesMac)
                val result = requireLink().request(Protocol.CMD_GRAB, timeoutMs = 20_000L)
                summary = "PC connecté à ${result.optString("mac")}"
            } catch (e: Exception) {
                summary = "restitution incomplète: ${e.message}"
            }
            AppLog.i("rendre: $summary")
            publish(summary)
        }, "blueco-give").start()
    }

    private fun requireLink(): PeerLink {
        startLink()
        return link ?: throw IOException("lien Bluetooth indisponible")
    }

    fun statusPayload(): JSONObject = JSONObject()
        .put("device", prefs.deviceName)
        .put("protocol", Protocol.VERSION)
        .put("audio", audio.status(prefs.headphonesMac))
        .put("peer", link?.describe() ?: "inactif")

    // ------------------------------------------------------------------ //
    // Notification et publication d'état
    // ------------------------------------------------------------------ //
    private fun createChannel() {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.channel_name),
            NotificationManager.IMPORTANCE_LOW
        ).apply { description = getString(R.string.channel_description) }
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val take = PendingIntent.getService(
            this, 1, Intent(this, BluecoService::class.java).setAction(ACTION_TAKE),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val give = PendingIntent.getService(
            this, 2, Intent(this, BluecoService::class.java).setAction(ACTION_GIVE),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val builder = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_blueco)
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, getString(R.string.action_take), take).build())
            .addAction(Notification.Action.Builder(null, getString(R.string.action_give), give).build())
        return builder.build()
    }

    private fun updateNotification(text: String) {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.notify(NOTIFICATION_ID, buildNotification(text))
        publish(text)
    }

    private fun publish(text: String) {
        listener?.invoke(text)
    }

    companion object {
        const val ACTION_TAKE = "dev.blueco.app.TAKE"
        const val ACTION_GIVE = "dev.blueco.app.GIVE"
        const val ACTION_STOP = "dev.blueco.app.STOP"
        private const val CHANNEL_ID = "blueco-link"
        private const val NOTIFICATION_ID = 42

        /** Observateur de l'interface (même processus ; volontairement minimal). */
        @Volatile
        var listener: ((String) -> Unit)? = null

        fun start(context: Context) = context.startForegroundServiceOrNull(Intent(context, BluecoService::class.java))

        fun action(context: Context, action: String) =
            context.startForegroundServiceOrNull(Intent(context, BluecoService::class.java).setAction(action))
    }
}

private fun Context.startForegroundServiceOrNull(intent: Intent) {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        startForegroundService(intent)
    } else {
        startService(intent)
    }
}
