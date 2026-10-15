package dev.blueco.app

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.Context
import org.json.JSONObject
import java.io.IOException
import java.lang.reflect.InvocationTargetException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Libère / reprend les écouteurs côté Android.
 *
 * Android n'expose **aucune API publique** pour connecter ou déconnecter un
 * profil audio : `BluetoothA2dp.connect()/disconnect()` et
 * `BluetoothHeadset.connect()/disconnect()` sont `@hide`.
 *
 * On obtient donc le proxy de profil par l'API publique
 * (`getProfileProxy`, qui est publique) puis on appelle ces méthodes par
 * réflexion. Cela fonctionne sur la plupart des Android 8→15 ; si une version
 * ou un constructeur bloque la réflexion, l'appli renvoie une erreur explicite
 * et il reste la connexion/déconnexion manuelle depuis les réglages Bluetooth,
 * ou l'usage de Shizuku/root (voir docs/ANDROID.md).
 */
@SuppressLint("MissingPermission")
class AudioController(private val context: Context) {

    private enum class Change { CONNECT, DISCONNECT }

    fun status(mac: String): JSONObject {
        val label = deviceName(mac)
        val connected = connectedAudioDevices()
            .any { it.address.equals(mac, ignoreCase = true) }
        return JSONObject()
            .put("mac", mac)
            .put("name", label)
            .put("found", label.isNotEmpty() || connected)
            .put("connected", connected)
            .put("backend", "android")
    }

    /** Déconnecte les écouteurs de ce téléphone : ils deviennent libres. */
    fun release(mac: String): JSONObject {
        val device = device(mac)
        val wasConnected = isConnected(device)
        if (!wasConnected) {
            return JSONObject()
                .put("released", mac)
                .put("name", safeName(device))
                .put("was_connected", false)
                .put("connected", false)
                .put("already_free", true)
        }
        val done = change(BluetoothProfile.A2DP, device, Change.DISCONNECT) or
            change(BluetoothProfile.HEADSET, device, Change.DISCONNECT)
        if (!done) {
            throw IOException(
                "Android a refusé la déconnexion de $mac (API masquée bloquée sur cette version) : " +
                    "déconnectez les écouteurs depuis les réglages Bluetooth"
            )
        }
        return JSONObject()
            .put("released", mac)
            .put("name", safeName(device))
            .put("was_connected", true)
            .put("connected", false)
            .put("already_free", false)
    }

    /** Reconnecte les écouteurs à ce téléphone. */
    fun grab(mac: String): JSONObject {
        val device = device(mac)
        val already = isConnected(device)
        if (already) {
            return JSONObject().put("connected", true).put("mac", mac).put("already_connected", true)
        }
        val done = change(BluetoothProfile.A2DP, device, Change.CONNECT)
        if (!done) {
            throw IOException(
                "Android a refusé la connexion vers $mac : " +
                    "ouvrez les réglages Bluetooth et touchez les écouteurs"
            )
        }
        return JSONObject().put("connected", true).put("mac", mac).put("already_connected", false)
    }

    fun connectedAudioDevices(): List<BluetoothDevice> {
        val found = mutableListOf<BluetoothDevice>()
        withProfile(BluetoothProfile.A2DP) { profile ->
            found.addAll(profile.connectedDevices.orEmpty())
        }
        withProfile(BluetoothProfile.HEADSET) { profile ->
            profile.connectedDevices.orEmpty().forEach { candidate ->
                if (found.none { it.address.equals(candidate.address, ignoreCase = true) }) {
                    found.add(candidate)
                }
            }
        }
        return found
    }

    fun isConnected(mac: String): Boolean = connectedAudioDevices()
        .any { it.address.equals(mac, ignoreCase = true) }

    // ------------------------------------------------------------------ //

    private fun isConnected(device: BluetoothDevice): Boolean = isConnected(device.address)

    private fun device(mac: String): BluetoothDevice {
        if (mac.isBlank()) throw IOException("aucune adresse d'écouteurs configurée")
        val adapter = adapter() ?: throw IOException("Bluetooth indisponible")
        return try {
            adapter.getRemoteDevice(mac)
        } catch (e: IllegalArgumentException) {
            throw IOException("adresse MAC invalide: $mac")
        }
    }

    private fun safeName(device: BluetoothDevice): String = try {
        device.name ?: ""
    } catch (_: SecurityException) {
        ""
    }

    private fun deviceName(mac: String): String = try {
        safeName(device(mac))
    } catch (_: IOException) {
        ""
    }

    private fun change(profile: Int, device: BluetoothDevice, action: Change): Boolean {
        val method = if (action == Change.CONNECT) "connect" else "disconnect"
        val outcome = withProfile(profile) { proxy -> invokeHidden(proxy, method, device) }
        return outcome == true
    }

    /**
     * Ouvre le proxy de profil (API publique), exécute [block], puis le referme.
     * Renvoie `null` si le proxy n'a pas pu être obtenu.
     */
    private fun <T> withProfile(profile: Int, block: (BluetoothProfile) -> T): T? {
        val adapter = adapter() ?: return null
        val latch = CountDownLatch(1)
        var proxy: BluetoothProfile? = null
        val listener = object : BluetoothProfile.ServiceListener {
            override fun onServiceConnected(profileId: Int, service: BluetoothProfile) {
                proxy = service
                latch.countDown()
            }

            override fun onServiceDisconnected(profileId: Int) {
                latch.countDown()
            }
        }
        val requested = try {
            adapter.getProfileProxy(context, listener, profile)
        } catch (_: SecurityException) {
            false
        }
        if (!requested) return null
        if (!latch.await(3, TimeUnit.SECONDS)) return null
        val service = proxy ?: return null
        try {
            return block(service)
        } finally {
            try {
                adapter.closeProfileProxy(profile, service)
            } catch (_: Exception) {
            }
        }
    }

    private fun invokeHidden(proxy: BluetoothProfile, method: String, device: BluetoothDevice): Boolean {
        return try {
            val callable = proxy.javaClass.getMethod(method, BluetoothDevice::class.java)
            (callable.invoke(proxy, device) as? Boolean) ?: false
        } catch (e: NoSuchMethodException) {
            AppLog.w("profil ${proxy.javaClass.simpleName}: $method() absente (API modifiée ?)")
            false
        } catch (e: IllegalAccessException) {
            AppLog.w("profil ${proxy.javaClass.simpleName}: $method() inaccessible (API masquée bloquée)")
            false
        } catch (e: InvocationTargetException) {
            AppLog.w("profil ${proxy.javaClass.simpleName}: $method() refusée (${e.targetException?.message})")
            false
        }
    }

    private fun adapter(): BluetoothAdapter? =
        (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
}
