package ai.lastcore.android;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Binder;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import java.io.InputStream;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicBoolean;

/** Explicitly started, same-process owner. Intents/notifications never carry credentials. */
public final class CloudWorkerService extends Service {
    private static final String CHANNEL="lastcore-worker";
    private static final int NOTIFICATION=5301;
    private static final String HOLD="ai.lastcore.android.HOLD";
    private static final String STOP="ai.lastcore.android.STOP";
    private static final String GENERATION="generation";
    private final Handler main=new Handler(Looper.getMainLooper());
    private final CopyOnWriteArrayList<CloudWorkerHost.Observer> observers=new CopyOnWriteArrayList<>();
    private final CloudWorkerHost host=new CloudWorkerHost(snapshot -> main.post(() -> publish(snapshot)));
    private final LocalBinder binder=new LocalBinder();
    private boolean destroyed, promoted;
    private int latestStartId;

    static void createChannel(Context context) {
        NotificationManager manager=context.getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel(CHANNEL,"LastCore 节点运行",NotificationManager.IMPORTANCE_LOW));
    }
    static boolean notificationsVisible(Context context) {
        try {
            if(Build.VERSION.SDK_INT>=33 && context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)
                return false;
            NotificationManager manager=context.getSystemService(NotificationManager.class);
            NotificationChannel channel=manager.getNotificationChannel(CHANNEL);
            return manager.areNotificationsEnabled() && channel!=null && channel.getImportance()!=NotificationManager.IMPORTANCE_NONE;
        } catch(RuntimeException error) {return false;}
    }
    private Notification notification(CloudWorkerHost.Snapshot snapshot) {
        Intent stop=new Intent(this,CloudWorkerService.class).setAction(STOP)
            .setData(android.net.Uri.parse("lastcore-worker://stop/"+snapshot.generation)).putExtra(GENERATION,snapshot.generation);
        PendingIntent stopAction=PendingIntent.getService(this,0,stop,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        PendingIntent show=PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this,CHANNEL).setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("LastCore 云端节点").setContentText(status(snapshot))
            .setContentIntent(show).setOngoing(true).setOnlyAlertOnce(true).setCategory(Notification.CATEGORY_SERVICE)
            .setVisibility(Notification.VISIBILITY_PRIVATE).setDeleteIntent(stopAction)
            .addAction(new Notification.Action.Builder(android.R.drawable.ic_menu_close_clear_cancel,snapshot.cleanupPending?"重试停止确认":"停止",stopAction).build()).build();
    }
    static String status(CloudWorkerHost.Snapshot snapshot) {
        if(snapshot.cleanupPending) return "停止尚未确认，保留资源；可重试停止确认。";
        switch(snapshot.phase) {
            case STARTING:return "正在恢复并准备节点…";
            case WAITING:return "节点已就绪，等待整模型任务。";
            case EXECUTING:return "正在执行整模型任务。";
            case STOPPING:return "正在停止，等待原生执行和状态保存…";
            default:return snapshot.error==null?"节点已停止，未确认回报保留供下次恢复。":"节点无法继续，请检查模型、网络、通知和恢复状态。";
        }
    }
    private void publish(CloudWorkerHost.Snapshot value) {
        if(destroyed || value.revision!=host.snapshot().revision) return;
        if(promoted && value.busy) {
            if(notificationsVisible(this)) {
                try {getSystemService(NotificationManager.class).notify(NOTIFICATION,notification(value));}
                catch(RuntimeException error) {if(value.phase!=CloudWorkerHost.Phase.STOPPING) host.stop();}
            } else if(value.phase!=CloudWorkerHost.Phase.STOPPING) host.stop();
        }
        for(CloudWorkerHost.Observer observer:observers) {
            try {observer.changed(value);} catch(RuntimeException ignored) { /* UI observation cannot stop cleanup. */ }
        }
        if(!value.busy) finish(value.generation);
    }
    private void finish(long generation) {
        if(destroyed || !host.canFinish(generation)) return;
        if(promoted) {stopForeground(STOP_FOREGROUND_REMOVE);promoted=false;}
        if(latestStartId!=0) stopSelfResult(latestStartId);
    }
    @Override public void onCreate() {super.onCreate();createChannel(this);}
    @Override public IBinder onBind(Intent intent) {return binder;}
    @Override public int onStartCommand(Intent intent,int flags,int startId) {
        latestStartId=startId;
        String action=intent==null?null:intent.getAction();
        if(STOP.equals(action)) {
            host.stop(intent.getLongExtra(GENERATION,-1));
            if(!host.snapshot().busy) {if(promoted) {stopForeground(STOP_FOREGROUND_REMOVE);promoted=false;}stopSelfResult(startId);}
        } else if(HOLD.equals(action)) {
            long generation=intent.getLongExtra(GENERATION,-1);
            if(generation==host.snapshot().generation && !host.snapshot().busy) finish(generation);
            else if(!host.snapshot().busy) {if(promoted) {stopForeground(STOP_FOREGROUND_REMOVE);promoted=false;}stopSelfResult(startId);}
        } else if(!host.snapshot().busy) {
            // Null, stale or unknown intents can never create an execution session.
            if(promoted) {stopForeground(STOP_FOREGROUND_REMOVE);promoted=false;}
            stopSelfResult(startId);
        }
        return START_NOT_STICKY;
    }
    @Override public void onDestroy() {
        destroyed=true;observers.clear();host.stop();
        // Host retains its process gate and state lock until actual cleanup/checkpoint. No join on the main thread.
        super.onDestroy();
    }
    public final class LocalBinder extends Binder implements CloudBindingCoordinator.Peer {
        CloudWorkerHost.Snapshot snapshot() {return host.snapshot();}
        void observe(CloudWorkerHost.Observer observer) {observers.addIfAbsent(observer);observer.changed(host.snapshot());}
        void unobserve(CloudWorkerHost.Observer observer) {observers.remove(observer);}
        void stop() {host.stop();}
        @Override public long start(String origin,boolean enroll,String registrationToken) {
            if(Looper.myLooper()!=Looper.getMainLooper()) throw new IllegalStateException("Visible UI control required");
            if(destroyed || !notificationsVisible(CloudWorkerService.this)) return -1;
            final String verified;
            try {verified=ControllerEndpoint.origin(origin);if(enroll) CloudApi.credential(registrationToken);}
            catch(Exception error) {return -1;}
            return host.start((stop,progress) -> new WorkerSession(verified,enroll,registrationToken,stop,progress),generation -> {
                // Called synchronously only by the resumed Activity's explicit button/Binder command.
                startForegroundService(new Intent(CloudWorkerService.this,CloudWorkerService.class).setAction(HOLD).putExtra(GENERATION,generation));
                int type=Build.VERSION.SDK_INT>=34?ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE:0;
                startForeground(NOTIFICATION,notification(host.snapshot()),type);promoted=true;
            });
        }
    }
    private final class WorkerSession implements CloudWorkerHost.Session {
        private final String origin;
        private final boolean enroll;
        private final AtomicBoolean cancelled;
        private final CloudWorkerHost.Progress progress;
        private volatile CloudWorker worker;
        private AndroidCloudState disk;
        private String registrationToken;
        WorkerSession(String origin,boolean enroll,String token,AtomicBoolean stop,CloudWorkerHost.Progress progress) {
            this.origin=origin;this.enroll=enroll;this.registrationToken=token;this.cancelled=stop;this.progress=progress;
        }
        @Override public void stop() {cancelled.set(true);CloudWorker active=worker;if(active!=null) active.stop();}
        @Override public void execute() throws Exception {
            try {
                if(cancelled.get()) return;
                disk=new AndroidCloudState(getApplicationContext());
                String hash;
                try(java.util.zip.ZipFile apk=new java.util.zip.ZipFile(getApplicationInfo().sourceDir)) {
                    java.util.zip.ZipEntry entry=apk.getEntry("lib/arm64-v8a/liblastcore_local.so");
                    if(entry==null) throw new IllegalStateException("Native backend unavailable");
                    try(InputStream input=apk.getInputStream(entry)) {hash=JniBackend.hash(input,cancelled);}
                }
                worker=new CloudWorker(new CloudApi(origin),disk,new JniBackend(new ModelStore(getApplicationContext()).path(),hash),
                    () -> notificationsVisible(CloudWorkerService.this) && new DevicePolicy(getApplicationContext()).allowed(),
                    message -> {
                        if("Cloud job executing.".equals(message)) progress.executing(true);
                        else if("Cloud worker ready for one whole-model job at a time.".equals(message)
                                || "Cloud job finished; native execution stopped.".equals(message)) progress.executing(false);
                    },SystemClock::elapsedRealtimeNanos);
                if(cancelled.get()) worker.stop();
                if(enroll) worker.register(registrationToken);else worker.run(0);
            } finally {registrationToken=null;}
        }
        @Override public void close() throws Exception {
            // run() already awaited real native close. A failed checkpoint retains the lock/gate/notification for retry.
            if(worker!=null) worker.checkpointStopped();
            if(disk!=null) disk.close();
        }
    }
}
