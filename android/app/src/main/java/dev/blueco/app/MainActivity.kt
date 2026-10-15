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
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast

/** Écran unique : état, deux gros boutons de bascule et les réglages. */
class MainActivity : Activity() {

    private lateinit var prefs: Prefs
    private lateinit var statusText: TextView
    private lateinit var logText: TextView
    private lateinit var peerField: EditText
    private lateinit var headphonesField: EditText
    private lateinit var nameField: EditText
    private lateinit var tokenField: EditText
    private lateinit var autoStartBox: CheckBox

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        setContentView(R.layout.activity_main)

        statusText = findViewById(R.id.text_status)
        logText = findViewById(R.id.text_log)
        peerField = findViewById(R.id.edit_peer)
        headphonesField = findViewById(R.id.edit_headphones)
        nameField = findViewById(R.id.edit_name)
        tokenField = findViewById(R.id.edit_token)
        autoStartBox = findViewById(R.id.check_autostart)

        peerField.setText(prefs.peerMac)
        headphonesField.setText(prefs.headphonesMac)
        nameField.setText(prefs.deviceName)
        tokenField.setText(prefs.token)
        autoStartBox.isChecked = prefs.autoStart

        findViewById<Button>(R.id.btn_save).setOnClickListener { saveSettings() }
        findViewById<Button>(R.id.btn_start).setOnClickListener { BluecoService.start(this) }
        findViewById<Button>(R.id.btn_stop).setOnClickListener {
            BluecoService.action(this, BluecoService.ACTION_STOP)
        }
        findViewById<Button>(R.id.btn_take).setOnClickListener {
            BluecoService.action(this, BluecoService.ACTION_TAKE)
        }
        findViewById<Button>(R.id.btn_give).setOnClickListener {
            BluecoService.action(this, BluecoService.ACTION_GIVE)
        }
        findViewById<Button>(R.id.btn_refresh).setOnClickListener { refreshStatus() }
        findViewById<Button>(R.id.btn_pick_peer).setOnClickListener {
            pickPairedDevice { address -> peerField.setText(address) }
        }
        findViewById<Button>(R.id.btn_pick_headphones).setOnClickListener {
            pickPairedDevice { address -> headphonesField.setText(address) }
        }

        ensurePermissions()
        if (prefs.autoStart) {
            BluecoService.start(this)
        }
        refreshStatus()
    }

    override fun onResume() {
        super.onResume()
        BluecoService.listener = { message -> runOnUiThread { statusText.text = message } }
        AppLog.listener = { runOnUiThread { renderLog() } }
        renderLog()
    }

    override fun onPause() {
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
            val denied = grantResults.any { it != PackageManager.PERMISSION_GRANTED }
            if (denied) {
                toast(getString(R.string.needs_permission))
            }
            refreshStatus()
        }
    }

    // ------------------------------------------------------------------ //

    private fun saveSettings() {
        prefs.peerMac = peerField.text.toString()
        prefs.headphonesMac = headphonesField.text.toString()
        prefs.deviceName = nameField.text.toString().ifBlank { "téléphone" }
        prefs.token = tokenField.text.toString()
        prefs.autoStart = autoStartBox.isChecked
        // Le service relit les réglages à chaque reconnexion : on le relance
        // proprement pour appliquer immédiatement un changement d'adresse.
        BluecoService.action(this, BluecoService.ACTION_STOP)
        BluecoService.start(this)
        toast(getString(R.string.saved))
    }

    private fun renderLog() {
        logText.text = AppLog.recent().takeLast(12).joinToString("\n")
    }

    private fun refreshStatus() {
        val audio = AudioController(this)
        val lines = mutableListOf<String>()
        lines += "PC          : ${prefs.peerMac.ifEmpty { "non configuré" }}"
        lines += "écouteurs   : ${prefs.headphonesMac.ifEmpty { "non configurés" }}"
        if (hasBluetoothPermission()) {
            val connected = audio.connectedAudioDevices()
                .map { device -> device.name ?: device.address }
            lines += "audio actif : ${if (connected.isEmpty()) "aucun" else connected.joinToString(", ")}"
        } else {
            lines += getString(R.string.needs_permission)
            ensurePermissions()
        }
        statusText.text = lines.joinToString("\n")
    }

    @SuppressLint("MissingPermission")
    private fun pickPairedDevice(onPick: (String) -> Unit) {
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
            toast("aucun appareil appairé : appairez d'abord le PC et les écouteurs")
            return
        }
        val labels = devices.map { "${it.name ?: "?"}  —  ${it.address}" }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle(R.string.paired_devices)
            .setItems(labels) { _, index -> onPick(devices[index].address) }
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
        if (missing.isNotEmpty()) {
            requestPermissions(missing.toTypedArray(), REQUEST_PERMISSIONS)
        }
    }

    private fun hasBluetoothPermission(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
            checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }

    private companion object {
        const val REQUEST_PERMISSIONS = 101
    }
}
