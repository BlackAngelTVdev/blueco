package dev.blueco.app

import android.util.Log

/**
 * Journal en mémoire (200 dernières lignes) à la fois dans logcat et dans l'UI.
 * Volontairement minimal : pas de dépendance, pas de fichier.
 */
object AppLog {
    private const val TAG = "blueco"
    private const val MAX_LINES = 200

    private val lines = ArrayDeque<String>()
    private val lock = Any()

    @Volatile
    var listener: ((String) -> Unit)? = null

    fun i(message: String) = add(Log.i(TAG, message), message)
    fun w(message: String) = add(Log.w(TAG, message), message)
    fun e(message: String, error: Throwable? = null) =
        add(Log.e(TAG, message, error), if (error == null) message else "$message (${error.message})")

    fun recent(): List<String> = synchronized(lock) { lines.toList() }

    private fun add(ignored: Int, message: String) {
        val stamped = "${java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault()).format(java.util.Date())}  $message"
        synchronized(lock) {
            lines.addLast(stamped)
            while (lines.size > MAX_LINES) lines.removeFirst()
        }
        listener?.invoke(stamped)
    }
}
