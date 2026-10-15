package dev.blueco.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.bluetooth.BluetoothManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast

/**
 * Écran unique, pensé pour un seul geste : un gros bouton qui bascule les
 * écouteurs entre ce téléphone et le PC, et qui dit toujours où sont les
 * écouteurs.
 *
 * Rien à configurer à la main : [AutoConfig] retrouve le PC et les écouteurs
 * parmi les appareils **déjà appairés**, et `scripts/setup-blueco.sh` peut tout
 * pousser par USB. Les adresses restent modifiables dans « Réglages avancés »,
 * repliés par défaut.
 */
class MainActivity : Activity() {

    private lateinit var prefs: Prefs
    private lateinit var audio: AudioController

    private lateinit var headline: TextView
    private lateinit var detail: TextView
    private lateinit var switchButton: Button
    private lateinit var logText: TextView
    private lateinit var advanced: View

    private lateinit var peerField: EditText
    private lateinit var headphonesField: EditText
    private lateinit var nameField: EditText
    private lateinit var tokenField: EditText
    private lateinit var autoStartBox: CheckBox

    private val handler = Handler(Looper.getMainLooper())
    /** Vrai quand l'utilisateur veut les écouteurs sur le PC : le bouton est armé. */
    private var headsetHere = false
    private val ticker = object : Runnable {
        override fun run() {
            refresh()
            handler.postDelayed(this, REFRESH_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        audio = AudioController(this)
        setContentView(R.layout.activity_main)

        headline = findViewById(R.id.text_headline)
        detail = findViewById(R.id.text_detail)
        switchButton = findViewById(R.id.btn_switch)
        logText = findViewById(R.id.text_log)
        advanced = findViewById(R.id.group_advanced)
        peerField = findViewById(R.id.edit_peer)
        headphonesField = findViewById(R.id.edit_headphones)
        nameField = findViewById(R.id.edit_name)
        tokenField = findViewById(R.id.edit_token)
        autoStartBox = findViewById(R.id.check_autostart)

        switchButton.setOnClickListener { onSwitch() }
        findViewById<Button>(R.id.btn_retry).setOnClickListener { relink() }
        findViewById<Button>(R.id.btn_advanced).setOnClickListener {
            advanced.visibility = if (advanced.visibility == View.VISIBLE) View.GONE else View.VISIBLE
            if (advanced.visibility == View.VISIBLE) fillFields()
        }
        findViewById<Button>(R.id.btn_save).setOnClickListener { saveSettings() }
        findViewById<Button>(R.id.btn_pick_peer).setOnClickListener {
            pickPairedDevice { address, name ->
                peerField.setText(address)
                prefs.peerName = name
            }
        }
        findViewById<Button>(R.id.btn_pick_headphones).setOnClickListener {
            pickPairedDevice { address, name ->
                headphonesField.setText(address)
                prefs.headphonesName = name
            }
        }

        fillFields()
        ensurePermissions()

        // Tout se configure ici : si une adresse manque, on la déduit des
        // appareils appairés. Aucune saisie demandée.
        if (AutoConfig.ensure(this, prefs, audio)) {
            fillFields()
            BluecoService.action(this, BluecoService.ACTION_STOP)
        }
        BluecoService.start(this)
    }

    override fun onResume() {
        super.onResume()
        BluecoService.listener = { runOnUiThread { render() } }
        AppLog.listener = { runOnUiThread { renderLog() } }
        handler.post(ticker)
    }

    override fun onPause() {
        handler.removeCallbacks(ticker)
        BluecoService.listener = null
        AppLog.listener = null
        super.onPause()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_PERMISSIONS) {
            if (grantResults.any { it != PackageManager.PERMISSION_GRANTED }) {
                toast(getString(R.string.needs_permission))
            } else {
                if (AutoConfig.ensure(this, prefs, audio)) fillFields()
                BluecoService.start(this)
            }
            render()
        }
    }

    // ------------------------------------------------------------------ //
    // Le geste unique
    // ------------------------------------------------------------------ //
    private fun onSwitch() {
        val message = if (headsetHere) {
            BluecoService.action(this, BluecoService.ACTION_GIVE)
            getString(R.string.working_give)
        } else {
            BluecoService.action(this, BluecoService.ACTION_TAKE)
            getString(R.string.working_take)
        }
        detail.text = message
    }

    private fun relink() {
        BluecoService.action(this, BluecoService.ACTION_STOP)
        BluecoService.start(this)
        detail.text = getString(R.string.relinking)
    }

    // ------------------------------------------------------------------ //
    // Affichage
    // ------------------------------------------------------------------ //
    private fun refresh() {
        // Interroge le PC (si la liaison est là) pour savoir qui tient les
        // écouteurs : c'est ce qui rend le gros bouton juste, sans réglage.
        if (BluecoService.isConnected) {
            Thread { BluecoService.requestRemoteStatus() }.start()
        }
        render()
    }

    private fun render() {
        val headphonesHere = prefs.headphonesMac.isNotEmpty() &&
            runCatching { audio.isConnected(prefs.headphonesMac) }.getOrDefault(false)
        val remote = BluecoService.remoteStatus
        val pcHolds = remote?.optBoolean("headphones_held") == true
        val linked = BluecoService.isConnected

        headsetHere = headphonesHere

        val headphoneName = prefs.headphonesName.ifEmpty { prefs.headphonesMac.ifEmpty { "écouteurs" } }
        headline.text = when {
            headphonesHere -> getString(R.string.headline_here, headphoneName)
            pcHolds -> getString(R.string.headline_pc)
            else -> getString(R.string.headline_unknown)
        }

        val pcName = prefs.peerName.ifEmpty { prefs.peerMac.ifEmpty { "PC" } }
        val linkLine = when {
            linked -> getString(R.string.link_ok, pcName)
            prefs.peerMac.isEmpty() -> getString(R.string.link_nopeer)
            else -> getString(R.string.link_search, pcName, BluecoService.lastState)
        }
        val hpLine = when {
            prefs.headphonesMac.isEmpty() -> getString(R.string.hp_none)
            headphonesHere -> getString(R.string.hp_here)
            pcHolds -> getString(R.string.hp_on_pc)
            else -> getString(R.string.hp_elsewhere)
        }
        detail.text = "$linkLine\n$hpLine"

        switchButton.isEnabled = prefs.peerMac.isNotEmpty() && prefs.headphonesMac.isNotEmpty()
        switchButton.text = if (headphonesHere) {
            getString(R.string.btn_switch_give)
        } else {
            getString(R.string.btn_switch_take)
        }

        renderLog()
    }

    private fun renderLog() {
        logText.text = AppLog.recent().takeLast(6).joinToString("\n")
    }

    private fun fillFields() {
        peerField.setText(prefs.peerMac)
        headphonesField.setText(prefs.headphonesMac)
        nameField.setText(prefs.deviceName)
        tokenField.setText(prefs.token)
        autoStartBox.isChecked = prefs.autoStart
    }

    private fun saveSettings() {
        prefs.peerMac = peerField.text.toString()
        prefs.headphonesMac = headphonesField.text.toString()
        prefs.deviceName = nameField.text.toString().ifBlank { "téléphone" }
        prefs.token = tokenField.text.toString()
        prefs.autoStart = autoStartBox.isChecked
        // Le service relit les réglages à chaque reconnexion : on le relance
        // proprement pour appliquer tout de suite un changement d'adresse.
        BluecoService.action(this, BluecoService.ACTION_STOP)
        BluecoService.start(this)
        toast(getString(R.string.saved))
        render()
    }

    // ------------------------------------------------------------------ //

    @SuppressLint("MissingPermission")
    private fun pickPairedDevice(onPick: (String, String) -> Unit) {
        if (!hasBluetoothPermission()) {
            ensurePermissions()
            toast(getString(R.string.needs_permission))
            return
        }
        val adapter = (getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
        val devices = try {
            adapter?.bondedDevices?.toList().orEmpty()
        } catch (_: SecurityException) {
            emptyList()
        }
        if (devices.isEmpty()) {
            toast(getString(R.string.no_paired))
            return
        }
        val labels = devices.map { "${AutoConfig.label(it)}  —  ${it.address}" }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle(R.string.paired_devices)
            .setItems(labels) { _, index ->
                val device = devices[index]
                onPick(device.address, AutoConfig.label(device))
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    private fun ensurePermissions() {
        val needed = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            needed += Manifest.permission.BLUETOOTH_CONNECT
            needed += Manifest.permission.BLUETOOTH_SCAN
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            needed += Manifest.permission.POST_NOTIFICATIONS
        }
        val missing = needed.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) requestPermissions(missing.toTypedArray(), REQUEST_PERMISSIONS)
    }

    private fun hasBluetoothPermission(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
            checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }

    private companion object {
        const val REQUEST_PERMISSIONS = 101
        const val REFRESH_MS = 2_000L
    }
}
