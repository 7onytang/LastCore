package ai.lastcore.android;

import android.app.Activity;
import android.app.ActivityManager;
import android.content.Intent;
import android.content.ComponentName;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.IBinder;
import android.os.Handler;
import android.os.Looper;
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

/** Local work remains screen-bound; cloud work is observed/controlled through the same-process Service. */
public final class MainActivity extends Activity {
    private static final int IMPORT_MODEL = 1;
    private static final int NOTIFICATIONS = 2;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private volatile AtomicBoolean cancelled = new AtomicBoolean();
    private volatile LocalRuntime runtime;
    private CloudWorkerService.LocalBinder cloud;
    private CloudWorkerHost.Snapshot cloudState;
    private final Handler main=new Handler(Looper.getMainLooper());
    private boolean visible;
    private CloudBindingCoordinator bindings;
    private final java.util.Map<Long,ServiceConnection> serviceConnections=new java.util.HashMap<>();
    private long cloudBindingGeneration=-1;
    private volatile boolean destroyed;
    private boolean busy;
    private TextView status, output, modelStatus;
    private LinearLayout localPanel, cloudPanel, resultPanel, enrollmentPanel;
    private ScrollView scroll;
    private Button localMode, cloudMode, enrollmentToggle;
    private boolean cloudModeSelected;
    private EditText prompt, endpoint, registration;
    private Button load, generate, clear, cancel, checkConnection, registerCloud, startCloud;
    private ModelStore models;
    private CloudWorkerHost.Observer cloudObserver;
    private CloudBindingCoordinator createBindings() {
        return new CloudBindingCoordinator(new CloudBindingCoordinator.Port() {
            @Override public boolean bind(long generation,boolean create) {
                ServiceConnection connection=new ServiceConnection() {
                    @Override public void onServiceConnected(ComponentName name,IBinder binder) {
                        if(!bindings.accepts(generation) || destroyed) return;
                        if(cloud!=null && cloudObserver!=null) cloud.unobserve(cloudObserver);
                        cloud=(CloudWorkerService.LocalBinder)binder;cloudBindingGeneration=generation;
                        cloudObserver=value -> {if(bindings.accepts(generation) && cloudBindingGeneration==generation) cloudChanged(value);};
                        cloud.observe(cloudObserver);bindings.connected(generation,cloud);
                    }
                    @Override public void onServiceDisconnected(ComponentName name) {
                        if(!bindings.accepts(generation)) return;
                        if(cloud!=null && cloudObserver!=null) cloud.unobserve(cloudObserver);
                        cloud=null;cloudState=null;cloudObserver=null;cloudBindingGeneration=-1;bindings.disconnected(generation);
                        if(!destroyed) {output.setText("节点连接已断开；不能据此确认执行停止。");refreshControls();}
                    }
                    @Override public void onBindingDied(ComponentName name) {bindings.died(generation);}
                    @Override public void onNullBinding(ComponentName name) {bindings.died(generation);}
                };
                serviceConnections.put(generation,connection);
                boolean registered=false;
                try {registered=bindService(new Intent(MainActivity.this,CloudWorkerService.class),connection,create?BIND_AUTO_CREATE:0);}
                catch(RuntimeException ignored) { /* Report a fixed safe failure below. */ }
                if(!registered) serviceConnections.remove(generation);
                return registered;
            }
            @Override public void unbind(long generation) {
                if(cloudBindingGeneration==generation) {
                    if(cloud!=null && cloudObserver!=null) cloud.unobserve(cloudObserver);
                    cloud=null;cloudState=null;cloudObserver=null;cloudBindingGeneration=-1;
                }
                ServiceConnection connection=serviceConnections.remove(generation);
                if(connection!=null) unbindService(connection);
            }
            @Override public void submitted(long generation,long workerGeneration) {
                if(!bindings.accepts(generation) || destroyed) return;
                if(workerGeneration<0) output.setText("节点或本机操作仍在运行／停止，或通知不可见；请确认后重试。");
                else if(cloud!=null) cloudChanged(cloud.snapshot());
                refreshControls();
            }
            @Override public void rejected() {
                if(!destroyed) {output.setText("无法连接节点服务，请重试。");refreshControls();}
            }
        });
    }
    private final Runnable refreshGate=new Runnable() {
        @Override public void run() {if(visible && !destroyed) {refreshControls();main.postDelayed(this,250);}}
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        bindings=createBindings();
        models = new ModelStore(this);
        getWindow().setStatusBarColor(color(R.color.lc_background));
        getWindow().setNavigationBarColor(color(R.color.lc_background));
        getWindow().getDecorView().setSystemUiVisibility(0);
        scroll = new ScrollView(this);
        scroll.setFillViewport(true); scroll.setBackgroundColor(color(R.color.lc_background));
        LinearLayout layout = column();
        layout.setPadding(dp(20), dp(24), dp(20), dp(32));
        TextView brand = label("LastCore", 28, R.color.lc_text);
        brand.setTypeface(android.graphics.Typeface.DEFAULT, android.graphics.Typeface.BOLD);
        layout.addView(brand);
        layout.addView(label("CONNECTED COMPUTE / ANDROID PREVIEW", 11, R.color.lc_accent));
        status = label("就绪 · 尚未执行操作", 13, R.color.lc_accent);
        status.setPadding(0, dp(18), 0, dp(18)); layout.addView(status);
        progress = new android.widget.ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setIndeterminate(true);
        progress.setIndeterminateTintList(android.content.res.ColorStateList.valueOf(color(R.color.lc_accent)));
        progress.setVisibility(android.view.View.GONE); layout.addView(progress);

        LinearLayout model = card(layout, "准备模型", "SmolLM2 135M · 仅接受固定 GGUF 文件");
        modelStatus = label("", 13, R.color.lc_muted); model.addView(modelStatus);
        LinearLayout modelActions = new LinearLayout(this); modelActions.setOrientation(LinearLayout.HORIZONTAL); model.addView(modelActions);
        load = button(modelActions, "导入模型", () -> {
            if(busy || CloudWorkerHost.deviceBusy()) return;
            Intent picker = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            picker.setType("*/*"); picker.addCategory(Intent.CATEGORY_OPENABLE);
            startActivityForResult(picker, IMPORT_MODEL);
        }, false);
        clear = button(modelActions, "删除模型", () -> new android.app.AlertDialog.Builder(this)
            .setTitle("删除本机模型？").setMessage("再次生成前需要重新导入。云端凭据与恢复状态会保留。")
            .setNegativeButton("保留", null).setPositiveButton("删除", (dialog, which) -> {
                CloudWorkerHost.DeviceUse use=busy?null:CloudWorkerHost.acquireLocal();
                if(use==null) {output.setText("操作尚未结束，请等待停止确认。");return;}
                try {models.delete();output.setText("已删除本机模型。请重新导入后生成。");}
                finally {use.close();updateModelState();}
            }).show(), false);
        clear.setTextColor(color(R.color.lc_danger));
        LinearLayout.LayoutParams importButton = new LinearLayout.LayoutParams(0, -2, 1); importButton.topMargin = dp(12); importButton.rightMargin = dp(6); load.setLayoutParams(importButton);
        LinearLayout.LayoutParams deleteButton = new LinearLayout.LayoutParams(0, -2, 1); deleteButton.topMargin = dp(12); deleteButton.leftMargin = dp(6); clear.setLayoutParams(deleteButton);

        LinearLayout modes = new LinearLayout(this);
        modes.setOrientation(LinearLayout.HORIZONTAL);
        LinearLayout.LayoutParams modeRow = new LinearLayout.LayoutParams(-1, -2); modeRow.bottomMargin = dp(16);
        layout.addView(modes, modeRow);
        localMode = button(modes, "本机推理", () -> selectMode(false), false);
        cloudMode = button(modes, "云端节点", () -> selectMode(true), false);
        LinearLayout.LayoutParams left = new LinearLayout.LayoutParams(0, -2, 1); left.rightMargin = dp(6); localMode.setLayoutParams(left);
        LinearLayout.LayoutParams right = new LinearLayout.LayoutParams(0, -2, 1); right.leftMargin = dp(6); cloudMode.setLayoutParams(right);

        localPanel = card(layout, "本机推理", "提示词留在本机。导入模型后输入问题，再点击生成。");
        prompt = field(localPanel, "提示词", "输入问题…", false, R.id.input_prompt);
        prompt.setMinLines(3); prompt.setMaxLines(6); prompt.setGravity(android.view.Gravity.TOP);
        prompt.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE | android.text.InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
        generate = button(localPanel, "生成", this::generate, true);

        cloudPanel = card(layout, "云端节点", "输入 HTTPS 地址。首次使用先注册；已有节点直接启动，恢复待确认状态。");
        endpoint = field(cloudPanel, "Controller 地址", "https://controller.example.com", true, R.id.controller_endpoint);
        endpoint.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        endpoint.setText(getPreferences(MODE_PRIVATE).getString("controller_origin", "https://8.134.161.123"));
        checkConnection = button(cloudPanel, "检查连接", this::checkController, false);
        startCloud = button(cloudPanel, "启动节点", () -> cloudAction(false), true);
        enrollmentToggle = button(cloudPanel, "首次使用？注册节点", () -> {
            if (busy) return;
            boolean reveal = enrollmentPanel.getVisibility() != android.view.View.VISIBLE;
            enrollmentPanel.setVisibility(reveal ? android.view.View.VISIBLE : android.view.View.GONE);
            enrollmentToggle.setText(reveal ? "收起注册" : "首次使用？注册节点");
        }, false);
        enrollmentPanel = column(); cloudPanel.addView(enrollmentPanel);
        enrollmentPanel.setVisibility(android.view.View.GONE);
        registration = field(enrollmentPanel, "首次注册凭据", "已有节点无需填写", true, R.id.registration_credential);
        registration.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD);
        registration.setSaveEnabled(false); registration.setImportantForAutofill(android.view.View.IMPORTANT_FOR_AUTOFILL_NO);
        registerCloud = button(enrollmentPanel, "注册节点", () -> cloudAction(true), false);
        cloudPanel.addView(label("手动启动后由前台服务运行；离开页面不会停止云端节点。可在通知或此处停止；新任务前校验模型，恢复上报不依赖模型文件存在。", 12, R.color.lc_muted));

        resultPanel = card(layout, "结果", "");
        output = label("等待操作。", 15, R.color.lc_text);
        output.setTextIsSelectable(true);
        output.setAccessibilityLiveRegion(android.view.View.ACCESSIBILITY_LIVE_REGION_POLITE);
        output.setPadding(dp(14), dp(14), dp(14), dp(14));
        output.setBackground(background(R.color.lc_background, 10, R.color.lc_card_border));
        resultPanel.addView(output);
        cancel = button(resultPanel, "停止", this::cancelWork, false);
        cancel.setEnabled(false); cancel.setVisibility(android.view.View.GONE);
        updateModelState();
        selectMode(state != null && state.getBoolean("cloud_mode", false));
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
    private LinearLayout card(LinearLayout parent, String title, String description) {
        LinearLayout card = column(); card.setPadding(dp(20), dp(20), dp(20), dp(20));
        card.setBackground(background(R.color.lc_surface, 14, R.color.lc_card_border));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2); params.bottomMargin = dp(16);
        parent.addView(card, params);
        // One heading per card; avoid repeated metadata above every action.
        TextView heading = label(title, 21, R.color.lc_text);
        heading.setTypeface(android.graphics.Typeface.DEFAULT, android.graphics.Typeface.BOLD);
        heading.setPadding(0, dp(8), 0, dp(6)); heading.setAccessibilityHeading(true); card.addView(heading);
        if (!description.isEmpty()) {
            TextView helper = label(description, 13, R.color.lc_muted); helper.setPadding(0, 0, 0, dp(10)); card.addView(helper);
        }
        return card;
    }
    private EditText field(LinearLayout parent, String title, String hint, boolean single, int id) {
        TextView name = label(title, 13, R.color.lc_muted); name.setPadding(0, dp(10), 0, dp(8)); parent.addView(name);
        EditText view = new EditText(this); view.setId(id); name.setLabelFor(id);
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
        states.addState(new int[]{android.R.attr.state_selected}, background(R.color.lc_accent, 10, R.color.lc_accent));
        states.addState(new int[]{}, background(fill, 10, primary ? R.color.lc_accent : R.color.lc_border));
        view.setBackground(new android.graphics.drawable.RippleDrawable(android.content.res.ColorStateList.valueOf(0x334e6941), states, null));
        int[][] selectors = {{-android.R.attr.state_enabled}, {android.R.attr.state_selected}, {}};
        view.setTextColor(new android.content.res.ColorStateList(selectors, new int[]{color(R.color.lc_muted), color(R.color.lc_on_accent), color(primary ? R.color.lc_on_accent : R.color.lc_text)}));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2); params.topMargin = dp(12); parent.addView(view, params);
        view.setOnClickListener(v -> action.run()); return view;
    }
    private void selectMode(boolean cloudSelected) {
        if (busy) return;
        cloudModeSelected = cloudSelected;
        localPanel.setVisibility(cloudSelected ? android.view.View.GONE : android.view.View.VISIBLE);
        cloudPanel.setVisibility(cloudSelected ? android.view.View.VISIBLE : android.view.View.GONE);
        localMode.setSelected(!cloudSelected); cloudMode.setSelected(cloudSelected);
    }
    private void updateModelState() {
        // Presence is a UI hint only. Exact size/hash remain verified before inference.
        boolean present = models.path().isFile() || new java.io.File(models.path() + ".bak").isFile();
        modelStatus.setText(present ? "已导入 · 执行前重新校验" : "未导入 · 请先导入模型");
        load.setText(present ? "替换模型" : "导入模型"); load.setSelected(!present);
        boolean available=!busy && !CloudWorkerHost.deviceBusy() && !bindings.hasPending();
        load.setEnabled(available); clear.setEnabled(available && present);
        clear.setVisibility(present ? android.view.View.VISIBLE : android.view.View.GONE);
        generate.setEnabled(available && present);
        // Keep cloud start available without a model so saved leases can recover.
    }
    private void setBusy(boolean value) {
        busy=value;refreshControls();
        if (value) {
            android.view.View focus = getCurrentFocus();
            if (focus != null) {
                ((android.view.inputmethod.InputMethodManager)getSystemService(INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(focus.getWindowToken(), 0);
                focus.clearFocus();
            }
            scroll.post(() -> scroll.scrollTo(0, resultPanel.getTop()));
        }
    }
    private void refreshControls() {
        if(destroyed || status==null || generate==null) return;
        boolean cloudBusy=cloudState!=null && cloudState.busy;
        boolean occupied=busy || bindings.hasPending() || CloudWorkerHost.deviceBusy();
        if(cloudBusy) status.setText(CloudWorkerService.status(cloudState));
        else if(busy) status.setText(cancelled.get()?"正在停止 · 等待确认":"处理中");
        else status.setText(occupied?"另一个操作尚未结束 · 等待确认":"就绪");
        progress.setVisibility(occupied?android.view.View.VISIBLE:android.view.View.GONE);
        updateModelState();
        prompt.setEnabled(!occupied);endpoint.setEnabled(!occupied);checkConnection.setEnabled(!occupied);
        registration.setEnabled(!occupied);registerCloud.setEnabled(!occupied);startCloud.setEnabled(!occupied);
        localMode.setEnabled(!occupied);cloudMode.setEnabled(!occupied);enrollmentToggle.setEnabled(!occupied);
        boolean stopping=cloudBusy && cloudState.phase==CloudWorkerHost.Phase.STOPPING;
        boolean retry=stopping && cloudState.cleanupPending;
        cancel.setText(retry?"重试停止确认":stopping || busy && cancelled.get()?"正在停止…":"停止");
        cancel.setEnabled(bindings.hasPending() || cloudBusy && (!stopping || retry) || busy && !cancelled.get());
        cancel.setVisibility(occupied?android.view.View.VISIBLE:android.view.View.GONE);
    }
    private void cloudChanged(CloudWorkerHost.Snapshot value) {
        if(!visible || destroyed || cloud==null) return;
        CloudWorkerHost.Snapshot current=cloud.snapshot();
        if(value.generation!=current.generation || value.revision!=current.revision) return;
        cloudState=value;
        if(value.busy) {if(!busy) selectMode(true);output.setText(CloudWorkerService.status(value));}
        else if(!busy) output.setText(CloudWorkerService.status(value));
        refreshControls();
    }
    private void submitLocal(Runnable task) {
        CloudWorkerHost.DeviceUse use=CloudWorkerHost.acquireLocal();
        if(use==null) {busy=false;output.setText("操作尚未结束，请等待停止确认。");refreshControls();return;}
        try {worker.execute(() -> {try {task.run();} finally {use.close();main.post(this::refreshControls);}});}
        catch(java.util.concurrent.RejectedExecutionException error) {use.close();busy=false;refreshControls();}
    }
    @Override protected void onResume() {
        super.onResume();visible=true;bindings.resume();main.post(refreshGate);
    }
    @Override protected void onPause() {
        visible=false;bindings.pause();
        super.onPause();
    }
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] grants) {
        super.onRequestPermissionsResult(request,permissions,grants);
        if(request==NOTIFICATIONS && !destroyed)
            output.setText(grants.length>0 && grants[0]==PackageManager.PERMISSION_GRANTED?"通知已允许，请再次点击注册或启动。":"未允许通知，云端节点不会启动。");
    }
    @Override protected void onSaveInstanceState(Bundle state) {
        state.putBoolean("cloud_mode", cloudModeSelected); super.onSaveInstanceState(state);
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
        endpoint.setError(null);
        try { origin = ControllerEndpoint.origin(endpoint.getText().toString()); }
        catch (Exception error) { endpoint.setError("请输入有效的 HTTPS 地址，不包含路径或凭据"); endpoint.requestFocus(); return; }
        AtomicBoolean operation = new AtomicBoolean(); cancelled = operation;
        setBusy(true); output.setText("正在检查安全连接…");
        submitLocal(() -> {
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
        if (busy || CloudWorkerHost.deviceBusy()) return;
        String reason = localRejection(); if (reason != null) { output.setText(reason); return; }
        prompt.setError(null);
        String input = prompt.getText().toString().trim();
        if (input.isEmpty() || input.length() > 512) { prompt.setError("请输入 1–512 字符"); prompt.requestFocus(); return; }
        AtomicBoolean operation = new AtomicBoolean(); cancelled = operation;
        setBusy(true); output.setText("正在校验模型并生成…");
        submitLocal(() -> {
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
        if(!visible || busy || bindings.hasPending() || CloudWorkerHost.deviceBusy()) return;
        final String origin, credential;
        endpoint.setError(null);registration.setError(null);
        try {origin=ControllerEndpoint.origin(endpoint.getText().toString());}
        catch(Exception error) {endpoint.setError("请输入有效的 HTTPS 地址");endpoint.requestFocus();return;}
        CloudWorkerService.createChannel(this);
        if(!CloudWorkerService.notificationsVisible(this)) {
            if(android.os.Build.VERSION.SDK_INT>=33 && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)
                requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS},NOTIFICATIONS);
            output.setText("启动云端节点需要可见的工作通知；请允许通知权限和运行通知渠道后重试。");return;
        }
        try {credential=enroll?CloudApi.credential(registration.getText().toString().trim()):null;}
        catch(Exception error) {registration.setError("首次注册需要有效凭据");registration.requestFocus();return;}
        registration.setText("");
        output.setText(enroll?"正在连接服务并注册节点…":"正在连接前台节点服务…");
        bindings.request(new CloudBindingCoordinator.Command(origin,enroll,credential));refreshControls();
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != IMPORT_MODEL || result != RESULT_OK || data == null || data.getData() == null || busy) return;
        final android.net.Uri uri = data.getData();
        AtomicBoolean operation = new AtomicBoolean(); cancelled = operation;
        setBusy(true); output.setText("正在导入并校验模型…");
        submitLocal(() -> {
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
    private void cancelLocalWork() {
        if (busy && !destroyed) {
            status.setText("正在停止 · 等待确认"); cancel.setText("正在停止…"); cancel.setEnabled(false);
        }
        cancelled.set(true); LocalRuntime active = runtime; if (active != null) active.cancel();
    }
    private void cancelWork() {
        cancelLocalWork();
        bindings.cancelPending();
        if(cloud!=null) cloud.stop();
        refreshControls();
    }
    @Override protected void onStop() {
        visible=false;main.removeCallbacks(refreshGate);cancelLocalWork();
        bindings.pause();
        super.onStop();
    }
    @Override protected void onDestroy() {
        destroyed = true; cancelLocalWork(); LocalRuntime active = runtime; if (active != null) active.close();
        worker.shutdown(); super.onDestroy();
    }
}
