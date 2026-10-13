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

/** Local and Controller whole-model work share one screen-bound executor. */
public final class MainActivity extends Activity {
    private static final int IMPORT_MODEL = 1;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private volatile AtomicBoolean cancelled = new AtomicBoolean();
    private volatile LocalRuntime runtime;
    private volatile CloudWorker cloud;
    private volatile boolean destroyed;
    private boolean busy;
    private TextView status, output;
    private EditText prompt, endpoint, registration;
    private Button load, generate, clear, cancel, checkConnection, registerCloud, startCloud;
    private ModelStore models;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        models = new ModelStore(this);
        getWindow().setStatusBarColor(color(R.color.lc_background));
        getWindow().setNavigationBarColor(color(R.color.lc_background));
        getWindow().getDecorView().setSystemUiVisibility(0);
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true); scroll.setBackgroundColor(color(R.color.lc_background));
        LinearLayout layout = column();
        layout.setPadding(dp(20), dp(24), dp(20), dp(32));
        TextView brand = label("LastCore", 28, R.color.lc_text);
        brand.setTypeface(android.graphics.Typeface.DEFAULT, android.graphics.Typeface.BOLD);
        layout.addView(brand);
        layout.addView(label("CONNECTED COMPUTE / ANDROID PREVIEW", 11, R.color.lc_accent));
        TextView title = label("你的设备，\n也能成为算力节点。", 26, R.color.lc_text);
        title.setPadding(0, dp(28), 0, dp(12)); layout.addView(title);
        layout.addView(label("本机推理与前台节点，共用一个清晰的工作空间。", 14, R.color.lc_muted));
        status = label("就绪 · 尚未执行操作", 13, R.color.lc_accent);
        status.setPadding(0, dp(18), 0, dp(18)); layout.addView(status);
        progress = new android.widget.ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setIndeterminate(true);
        progress.setIndeterminateTintList(android.content.res.ColorStateList.valueOf(color(R.color.lc_accent)));
        progress.setVisibility(android.view.View.GONE); layout.addView(progress);

        LinearLayout connection = card(layout, "01 / CONTROLLER", "连接工作空间", "使用 HTTPS 地址连接 Controller；连接检查不发送凭据或提示词。");
        endpoint = field(connection, "Controller 地址", "https://controller.example.com", true);
        endpoint.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        endpoint.setText(getPreferences(MODE_PRIVATE).getString("controller_origin", "https://8.134.161.123"));
        checkConnection = button(connection, "检查安全连接", this::checkController, false);

        LinearLayout local = card(layout, "02 / LOCAL INFERENCE", "在这台设备上生成", "SmolLM2 135M · 固定 GGUF · 提示词最多 512 字符");
        load = button(local, "导入固定模型", () -> {
            Intent picker = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            picker.setType("*/*"); picker.addCategory(Intent.CATEGORY_OPENABLE);
            startActivityForResult(picker, IMPORT_MODEL);
        }, false);
        prompt = field(local, "提示词", "输入一段简短的问题…", false);
        prompt.setMinLines(3); prompt.setMaxLines(6); prompt.setGravity(android.view.Gravity.TOP);
        prompt.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE | android.text.InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
        generate = button(local, "本机生成", this::generate, true);
        clear = button(local, "删除已导入的模型", () -> new android.app.AlertDialog.Builder(this)
            .setTitle("删除本机模型？").setMessage("仅删除本机导入的 GGUF；再次生成需要重新导入。")
            .setNegativeButton("保留", null).setPositiveButton("删除模型", (dialog, which) -> {
                if (!busy) { models.delete(); output.setText("已删除本机导入的模型。"); }
            }).show(), false);
        clear.setTextColor(color(R.color.lc_danger));

        LinearLayout result = card(layout, "03 / ACTIVITY", "运行状态与结果", "任务结果与操作反馈显示在这里。");
        output = label("尚未执行任务。导入模型后即可尝试本机推理。", 15, R.color.lc_text);
        output.setTextIsSelectable(true);
        output.setAccessibilityLiveRegion(android.view.View.ACCESSIBILITY_LIVE_REGION_POLITE);
        output.setPadding(dp(14), dp(14), dp(14), dp(14));
        output.setBackground(background(R.color.lc_background, 12, R.color.lc_border));
        result.addView(output);
        cancel = button(result, "停止当前操作", this::cancelWork, false); cancel.setEnabled(false);

        LinearLayout node = card(layout, "04 / CLOUD WORKER", "接入整模型任务", "先导入模型，再注册节点。节点仅在此页面打开时运行；离开页面会请求停止。");
        registration = field(node, "首次注册凭据", "仅首次注册需要填写", true);
        registration.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD);
        registration.setSaveEnabled(false); registration.setImportantForAutofill(android.view.View.IMPORTANT_FOR_AUTOFILL_NO);
        registerCloud = button(node, "注册云端节点", () -> cloudAction(true), false);
        startCloud = button(node, "启动前台节点", () -> cloudAction(false), true);
        TextView notice = label("本机提示词留在设备上。云端任务接收 Controller 提示词，并回传生成文本。\n\n预发布版本 · Android 真机验收待完成\nSmolLM2 Apache-2.0 · llama.cpp MIT · LastCore MIT", 12, R.color.lc_muted);
        notice.setPadding(0, dp(8), 0, 0); layout.addView(notice);
        // Target 35 enforces edge-to-edge: apply system/IME insets, never fixed pixel offsets.
        scroll.setOnApplyWindowInsetsListener((view, insets) -> {
            if (android.os.Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets safe = insets.getInsets(android.view.WindowInsets.Type.systemBars() | android.view.WindowInsets.Type.displayCutout() | android.view.WindowInsets.Type.ime());
                view.setPadding(safe.left, safe.top, safe.right, safe.bottom);
            } else {
                view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(), insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            }
            return insets;
        });
        scroll.addView(layout); setContentView(scroll); scroll.requestApplyInsets();
    }
    private android.widget.ProgressBar progress;
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private int color(int token) { return getColor(token); }
    private LinearLayout column() {
        LinearLayout layout = new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL); return layout;
    }
    private android.graphics.drawable.GradientDrawable background(int fill, int radius, int border) {
        android.graphics.drawable.GradientDrawable shape = new android.graphics.drawable.GradientDrawable();
        shape.setColor(color(fill)); shape.setCornerRadius(dp(radius)); shape.setStroke(dp(1), color(border)); return shape;
    }
    private TextView label(String text, int size, int token) {
        TextView view = new TextView(this); view.setText(text); view.setTextSize(size); view.setTextColor(color(token));
        view.setLineSpacing(dp(3), 1f); return view;
    }
    private LinearLayout card(LinearLayout parent, String overline, String title, String description) {
        LinearLayout card = column(); card.setPadding(dp(20), dp(20), dp(20), dp(20));
        card.setBackground(background(R.color.lc_surface, 20, R.color.lc_card_border));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2); params.bottomMargin = dp(16);
        parent.addView(card, params);
        card.addView(label(overline, 11, R.color.lc_accent));
        TextView heading = label(title, 21, R.color.lc_text);
        heading.setTypeface(android.graphics.Typeface.DEFAULT, android.graphics.Typeface.BOLD);
        heading.setPadding(0, dp(8), 0, dp(6)); heading.setAccessibilityHeading(true); card.addView(heading);
        TextView helper = label(description, 13, R.color.lc_muted); helper.setPadding(0, 0, 0, dp(14)); card.addView(helper);
        return card;
    }
    private EditText field(LinearLayout parent, String title, String hint, boolean single) {
        TextView name = label(title, 13, R.color.lc_muted); name.setPadding(0, dp(10), 0, dp(8)); parent.addView(name);
        EditText view = new EditText(this); view.setId(android.view.View.generateViewId()); name.setLabelFor(view.getId());
        view.setTextColor(color(R.color.lc_text)); view.setHintTextColor(color(R.color.lc_muted)); view.setTextSize(16);
        view.setSingleLine(single); view.setHint(hint); view.setMinHeight(dp(52)); view.setPadding(dp(14), dp(12), dp(14), dp(12));
        android.graphics.drawable.StateListDrawable states = new android.graphics.drawable.StateListDrawable();
        states.addState(new int[]{android.R.attr.state_focused}, background(R.color.lc_background, 10, R.color.lc_accent));
        states.addState(new int[]{}, background(R.color.lc_background, 10, R.color.lc_border)); view.setBackground(states);
        parent.addView(view, new LinearLayout.LayoutParams(-1, -2)); return view;
    }
    private Button button(LinearLayout parent, String title, Runnable action, boolean primary) {
        Button view = new Button(this); view.setText(title); view.setAllCaps(false); view.setTextSize(14);
        view.setMinHeight(dp(52)); view.setPadding(dp(12), dp(10), dp(12), dp(10));
        int fill = primary ? R.color.lc_accent : R.color.lc_raised;
        android.graphics.drawable.StateListDrawable states = new android.graphics.drawable.StateListDrawable();
        states.addState(new int[]{-android.R.attr.state_enabled}, background(R.color.lc_raised, 10, R.color.lc_card_border));
        states.addState(new int[]{}, background(fill, 10, primary ? R.color.lc_accent : R.color.lc_border));
        view.setBackground(new android.graphics.drawable.RippleDrawable(android.content.res.ColorStateList.valueOf(0x334e6941), states, null));
        int[][] selectors = {{-android.R.attr.state_enabled}, {}};
        view.setTextColor(new android.content.res.ColorStateList(selectors, new int[]{color(R.color.lc_muted), color(primary ? R.color.lc_on_accent : R.color.lc_text)}));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2); params.topMargin = dp(12); parent.addView(view, params);
        view.setOnClickListener(v -> action.run()); return view;
    }
    private void setBusy(boolean value) {
        busy = value; status.setText(value ? "处理中 · 可停止当前操作" : "就绪 · 请查看运行结果");
        progress.setVisibility(value ? android.view.View.VISIBLE : android.view.View.GONE);
        load.setEnabled(!value); generate.setEnabled(!value); clear.setEnabled(!value);
        prompt.setEnabled(!value); endpoint.setEnabled(!value); checkConnection.setEnabled(!value); cancel.setEnabled(value);
        registration.setEnabled(!value); registerCloud.setEnabled(!value); startCloud.setEnabled(!value);
    }
    private String localRejection() {
        ActivityManager.MemoryInfo memory = new ActivityManager.MemoryInfo();
        ((ActivityManager)getSystemService(ACTIVITY_SERVICE)).getMemoryInfo(memory);
        int thermal = ((PowerManager)getSystemService(POWER_SERVICE)).getCurrentThermalStatus();
        BatteryManager battery = (BatteryManager)getSystemService(BATTERY_SERVICE);
        int level = battery.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
        if (memory.lowMemory || memory.availMem < 512L*1024*1024) return "至少需要 512 MiB 可用内存。";
        if (thermal < 0 || thermal >= PowerManager.THERMAL_STATUS_SEVERE) return "设备温度过高或无法读取温度状态，请稍后重试。";
        if (level < 0 || level > 100 || (!battery.isCharging() && level < 20)) return "请先为设备充电，再尝试本机推理。";
        return null;
    }
    private void checkController() {
        if (busy) return;
        final String origin;
        try { origin = ControllerEndpoint.origin(endpoint.getText().toString()); }
        catch (Exception error) { output.setText("请输入不含路径或凭据的 HTTPS Controller 地址。"); return; }
        AtomicBoolean operation = new AtomicBoolean(); cancelled = operation;
        setBusy(true); output.setText("正在检查安全连接…");
        worker.execute(() -> {
            String result;
            try {
                String body = ControllerEndpoint.health(origin, operation);
                if (!"ok".equals(new org.json.JSONObject(body).optString("status")))
                    throw new IllegalStateException("Controller health status is invalid");
                if (operation.get()) result = "已取消连接检查。";
                else {
                    getPreferences(MODE_PRIVATE).edit().putString("controller_origin", origin).apply();
                    result = "安全连接已验证。请手动注册或启动前台节点。";
                }
            } catch (Exception error) { result = operation.get() ? "已取消连接检查。" : "安全连接失败，请检查地址、网络与设备时间。"; }
            complete(result);
        });
    }
    private void generate() {
        if (busy || CloudWorker.RUNNING.get()) return;
        String reason = localRejection(); if (reason != null) { output.setText(reason); return; }
        String input = prompt.getText().toString().trim();
        if (input.isEmpty() || input.length() > 512) { output.setText("请输入 1–512 字符的提示词。"); return; }
        AtomicBoolean operation = new AtomicBoolean(); cancelled = operation;
        setBusy(true); output.setText("正在校验模型并生成…");
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
                if (operation.get()) result = "操作已取消。";
                else if (result.isEmpty()) result = "模型未返回文本。";
            } catch (Exception | LinkageError error) {
                // No file paths, tokens, prompts or arbitrary provider exception text in output/logs.
                result = operation.get() ? "操作已取消。" : "本机推理失败，请检查模型文件或缩短提示词。";
            }
            complete(result);
        });
    }
    private void cloudAction(boolean enroll) {
        if (busy || CloudWorker.RUNNING.get()) return;
        final String origin, enrollment;
        try {
            origin = ControllerEndpoint.origin(endpoint.getText().toString());
            enrollment = enroll ? CloudApi.credential(registration.getText().toString().trim()) : null;
        } catch (Exception error) { output.setText("注册时请输入有效的 HTTPS 地址和注册凭据。"); return; }
        registration.setText("");
        AtomicBoolean operation = new AtomicBoolean(); cancelled = operation;
        setBusy(true); output.setText(enroll ? "正在校验并注册云端节点…" : "正在恢复并启动前台节点…");
        worker.execute(() -> {
            String result;
            try (AndroidCloudState disk = new AndroidCloudState(this)) {
                java.io.File model = models.path();
                String hash;
                try (java.util.zip.ZipFile apk = new java.util.zip.ZipFile(getApplicationInfo().sourceDir)) {
                    java.util.zip.ZipEntry entry = apk.getEntry("lib/arm64-v8a/liblastcore_local.so");
                    if (entry == null) throw new IllegalStateException("Native backend is unavailable");
                    try (InputStream input = apk.getInputStream(entry)) { hash = JniBackend.hash(input, operation); }
                }
                CloudWorker running = new CloudWorker(new CloudApi(origin), disk, new JniBackend(model, hash),
                    new DevicePolicy(this), message -> runOnUiThread(() -> {
                        if (!destroyed && !operation.get()) output.setText(message);
                    }));
                cloud = running;
                if (operation.get()) running.stop();
                if (enroll) running.register(enrollment); else running.run(0);
                result = enroll ? "云端节点已注册。请保持此页面打开并手动启动。" : "云端节点已停止，未确认的任务状态已保留以供恢复。";
            } catch (Exception | LinkageError error) {
                result = operation.get() ? "云端节点已停止，重新打开并启动可恢复待确认租约。" :
                    "云端节点无法继续，请检查模型、网络、凭据和恢复状态。注册结果未知或状态损坏时需人工恢复，请勿清除应用数据。";
            } finally { cloud = null; }
            complete(result);
        });
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != IMPORT_MODEL || result != RESULT_OK || data == null || data.getData() == null || busy) return;
        final android.net.Uri uri = data.getData();
        AtomicBoolean operation = new AtomicBoolean(); cancelled = operation;
        setBusy(true); output.setText("正在导入并校验模型…");
        worker.execute(() -> {
            String message;
            try (InputStream in = getContentResolver().openInputStream(uri)) {
                if (in == null) throw new IllegalStateException("Missing input");
                models.importModel(in, operation);
                message = "SmolLM2 模型校验成功，可开始本机推理。";
            } catch (Exception error) {
                message = operation.get() ? "模型导入已取消。" : "导入失败，请选择固定的 SmolLM2-135M-Instruct-Q4_K_M.gguf 文件。";
            }
            complete(message);
        });
    }
    private void complete(String result) {
        runOnUiThread(() -> { if (!destroyed) { output.setText(result); setBusy(false); } });
    }
    private void cancelWork() {
        if (busy && !destroyed) status.setText("正在请求停止 · 等待任务确认");
        cancelled.set(true); LocalRuntime active = runtime; if (active != null) active.cancel();
        CloudWorker running = cloud; if (running != null) running.stop();
    }
    @Override protected void onStop() { cancelWork(); super.onStop(); }
    @Override protected void onDestroy() {
        destroyed = true; cancelWork(); LocalRuntime active = runtime; if (active != null) active.close();
        worker.shutdown(); super.onDestroy();
    }
}
