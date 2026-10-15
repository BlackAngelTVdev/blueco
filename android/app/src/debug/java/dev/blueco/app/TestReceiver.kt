package dev.blueco.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Pilotage depuis le PC, **uniquement dans la variante debug**.
 *
 * Permet à `scripts/setup-blueco.sh` (et à `adb`) de déclencher les mêmes
 * actions que les boutons de l'écran, pour vérifier la bascule de bout en bout
 * sans toucher au téléphone :
 *
 * ```
 * adb shell am broadcast -a dev.blueco.app.TAKE   -n dev.blueco.app.debug/dev.blueco.app.TestReceiver
 * ```
 *
 * Ce récepteur n'existe pas dans l'APK de production (voir
 * `src/debug/AndroidManifest.xml`), et il n'exécute que des actions locales :
 * aucun ordre n'est accepté depuis le réseau.
 */
class TestReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            BluecoService.ACTION_TAKE -> forward(context, BluecoService.ACTION_TAKE)
            BluecoService.ACTION_GIVE -> forward(context, BluecoService.ACTION_GIVE)
            BluecoService.ACTION_STATUS -> forward(context, BluecoService.ACTION_STATUS)
            BluecoService.ACTION_STOP -> forward(context, BluecoService.ACTION_STOP)
            ACTION_START -> {
                AppLog.i("pilotage à distance : démarrage")
                BluecoService.start(context)
            }
            else -> AppLog.w("pilotage à distance : action ignorée (${intent.action})")
        }
    }

    private fun forward(context: Context, action: String) {
        AppLog.i("pilotage à distance : $action")
        BluecoService.action(context, action)
    }

    private companion object {
        const val ACTION_START = "dev.blueco.app.START"
    }
}
