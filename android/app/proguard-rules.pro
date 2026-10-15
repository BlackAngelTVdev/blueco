# Les appels A2DP/HFP passent par la réflexion : on conserve les signatures
# utilisées pour que la minification ne casse pas la bascule.
-keepclassmembers class android.bluetooth.BluetoothA2dp {
    public boolean connect(android.bluetooth.BluetoothDevice);
    public boolean disconnect(android.bluetooth.BluetoothDevice);
}
-keepclassmembers class android.bluetooth.BluetoothHeadset {
    public boolean connect(android.bluetooth.BluetoothDevice);
    public boolean disconnect(android.bluetooth.BluetoothDevice);
}
-keep class dev.blueco.app.** { *; }
