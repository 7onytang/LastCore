package ai.lastcore.android;

import android.app.Activity;
import android.app.ActivityManager;
import android.content.Intent;
import android.os.Bundle;
import android.os.PowerManager;
import android.os.BatteryManager;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.EditText;
import android.widget.Button;
import java.io.InputStream;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/** Local inference stays screen-bound; distributed-node admission remains closed. */
public final class MainActivity extends Activity {
    private static final int IMPORT_MODEL = 1;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private volatile AtomicBoolean cancelled = new AtomicBoolean();
    private volatile LocalRuntime runtime;
    private volatile boolean destroyed;
    private boolean busy;
    private TextView status, output;
    private EditText prompt, endpoint;
    private Button load, generate, clear, cancel, checkConnection;
    private ModelStore models;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        models = new ModelStore(this);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL); layout.setPadding(32, 32, 32, 32);
        status = new TextView(this);
        status.setText("LastCore Android Preview\nLocal model: SmolLM2 135M\nChoose the pinned GGUF file to try inference on this device.\nDistributed node: offline; Controller connection is pending.");
        layout.addView(status);
        endpoint = new EditText(this); endpoint.setSingleLine(true); endpoint.setHint("HTTPS Controller URL");
        endpoint.setText(getPreferences(MODE_PRIVATE).getString("controller_origin", "https://8.134.161.123"));
        layout.addView(endpoint);
        checkConnection = button(layout, "Check Controller connection", this::checkController);
        load = button(layout, "Import model", () -> {
            Intent picker = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            picker.setType("*/*"); picker.addCategory(Intent.CATEGORY_OPENABLE);
            startActivityForResult(picker, IMPORT_MODEL);
        });
        prompt = new EditText(this); prompt.setHint("Enter a short prompt"); prompt.setMaxLines(4); layout.addView(prompt);
        generate = button(layout, "Generate locally", this::generate);
        cancel = button(layout, "Cancel", this::cancelWork); cancel.setEnabled(false);
        clear = button(layout, "Delete imported model", () -> { models.delete(); output.setText("Imported model deleted."); });
        output = new TextView(this); layout.addView(output);
        Button start = button(layout, "Start distributed node — unavailable", () -> {}); start.setEnabled(false);
        TextView notice = new TextView(this);
        notice.setText("Model and prompts stay on this device. Leaving this screen requests cancellation.\nModel: Apache-2.0 · llama.cpp: MIT · LastCore: MIT");
        layout.addView(notice);
        ScrollView scroll = new ScrollView(this); scroll.addView(layout); setContentView(scroll);
    }
    private Button button(LinearLayout layout, String label, Runnable action) {
        Button b = new Button(this); b.setText(label); b.setOnClickListener(v -> action.run()); layout.addView(b); return b;
    }
    private void setBusy(boolean value) {
        busy = value; load.setEnabled(!value); generate.setEnabled(!value); clear.setEnabled(!value);
        prompt.setEnabled(!value); endpoint.setEnabled(!value); checkConnection.setEnabled(!value); cancel.setEnabled(value);
    }
    private String localRejection() {
        ActivityManager.MemoryInfo memory = new ActivityManager.MemoryInfo();
        ((ActivityManager)getSystemService(ACTIVITY_SERVICE)).getMemoryInfo(memory);
        int thermal = ((PowerManager)getSystemService(POWER_SERVICE)).getCurrentThermalStatus();
        BatteryManager battery = (BatteryManager)getSystemService(BATTERY_SERVICE);
        int level = battery.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
        if (memory.lowMemory || memory.availMem < 512L*1024*1024) return "At least 512 MiB available memory is required.";
        if (thermal < 0 || thermal >= PowerManager.THERMAL_STATUS_SEVERE) return "Device is too warm or thermal status is unavailable.";
        if (level < 0 || level > 100 || (!battery.isCharging() && level < 20)) return "Charge the device before local inference.";
        return null;
    }
    private void checkController() {
        if (busy) return;
        final String origin;
        try { origin = ControllerEndpoint.origin(endpoint.getText().toString()); }
        catch (Exception error) { output.setText("Enter an HTTPS Controller URL without a path or credentials."); return; }
        AtomicBoolean operation = new AtomicBoolean(); cancelled = operation;
        setBusy(true); output.setText("Checking secure Controller connection…");
        worker.execute(() -> {
            String result;
            try {
                String body = ControllerEndpoint.health(origin, operation);
                if (!"ok".equals(new org.json.JSONObject(body).optString("status")))
                    throw new IllegalStateException("Controller health status is invalid");
                if (operation.get()) result = "Connection check cancelled.";
                else {
                    getPreferences(MODE_PRIVATE).edit().putString("controller_origin", origin).apply();
                    result = "Secure Controller connection verified. This preview does not accept cloud jobs yet.";
                }
            } catch (Exception error) { result = operation.get() ? "Connection check cancelled." : "Secure connection failed. Check the URL, network and device clock."; }
            complete(result);
        });
    }
    private void generate() {
        if (busy) return;
        String reason = localRejection(); if (reason != null) { output.setText(reason); return; }
        String input = prompt.getText().toString().trim();
        if (input.isEmpty() || input.length() > 512) { output.setText("Enter a prompt with 1–512 characters."); return; }
        AtomicBoolean operation = new AtomicBoolean(); cancelled = operation;
        setBusy(true); output.setText("Verifying model and generating…");
        worker.execute(() -> {
            String result;
            try {
                java.io.File model = models.verified(operation);
                if (operation.get()) throw new IllegalStateException("Operation cancelled");
                try (LocalRuntime session = new LocalRuntime()) {
                    runtime = session;
                    if (operation.get()) session.cancel();
                    result = session.generate(model, input, 64);
                } finally { runtime = null; }
                if (operation.get()) result = "Operation cancelled.";
                else if (result.isEmpty()) result = "The model returned no text.";
            } catch (Exception | LinkageError error) {
                // No file paths, tokens, prompts or arbitrary provider exception text in output/logs.
                result = operation.get() ? "Operation cancelled." : "Local inference failed. Check the model file and shorten the prompt.";
            }
            complete(result);
        });
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != IMPORT_MODEL || result != RESULT_OK || data == null || data.getData() == null || busy) return;
        final android.net.Uri uri = data.getData();
        AtomicBoolean operation = new AtomicBoolean(); cancelled = operation;
        setBusy(true); output.setText("Importing and verifying model…");
        worker.execute(() -> {
            String message;
            try (InputStream in = getContentResolver().openInputStream(uri)) {
                if (in == null) throw new IllegalStateException("Missing input");
                models.importModel(in, operation);
                message = "Verified SmolLM2 model is ready for local inference.";
            } catch (Exception error) {
                message = operation.get() ? "Import cancelled." : "Import failed. Select the exact SmolLM2-135M-Instruct-Q4_K_M.gguf file.";
            }
            complete(message);
        });
    }
    private void complete(String result) {
        runOnUiThread(() -> { if (!destroyed) { output.setText(result); setBusy(false); } });
    }
    private void cancelWork() {
        cancelled.set(true); LocalRuntime active = runtime; if (active != null) active.cancel();
    }
    @Override protected void onStop() { cancelWork(); super.onStop(); }
    @Override protected void onDestroy() {
        destroyed = true; cancelWork(); LocalRuntime active = runtime; if (active != null) active.close();
        worker.shutdown(); super.onDestroy();
    }
}
