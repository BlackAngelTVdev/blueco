package dev.blueco.app

import android.annotation.SuppressLint
import android.bluetooth.BluetoothClass
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.content.Context
import java.util.UUID

/**
 * Devine tout seul le PC et les écouteurs, pour que l'utilisateur n'ait **rien**
 * à saisir.
 *
 * Une seule règle : on ne regarde que les appareils **déjà appairés** (donc
 * jamais de scan, jamais de demande de localisation). Si le PC et les écouteurs
 * ont été appairés une fois avec le téléphone, l'appli les retrouve.
 *
 * Le script `scripts/setup-blueco.sh` peut en plus pousser les valeurs exactes
 * par USB ; cette détection sert de filet de sécurité (nouveau PC, appli
 * réinstallée, préférences vidées…).
 */
object AutoConfig {

    private val SPP = UUID.fromString(Protocol.SPP_UUID)

    /** Profil « sink » A2DP : les écouteurs qui reçoivent le son. */
    private val A2DP_SINK = UUID.fromString("0000110b-0000-1000-8000-00805f9b34fb")

    /** Profil « hands-free » : utilisé par la plupart des casques. */
    private val HEADSET = UUID.fromString("00001108-0000-1000-8000-00805f9b34fb")

    /**
     * Complète les adresses manquantes dans [prefs].
     * @return `true` si une valeur a été trouvée (donc écrite).
     */
    fun ensure(context: Context, prefs: Prefs, audio: AudioController): Boolean {
        var changed = false

        if (prefs.peerMac.isEmpty()) {
            val peer = findPeer(context)
            if (peer != null) {
                prefs.peerMac = peer.address
                prefs.peerName = label(peer)
                AppLog.i("PC reconnu tout seul : ${prefs.peerName} (${peer.address})")
                changed = true
            }
        }

        if (prefs.headphonesMac.isEmpty()) {
            val headphones = findHeadphones(context, audio)
            if (headphones != null) {
                prefs.headphonesMac = headphones.address
                prefs.headphonesName = label(headphones)
                AppLog.i("écouteurs reconnus tout seuls : ${prefs.headphonesName} (${headphones.address})")
                changed = true
            }
        }

        return changed
    }

    /** L'appareil appairé qui ressemble le plus à un PC. */
    @SuppressLint("MissingPermission")
    fun findPeer(context: Context): BluetoothDevice? {
        val candidates = bonded(context).filterNot { looksLikeAudio(it) }
        if (candidates.isEmpty()) return null

        // Un PC se reconnaît à sa classe Bluetooth (COMPUTER) et, une fois que
        // BlueZ a publié le profil, à l'UUID SPP dans son cache SDP.
        val scored = candidates.map { device ->
            var score = 0
            if (majorClass(device) == BluetoothClass.Device.Major.COMPUTER) score += 10
            if (uuids(device).contains(SPP)) score += 5
            device to score
        }
        val best = scored.maxByOrNull { it.second } ?: return null
        return if (best.second > 0) best.first else null
    }

    /** Les écouteurs : ceux qui jouent le son, sinon ceux qui en ont le profil. */
    fun findHeadphones(context: Context, audio: AudioController): BluetoothDevice? {
        val active = audio.connectedAudioDevices()
        if (active.isNotEmpty()) return active.first()

        val scored = bonded(context).map { device ->
            var score = if (looksLikeAudio(device)) 1 else 0
            val profiles = uuids(device)
            if (profiles.contains(A2DP_SINK)) score += 4
            if (profiles.contains(HEADSET)) score += 2
            device to score
        }
        val best = scored.maxByOrNull { it.second } ?: return null
        return if (best.second > 0) best.first else null
    }

    // ------------------------------------------------------------------ //

    @SuppressLint("MissingPermission")
    private fun bonded(context: Context): List<BluetoothDevice> {
        val adapter = (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
            ?: return emptyList()
        return try {
            adapter.bondedDevices?.toList().orEmpty().filter { it.bondState == BluetoothDevice.BOND_BONDED }
        } catch (_: SecurityException) {
            emptyList()
        }
    }

    /** La classe Bluetooth de l'appareil, ou `null` si Android la cache. */
    @SuppressLint("MissingPermission")
    private fun majorClass(device: BluetoothDevice): Int? = try {
        device.bluetoothClass?.majorDeviceClass
    } catch (_: SecurityException) {
        null
    }

    private fun looksLikeAudio(device: BluetoothDevice): Boolean =
        majorClass(device) == BluetoothClass.Device.Major.AUDIO_VIDEO

    /** Cache SDP des profils de l'appareil (nécessite la permission Bluetooth). */
    @SuppressLint("MissingPermission")
    private fun uuids(device: BluetoothDevice): Set<UUID> = try {
        device.uuids?.map { it.uuid }?.toSet().orEmpty()
    } catch (_: SecurityException) {
        emptySet()
    }

    /** Nom lisible, avec repli sur l'adresse. */
    @SuppressLint("MissingPermission")
    fun label(device: BluetoothDevice): String = try {
        device.name?.takeIf { it.isNotBlank() } ?: device.address
    } catch (_: SecurityException) {
        device.address
    }
}
