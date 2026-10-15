package dev.blueco.app

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothSocket
import android.content.Context
import org.json.JSONObject
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader
import java.util.UUID

/**
 * Lien RFCOMM vers le PC Linux, avec reconnexion automatique.
 *
 * L'appli est *cliente* : le PC enregistre le profil SPP auprès de BlueZ et
 * publie l'enregistrement SDP, donc la résolution du service se fait par UUID
 * avec l'API publique [BluetoothSocket] — aucun canal en dur à maintenir.
 */
class PeerLink(
    private val context: Context,
    private val handler: (JSONObject) -> JSONObject,
    private val onEvent: (JSONObject) -> Unit,
    private val onState: (State, String) -> Unit,
) {
    enum class State { IDLE, WAITING, CONNECTING, CONNECTED }

    @Volatile var peerMac: String = ""
    @Volatile var token: String = ""
    @Volatile var deviceName: String = "téléphone"
    @Volatile var retryMs: Long = 5_000L

    @Volatile private var running = false
    @Volatile private var active: Session? = null
    private var socket: BluetoothSocket? = null
    private var thread: Thread? = null

    val isConnected: Boolean get() = active?.isAlive == true

    fun start() {
        if (running) return
        running = true
        val worker = Thread({ loop() }, "blueco-link")
        worker.isDaemon = true
        thread = worker
        worker.start()
    }

    fun stop() {
        running = false
        active?.close()
        active = null
        closeSocket()
        thread?.interrupt()
        thread = null
    }

    /** Requête vers le PC ; lève [IOException] si le lien n'est pas établi. */
    fun request(command: String, args: JSONObject? = null, timeoutMs: Long = 15_000L): JSONObject {
        val session = active
        if (session == null || !session.isAlive) throw IOException("pair non connecté")
        return session.request(command, args, token.ifEmpty { null }, timeoutMs)
    }

    fun notify(event: String, data: JSONObject? = null) {
        active?.notify(event, data)
    }

    fun describe(): String = when {
        active?.isAlive == true -> "connecté à $peerMac"
        peerMac.isEmpty() -> "adresse du PC non configurée"
        else -> "en attente de $peerMac"
    }

    private fun loop() {
        var backoff = retryMs.coerceAtLeast(1_000L)
        while (running) {
            if (peerMac.isEmpty()) {
                onState(State.IDLE, "adresse du PC non configurée")
                if (!pause(3_000L)) break
                continue
            }
            onState(State.CONNECTING, "connexion à $peerMac…")
            try {
                val bluetoothSocket = connect()
                socket = bluetoothSocket
                backoff = retryMs.coerceAtLeast(1_000L)
                val session = Session(
                    reader = BufferedReader(InputStreamReader(bluetoothSocket.inputStream, Charsets.UTF_8)),
                    writer = { message ->
                        val out = bluetoothSocket.outputStream
                        out.write(Protocol.encode(message))
                        out.flush()
                    },
                    handler = handler,
                    onEvent = onEvent,
                    onClosed = { reason -> onState(State.WAITING, "déconnecté ($reason)") },
                ).start()
                active = session
                onState(State.CONNECTED, "connecté à $peerMac")
                session.notify(
                    Protocol.EVT_HELLO,
                    JSONObject().put("device", deviceName).put("protocol", Protocol.VERSION)
                )
                session.join()
                active = null
                closeSocket()
            } catch (e: Exception) {
                onState(State.WAITING, "échec: ${e.message}")
                closeSocket()
            }
            if (!running) break
            if (!pause(backoff)) break
            backoff = (backoff * 2).coerceAtMost(60_000L)
        }
        onState(State.IDLE, "lien arrêté")
    }

    @SuppressLint("MissingPermission")
    private fun connect(): BluetoothSocket {
        val adapter = adapter() ?: throw IOException("Bluetooth indisponible")
        if (!adapter.isEnabled) throw IOException("Bluetooth désactivé")
        val device = try {
            adapter.getRemoteDevice(peerMac)
        } catch (e: IllegalArgumentException) {
            throw IOException("adresse MAC invalide: $peerMac")
        }
        try {
            adapter.cancelDiscovery()
        } catch (_: SecurityException) {
        }
        val uuid = UUID.fromString(Protocol.SPP_UUID)
        val candidate = device.createRfcommSocketToServiceRecord(uuid)
        try {
            candidate.connect()
        } catch (e: IOException) {
            try {
                candidate.close()
            } catch (_: IOException) {
            }
            throw IOException("connexion SPP refusée (${e.message})")
        }
        return candidate
    }

    @SuppressLint("MissingPermission")
    private fun adapter(): BluetoothAdapter? =
        (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter

    private fun closeSocket() {
        val current = socket
        socket = null
        try {
            current?.close()
        } catch (_: IOException) {
        }
    }

    private fun pause(millis: Long): Boolean = try {
        Thread.sleep(millis)
        true
    } catch (_: InterruptedException) {
        false
    }
}
