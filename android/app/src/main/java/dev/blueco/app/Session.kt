package dev.blueco.app

import org.json.JSONObject
import java.io.BufferedReader
import java.io.IOException
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Session bidirectionnelle sur un flux RFCOMM, miroir de `blueco/session.py`.
 *
 * L'appli peut appeler [request] (elle envoie `RELEASE` au PC) et le PC peut lui
 * envoyer une requête sur la même connexion ([handler] l'exécute) : c'est ce qui
 * permet aussi la bascule déclenchée depuis le PC.
 */
class Session(
    private val reader: BufferedReader,
    private val writer: (JSONObject) -> Unit,
    private val handler: ((JSONObject) -> JSONObject)? = null,
    private val onEvent: ((JSONObject) -> Unit)? = null,
    private val onClosed: ((String) -> Unit)? = null,
) {
    private val pending = ConcurrentHashMap<String, ArrayBlockingQueue<JSONObject>>()
    private val closed = AtomicBoolean(false)
    private val workers = Executors.newFixedThreadPool(2)
    private var readerThread: Thread? = null

    val isAlive: Boolean get() = !closed.get()

    fun start(): Session {
        val thread = Thread({ readLoop() }, "blueco-read")
        thread.isDaemon = true
        readerThread = thread
        thread.start()
        return this
    }

    /** Bloque jusqu'à la fermeture de la connexion. */
    fun join(timeoutMs: Long = 0L) {
        val thread = readerThread ?: return
        if (timeoutMs > 0L) thread.join(timeoutMs) else thread.join()
    }

    /**
     * Envoie une requête et attend la réponse.
     * @throws RemoteException si le pair répond `ok:false`
     * @throws IOException si la connexion est fermée ou le délai dépassé
     */
    fun request(
        command: String,
        args: JSONObject? = null,
        token: String? = null,
        timeoutMs: Long = 15_000L,
    ): JSONObject {
        val message = Protocol.request(command, args, token)
        val id = message.getString("id")
        val slot = ArrayBlockingQueue<JSONObject>(1)
        pending[id] = slot
        try {
            send(message)
        } catch (e: Exception) {
            pending.remove(id)
            throw e
        }
        val reply = slot.poll(timeoutMs, TimeUnit.MILLISECONDS)
            ?: run {
                pending.remove(id)
                throw IOException("$command: aucune réponse du pair dans le délai imparti")
            }
        return Protocol.resultOf(reply)
    }

    fun notify(event: String, data: JSONObject? = null) {
        try {
            send(Protocol.event(event, data))
        } catch (e: IOException) {
            AppLog.w("événement $event non envoyé: ${e.message}")
        }
    }

    fun send(message: JSONObject) {
        if (closed.get()) throw IOException("connexion fermée")
        writer(message)
    }

    fun close() {
        if (closed.getAndSet(true)) return
        workers.shutdownNow()
        try {
            reader.close()
        } catch (_: IOException) {
        }
        failPending("connexion fermée")
    }

    private fun readLoop() {
        var reason = "pair déconnecté"
        try {
            while (!closed.get()) {
                val line = reader.readLine() ?: break
                if (line.isBlank()) continue
                val message = try {
                    Protocol.decode(line)
                } catch (e: ProtocolException) {
                    // Trame illisible : pas d'identifiant fiable, on ne répond pas.
                    AppLog.w("trame invalide (${e.code}): ${e.message}")
                    continue
                }
                dispatch(message)
            }
        } catch (e: IOException) {
            reason = e.message ?: "erreur de lecture"
        } finally {
            val wasOpen = !closed.getAndSet(true)
            workers.shutdown()
            failPending(reason)
            if (wasOpen) onClosed?.invoke(reason)
        }
    }

    private fun dispatch(message: JSONObject) {
        if (message.has("ok")) {
            val slot = pending.remove(message.optString("id"))
            slot?.offer(message)
            return
        }
        if (message.has("evt")) {
            onEvent?.invoke(message)
            return
        }
        val currentHandler = handler
        val id = message.optString("id")
        if (currentHandler == null) {
            try {
                send(Protocol.failure(id, Protocol.E_UNSUPPORTED, "aucun gestionnaire"))
            } catch (_: IOException) {
            }
            return
        }
        workers.execute {
            val reply = try {
                currentHandler(message)
            } catch (e: Exception) {
                AppLog.e("gestion de ${message.optString("cmd")} en échec", e)
                Protocol.failure(id, Protocol.E_INTERNAL, e.message ?: "erreur interne")
            }
            try {
                send(reply)
            } catch (_: IOException) {
            }
        }
    }

    private fun failPending(reason: String) {
        val failure = Protocol.failure("", Protocol.E_NOT_CONNECTED, reason)
        pending.values.forEach { it.offer(failure) }
        pending.clear()
    }
}
