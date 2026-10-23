package ai.lastcore.android;

import java.util.concurrent.atomic.AtomicBoolean;

/** Process-local ownership and cleanup ordering. No Android, network, credentials or model code. */
final class CloudWorkerHost {
    enum Phase { STARTING, WAITING, EXECUTING, STOPPING, STOPPED }
    interface Progress { void executing(boolean value); }
    interface Session {
        void execute() throws Exception;
        void stop();
        /** Persist stopped state, then release owned resources. May be retried; never reexecutes. */
        void close() throws Exception;
    }
    interface Factory { Session create(AtomicBoolean stop, Progress progress) throws Exception; }
    interface BeforeStart { void promote(long generation) throws Exception; }
    interface Observer { void changed(Snapshot snapshot); }

    static final class Snapshot {
        final long generation, revision;
        final Phase phase;
        final boolean busy, cleanupPending;
        final String error;
        Snapshot(long generation, long revision, Phase phase, boolean busy, boolean cleanupPending, String error) {
            this.generation=generation; this.revision=revision; this.phase=phase;
            this.busy=busy; this.cleanupPending=cleanupPending; this.error=error;
        }
    }

    private static final Object DEVICE = new Object();
    private static DeviceUse deviceOwner;
    private static long sequence, latestCloud;
    static final class DeviceUse implements AutoCloseable {
        final long generation;
        final boolean cloud;
        private boolean closed;
        private DeviceUse(long generation, boolean cloud) {this.generation=generation;this.cloud=cloud;}
        @Override public void close() {
            synchronized(DEVICE) {
                if(closed) return;
                if(deviceOwner!=this) throw new IllegalStateException("Device ownership changed");
                closed=true;deviceOwner=null;
            }
        }
    }
    private static DeviceUse acquire(boolean cloud) {
        synchronized(DEVICE) {
            if(deviceOwner!=null) return null;
            deviceOwner=new DeviceUse(++sequence,cloud);
            if(cloud) latestCloud=deviceOwner.generation;
            return deviceOwner;
        }
    }
    static DeviceUse acquireLocal() {return acquire(false);}
    static boolean deviceBusy() {synchronized(DEVICE) {return deviceOwner!=null;}}
    static boolean cloudBusy() {synchronized(DEVICE) {return deviceOwner!=null && deviceOwner.cloud;}}
    static boolean latestCloud(long generation) {synchronized(DEVICE) {return latestCloud==generation;}}

    private static final class Run {
        final DeviceUse use;
        final AtomicBoolean stop=new AtomicBoolean();
        volatile Session session;
        Factory factory;
        boolean executionDone, closing;
        String error;
        Run(DeviceUse use, Factory factory) {this.use=use;this.factory=factory;}
    }
    private final Observer observer;
    private Run active;
    private long revision;
    private Snapshot state=new Snapshot(0,0,Phase.STOPPED,false,false,null);
    CloudWorkerHost(Observer observer) {this.observer=observer;}
    synchronized Snapshot snapshot() {return state;}
    synchronized boolean canFinish(long generation) {return !state.busy && state.generation==generation && latestCloud(generation);}
    private void notifyObserver(Snapshot value) {
        try {observer.changed(value);} catch(RuntimeException ignored) { /* Observation never changes execution. */ }
    }
    private Snapshot set(Run run, Phase phase, boolean cleanup, String error) {
        state=new Snapshot(run.use.generation,++revision,phase,true,cleanup,error);return state;
    }
    long start(Factory factory, BeforeStart before) {
        final Run run;final Snapshot initial;
        synchronized(this) {
            if(active!=null) return -1;
            DeviceUse use=acquire(true);if(use==null) return -1;
            run=new Run(use,factory);active=run;initial=set(run,Phase.STARTING,false,null);
        }
        notifyObserver(initial);
        try {before.promote(run.use.generation);}
        catch(Exception | LinkageError error) {
            synchronized(this) {run.error="START_REJECTED";run.factory=null;run.executionDone=true;}
            cleanup(run);return run.use.generation;
        }
        new Thread(() -> execute(run),"lastcore-cloud-session").start();
        return run.use.generation;
    }
    private void execute(Run run) {
        try {
            Factory factory=run.factory;run.factory=null;
            if(!run.stop.get()) {
                run.session=factory.create(run.stop,value -> progress(run,value));
                if(run.stop.get()) run.session.stop();
                run.session.execute();
            }
        } catch(Exception | LinkageError error) {synchronized(this) {run.error="WORKER_FAILED";}}
        finally {
            synchronized(this) {run.executionDone=true;}
            cleanup(run);
        }
    }
    private void progress(Run run, boolean executing) {
        final Snapshot value;
        synchronized(this) {
            if(active!=run || run.stop.get()) return;
            value=set(run,executing?Phase.EXECUTING:Phase.WAITING,false,null);
        }
        notifyObserver(value);
    }
    void stop() {
        final Run run;final Snapshot value;final boolean retry;
        synchronized(this) {
            run=active;if(run==null) return;
            run.stop.set(true);
            retry=run.executionDone && !run.closing;
            value=set(run,Phase.STOPPING,state.cleanupPending,state.cleanupPending?"CLEANUP_REQUIRED":null);
        }
        notifyObserver(value);
        Session session=run.session;
        if(session!=null) {try {session.stop();} catch(RuntimeException ignored) { /* Still wait for execute/close. */ }}
        if(retry) new Thread(() -> cleanup(run),"lastcore-cloud-cleanup").start();
    }
    private void cleanup(Run run) {
        synchronized(this) {
            if(active!=run || !run.executionDone || run.closing) return;
            run.closing=true;
        }
        try {
            if(run.session!=null) run.session.close();
        } catch(Exception | LinkageError error) {
            final Snapshot failed;
            synchronized(this) {run.closing=false;failed=set(run,Phase.STOPPING,true,"CLEANUP_REQUIRED");}
            notifyObserver(failed);return; // No release, stopped event or notification removal without a checkpoint.
        }
        final Snapshot stopped;
        synchronized(this) {
            run.use.close();active=null;
            stopped=state=new Snapshot(run.use.generation,++revision,Phase.STOPPED,false,false,
                run.stop.get()?null:run.error);
        }
        notifyObserver(stopped);
    }
}
