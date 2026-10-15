package dev.blueco.app

import org.json.JSONException
import org.json.JSONObject
import java.util.UUID

/**
 * Protocole blueco v1 — miroir de `blueco/protocol.py` (voir docs/PROTOCOL.md).
 *
 * Une trame = un objet JSON par ligne (`\n`). Trois formes circulent :
 * requête (`cmd`), réponse (`ok`), événement (`evt`).
 */
object Protocol {
    const val VERSION = 1
    const val MAX_FRAME = 2048

    /** UUID du profil SPP, identique côté Linux. */
    const val SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"

    const val CMD_PING = "PING"
    const val CMD_STATUS = "STATUS"
    const val CMD_RELEASE = "RELEASE"
    const val CMD_GRAB = "GRAB"
    const val CMD_WHOAMI = "WHOAMI"
    val COMMANDS = setOf(CMD_PING, CMD_STATUS, CMD_RELEASE, CMD_GRAB, CMD_WHOAMI)

    const val EVT_HELLO = "HELLO"
    const val EVT_STATE = "STATE"

    const val E_BAD_FRAME = "BAD_FRAME"
    const val E_UNSUPPORTED = "UNSUPPORTED_COMMAND"
    const val E_UNAUTHORIZED = "UNAUTHORIZED"
    const val E_NO_DEVICE = "NO_DEVICE"
    const val E_AUDIO_UNAVAILABLE = "AUDIO_UNAVAILABLE"
    const val E_NOT_CONNECTED = "NOT_CONNECTED"
    const val E_INTERNAL = "INTERNAL"

    fun newId(): String = UUID.randomUUID().toString().replace("-", "").substring(0, 12)

    fun request(command: String, args: JSONObject? = null, token: String? = null): JSONObject {
        val message = JSONObject()
            .put("v", VERSION)
            .put("id", newId())
            .put("cmd", command.uppercase())
        if (args != null) message.put("args", args)
        if (!token.isNullOrEmpty()) message.put("token", token)
        return message
    }

    fun ok(id: String, result: JSONObject? = null): JSONObject = JSONObject()
        .put("v", VERSION)
        .put("id", id)
        .put("ok", true)
        .put("result", result ?: JSONObject())

    fun failure(id: String, code: String, message: String = ""): JSONObject {
        val error = JSONObject().put("code", code)
        if (message.isNotEmpty()) error.put("message", message)
        return JSONObject()
            .put("v", VERSION)
            .put("id", id)
            .put("ok", false)
            .put("error", error)
    }

    fun event(name: String, data: JSONObject? = null): JSONObject = JSONObject()
        .put("v", VERSION)
        .put("evt", name)
        .put("data", data ?: JSONObject())

    /** Sérialise une trame en une ligne UTF-8 terminée par `\n`. */
    fun encode(message: JSONObject): ByteArray = (message.toString() + "\n").toByteArray(Charsets.UTF_8)

    /** Valide et désérialise une ligne. Lève [ProtocolException] si non conforme. */
    fun decode(line: String): JSONObject {
        val trimmed = line.trim()
        if (trimmed.isEmpty()) throw ProtocolException(E_BAD_FRAME, "trame vide")
        if (trimmed.toByteArray(Charsets.UTF_8).size > MAX_FRAME) {
            throw ProtocolException(E_BAD_FRAME, "trame trop longue")
        }
        val message = try {
            JSONObject(trimmed)
        } catch (e: JSONException) {
            throw ProtocolException(E_BAD_FRAME, "JSON invalide: ${e.message}")
        }
        if (message.optInt("v", VERSION) != VERSION) {
            throw ProtocolException(E_BAD_FRAME, "version non supportée")
        }
        if (message.has("evt")) {
            if (message.optString("evt").isEmpty()) throw ProtocolException(E_BAD_FRAME, "événement sans nom")
            return message
        }
        if (message.has("ok")) {
            if (!message.has("id")) throw ProtocolException(E_BAD_FRAME, "réponse sans identifiant")
            return message
        }
        val command = message.optString("cmd").uppercase()
        if (command.isEmpty()) throw ProtocolException(E_BAD_FRAME, "ni cmd, ni ok, ni evt")
        if (command !in COMMANDS) throw ProtocolException(E_UNSUPPORTED, "commande inconnue: $command")
        if (!message.has("id")) throw ProtocolException(E_BAD_FRAME, "requête sans identifiant")
        return message
    }

    /** Renvoie le `result` d'une réponse, ou lève [RemoteException] si `ok` est faux. */
    fun resultOf(reply: JSONObject): JSONObject {
        if (reply.optBoolean("ok", false)) return reply.optJSONObject("result") ?: JSONObject()
        val error = reply.optJSONObject("error")
        val rawCode = error?.optString("code").orEmpty()
        throw RemoteException(
            if (rawCode.isEmpty()) E_INTERNAL else rawCode,
            error?.optString("message").orEmpty()
        )
    }
}

class ProtocolException(val code: String, message: String) : Exception(message)

/** Le pair a répondu `ok:false`. */
class RemoteException(val code: String, message: String) : Exception("$code: $message")
