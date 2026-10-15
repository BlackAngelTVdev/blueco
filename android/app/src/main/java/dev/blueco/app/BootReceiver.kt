package dev.blueco.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Relance la liaison tout seul au démarrage du téléphone (et après une mise à
 * jour de l'appli), pour qu'il n'y ait jamais de « Démarrer le service » à
 * toucher : le PC peut basculer les écouteurs même si l'appli n'a pas été
 * ouverte depuis le démarrage.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            Intent.ACTION_LOCKED_BOOT_COMPLETED,
            -> {
                if (!Prefs(context).autoStart) {
                    AppLog.i("démarrage automatique désactivé : liaison non lancée")
                    return
                }
                AppLog.i("démarrage du téléphone : liaison relancée")
                BluecoService.start(context)
            }
        }
    }
}
