package ai.lastcore.android;

import android.app.Activity;
import android.app.ActivityManager;
import android.os.Bundle;
import android.os.PowerManager;
import android.os.BatteryManager;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Button;

/** Native platform diagnostics; no simulated node execution or registration. */
public final class MainActivity extends Activity {
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(32, 32, 32, 32);
        TextView status = new TextView(this);
        ActivityManager.MemoryInfo memory = new ActivityManager.MemoryInfo();
        ((ActivityManager) getSystemService(ACTIVITY_SERVICE)).getMemoryInfo(memory);
        int thermal = ((PowerManager) getSystemService(POWER_SERVICE)).getCurrentThermalStatus();
        BatteryManager battery = (BatteryManager) getSystemService(BATTERY_SERVICE);
        int level = battery.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
        String reason = AdmissionPolicy.rejection(false, memory.availMem, thermal, level, battery.isCharging());
        status.setText("LastCore Android native preview\nAvailable memory: " + memory.availMem / 1048576 + " MiB\nThermal status: " + thermal + "\nBattery: " + level + "%\nAdmission: " + reason + "\nNode offline: real model backend and transport integration are pending. No work is accepted.");
        layout.addView(status);
        Button start = new Button(this);
        start.setText("Start node — backend unavailable");
        start.setEnabled(false);
        layout.addView(start);
        setContentView(layout);
    }
}
