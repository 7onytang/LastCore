package ai.lastcore.android;

import android.app.ActivityManager;
import android.content.Context;
import android.os.BatteryManager;
import android.os.PowerManager;

/** Conservative foreground budget; framework measurements are not hardware attestation or peak-memory proof. */
final class DevicePolicy implements CloudWorker.Policy {
    private final Context context;
    DevicePolicy(Context context) {this.context=context.getApplicationContext();}
    @Override public boolean allowed() {
        try {
            ActivityManager.MemoryInfo info=new ActivityManager.MemoryInfo();
            ((ActivityManager)context.getSystemService(Context.ACTIVITY_SERVICE)).getMemoryInfo(info);
            int thermal=((PowerManager)context.getSystemService(Context.POWER_SERVICE)).getCurrentThermalStatus();
            BatteryManager battery=(BatteryManager)context.getSystemService(Context.BATTERY_SERVICE);
            int level=battery.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
            return !info.lowMemory && info.availMem>=512L*1024*1024 && thermal>=0 && thermal<PowerManager.THERMAL_STATUS_SEVERE
                && level>=0 && level<=100 && (battery.isCharging() || level>=20);
        } catch(RuntimeException error) {return false;}
    }
}
