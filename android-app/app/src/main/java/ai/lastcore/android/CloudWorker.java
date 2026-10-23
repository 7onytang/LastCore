package ai.lastcore.android;

import org.json.JSONObject;
import java.security.SecureRandom;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.LongSupplier;

/** Single whole-model worker; its host owns lifetime, credentials lock and positive cleanup. */
final class CloudWorker {
    static final AtomicBoolean RUNNING = new AtomicBoolean();
    interface Policy { boolean allowed(); }
    interface Backend {
        void verify(AtomicBoolean cancelled) throws Exception;
        String identity();
        Execution start(String prompt, int tokens, int timeoutMillis, AtomicBoolean cancelled);
    }
    interface Execution {
        void cancel(); boolean stopped(); void awaitStopped() throws InterruptedException;
        String text(); boolean success(); double elapsed();
    }
    interface Events { void status(String value); }
    private final CloudApi api;
    private final CloudState disk;
    private final Backend backend;
    private final Policy policy;
    private final Events events;
    private final LeaseClock clock;
    private JSONObject state;
    private final AtomicBoolean stop = new AtomicBoolean();
    private volatile Execution active;
    CloudWorker(CloudApi api, CloudState disk, Backend backend, Policy policy, Events events) throws Exception {
        this(api,disk,backend,policy,events,System::nanoTime);
    }
    CloudWorker(CloudApi api, CloudState disk, Backend backend, Policy policy, Events events, LongSupplier clock) throws Exception {
        this.api=api; this.disk=disk; this.backend=backend; this.policy=policy; this.events=events;
        this.clock=new LeaseClock(clock);
        state=disk.load();
        if (state.has("origin") && !state.getString("origin").equals(api.origin))
            throw new IllegalStateException("Saved credentials belong to another Controller");
    }
    void stop() { stop.set(true); Execution running=active; if(running!=null) running.cancel(); }
    void checkpointStopped() throws Exception {
        if(active!=null || RUNNING.get()) throw new IllegalStateException("Worker cleanup is not complete");
        persist();
    }
    private static void await(Execution execution) {
        boolean interrupted=false;
        while(!execution.stopped()) {
            try {execution.awaitStopped();} catch(InterruptedException error) {interrupted=true;}
        }
        if(interrupted) Thread.currentThread().interrupt();
    }
    private static void join(Thread thread) {
        boolean interrupted=false;
        while(thread.isAlive()) {try {thread.join();} catch(InterruptedException error) {interrupted=true;}}
        if(interrupted) Thread.currentThread().interrupt();
    }
    private void persist() throws Exception { disk.save(state); }
    private String node() throws Exception { return CloudApi.id(state.getJSONObject("credential").getString("node_id")); }
    private String token() throws Exception { return CloudApi.credential(state.getJSONObject("credential").getString("node_token")); }
    private static String key() {
        byte[] random=new byte[32]; new SecureRandom().nextBytes(random); StringBuilder text=new StringBuilder();
        for(byte b:random) text.append(String.format(java.util.Locale.ROOT,"%02x",b&255));
        java.util.Arrays.fill(random,(byte)0); return text.toString();
    }
    void register(String registrationToken) throws Exception {
        if(!policy.allowed()) throw new IllegalStateException("Device budget is unavailable");
        backend.verify(stop);
        if(stop.get()) throw new IllegalStateException("Worker stopped");
        if(state.has("credential")) { capability(); return; }
        if(state.optBoolean("registration_pending",false)) throw new IllegalStateException("Registration outcome is unknown; do not register again blindly");
        state.put("origin",api.origin).put("registration_pending",true); persist();
        JSONObject credential;
        try {
            credential=api.post("/v1/nodes/register", registrationToken, new JSONObject()
                .put("name","android-real-model-worker").put("platform","Android-whole-model-worker")
                .put("endpoint","http://127.0.0.1:1").put("available_memory_mb",512)
                .put("compute_weight",1).put("accepting_tasks",false),201);
        } catch(CloudApi.Failure error) {
            if(error.status==401 || error.status==403 || error.status==422) {state.remove("registration_pending");persist();}
            throw error;
        }
        CloudApi.id(credential.getString("node_id")); CloudApi.credential(credential.getString("node_token"));
        state.put("credential",credential); state.remove("registration_pending"); persist();
        capability(); events.status("Cloud worker registered. Start explicitly to accept whole-model jobs.");
    }
    private void capability() throws Exception {
        api.node(node(),"capability",token(),new JSONObject().put("model_id",CloudApi.MODEL)
            .put("model_sha256",CloudApi.MODEL_SHA).put("backend_commit",CloudApi.BACKEND).put("backend_sha256",backend.identity()));
    }
    private void deliver(JSONObject job, JSONObject outcome) throws Exception {
        try { api.lease(job,node(),token(),"report",outcome); }
        catch(CloudApi.Failure error) {
            if(error.status!=409) throw error; // Unknown transport outcome retains the durable stopped result for exact retry.
            api.lease(job,node(),token(),"stop-ack",new JSONObject());
        }
        state.remove("ledger"); state.remove("claim_pending"); persist();
    }
    private void recover() throws Exception {
        JSONObject ledger=state.optJSONObject("ledger");
        if(ledger!=null) {
            JSONObject job=ledger.getJSONObject("job");
            JSONObject outcome=ledger.optJSONObject("outcome");
            // The exclusive process lock is held; the old process/worker's JNI cannot still be running.
            // Same-process lock release is permitted only after awaitStopped(). Never execute this lease again.
            if(outcome==null) outcome=new JSONObject().put("status","failed").put("error_code","worker_restart");
            deliver(job,outcome);
        }
    }
    private static long delta(JSONObject job,String field,long sent) throws Exception {
        return LeaseClock.deadline(job.getDouble(field),job.getDouble("server_time"),sent);
    }
    private void validateJob(JSONObject job) throws Exception {
        CloudApi.id(job.getString("id"));
        if(!job.getString("worker_node_id").equals(node()) || job.getLong("lease_epoch")<1)
            throw new IllegalStateException("Foreign lease");
        JSONObject request=job.getJSONObject("request");
        String prompt=request.getString("prompt"); int tokens=request.getInt("max_tokens");
        double timeout=request.getDouble("execution_timeout_seconds");
        if(!CloudApi.MODEL.equals(request.getString("model_id")) || prompt.trim().isEmpty()
            || prompt.getBytes(java.nio.charset.StandardCharsets.UTF_8).length>4096 || tokens<1 || tokens>64
            || !Double.isFinite(timeout) || timeout<.01 || timeout>90)
            throw new IllegalStateException("Job budget mismatch");
    }
    private void execute(JSONObject job) throws Exception {
        validateJob(job);
        JSONObject saved=new JSONObject().put("id",job.getString("id")).put("lease_epoch",job.getLong("lease_epoch"))
            .put("execution_key",key());
        JSONObject ledger=new JSONObject().put("job",saved).put("phase","claimed");
        state.put("ledger",ledger); state.remove("claim_pending"); persist();
        api.lease(saved,node(),token(),"report",new JSONObject().put("status","running"));
        long sent=clock.now();
        JSONObject renewed=api.lease(saved,node(),token(),"renew",new JSONObject());
        AtomicLong leaseDeadline=new AtomicLong(delta(renewed,"lease_expires",sent));
        long overallDeadline=delta(renewed,"deadline",sent);
        AtomicBoolean cancelled=new AtomicBoolean(stop.get() || renewed.optBoolean("cancel_requested",false));
        AtomicBoolean guarding=new AtomicBoolean(true);
        AtomicLong executionDeadline=new AtomicLong(Long.MAX_VALUE);
        AtomicLong cause=new AtomicLong(cancelled.get()?1:0); // 1: local stop/cancel, 2: lease, 3: execution/overall
        Thread guard=new Thread(() -> {
            while(guarding.get()) {
                long now=clock.now();
                if(stop.get() || cancelled.get() || now>=leaseDeadline.get() || now>=overallDeadline || now>=executionDeadline.get()) {
                    if(now>=overallDeadline || now>=executionDeadline.get()) cause.compareAndSet(0,3);
                    else if(now>=leaseDeadline.get()) cause.compareAndSet(0,2);
                    else cause.compareAndSet(0,1);
                    cancelled.set(true); Execution running=active; if(running!=null) running.cancel();
                }
                try { Thread.sleep(25); } catch(InterruptedException error) { return; }
            }
        },"lastcore-lease-guard");
        guard.start();
        boolean controllerCancel=renewed.optBoolean("cancel_requested",false);
        JSONObject outcome;
        try {
            backend.verify(cancelled); // Integrity hashing is independently under the lease guard.
            if(!policy.allowed()) cancelled.set(true);
            if(!cancelled.get()) {
                double requested=job.getJSONObject("request").getDouble("execution_timeout_seconds");
                if(clock.now()>=leaseDeadline.get() || clock.now()>=overallDeadline || cancelled.get())
                    throw new IllegalStateException("Lease expired before native launch");
                int millis=(int)Math.max(1,Math.min(requested*1000,(overallDeadline-clock.now())/1_000_000));
                if(millis>90000) throw new IllegalStateException("Native budget mismatch");
                executionDeadline.set(clock.now()+millis*1_000_000L);
                ledger.put("phase","active");persist(); // Durable execution identity precedes any native thread.
                active=backend.start(job.getJSONObject("request").getString("prompt"),job.getJSONObject("request").getInt("max_tokens"),millis,cancelled);
                events.status("Cloud job executing.");
                long nextRenew=clock.now(), nextPolicy=clock.now();
                while(!active.stopped()) {
                    if(clock.now()>=nextPolicy) {
                        if(!policy.allowed()) {cancelled.set(true);active.cancel();}
                        nextPolicy=clock.now()+500_000_000L;
                    }
                    if(clock.now()>=nextRenew && !cancelled.get()) {
                        sent=clock.now();
                        try {
                            renewed=api.lease(saved,node(),token(),"renew",new JSONObject());
                            if(renewed.optBoolean("cancel_requested",false)) {controllerCancel=true;cancelled.set(true);active.cancel();}
                            else leaseDeadline.set(delta(renewed,"lease_expires",sent));
                        } catch(CloudApi.Failure error) {
                            if(error.status==409 || error.status==401 || error.status==403) {cause.compareAndSet(0,2);cancelled.set(true);active.cancel();}
                            // Unconfirmed renewals never extend the guard's previous deadline.
                        }
                        nextRenew=clock.now()+2_000_000_000L;
                    }
                    Thread.sleep(25);
                }
                await(active);
            }
            if(controllerCancel) outcome=new JSONObject().put("status","cancelled");
            else if(active!=null && active.success() && !cancelled.get() && !stop.get()
                    && clock.now()<overallDeadline && active.elapsed()<=90)
                outcome=new JSONObject().put("status","succeeded").put("result",new JSONObject().put("text",active.text())
                    .put("elapsed_seconds",active.elapsed()).put("model_sha256",CloudApi.MODEL_SHA));
            else outcome=new JSONObject().put("status","failed").put("error_code",cause.get()==3?"deadline":cause.get()==2?"lease_lost":stop.get()?"worker_restart":"backend_error");
        } catch(Exception | LinkageError error) {
            if(active!=null) {active.cancel();await(active);}
            outcome=new JSONObject().put("status","failed").put("error_code",cause.get()==3?"deadline":cause.get()==2?"lease_lost":"backend_error");
        } finally {
            if(active!=null) {active.cancel();await(active);active=null;}
            guarding.set(false);guard.interrupt();join(guard);
        }
        ledger.put("phase","stopped").put("outcome",outcome);persist();
        deliver(saved,outcome); // Only after the actual native thread has returned/closed.
        events.status("Cloud job finished; native execution stopped.");
    }
    void run(int maxJobs) throws Exception {
        if(!RUNNING.compareAndSet(false,true)) throw new IllegalStateException("A cloud worker is already active or stopping");
        try {
            recover(); // Prior physical cleanup never depends on model availability or current device budgets.
            if(!policy.allowed()) throw new IllegalStateException("Device budget is unavailable");
            backend.verify(stop); capability();
            events.status("Cloud worker ready for one whole-model job at a time.");
            int count=0;long heartbeat=0;
            while(!stop.get()) {
                if(!policy.allowed()) throw new IllegalStateException("Device budget is unavailable");
                if(clock.now()>=heartbeat) {
                    api.post("/v1/nodes/"+node()+"/heartbeat",token(),new JSONObject().put("available_memory_mb",512).put("accepting_tasks",false),200);
                    heartbeat=clock.now()+5_000_000_000L;
                }
                boolean uncertain=state.optBoolean("claim_pending",false);
                state.put("claim_pending",true);persist();
                JSONObject response=api.node(node(),"claim",token(),new JSONObject());
                JSONObject cleanup=response.optJSONObject("cleanup_required");
                if(cleanup!=null) {
                    if(!uncertain) throw new IllegalStateException("Unknown prior cleanup requires recovery; no job will launch");
                    // A pending claim never launched here. Missing ownership cannot acknowledge another actor's key.
                    JSONObject saved=new JSONObject().put("id",cleanup.getString("id")).put("lease_epoch",cleanup.getLong("lease_epoch"));
                    api.lease(saved,node(),token(),"stop-ack",new JSONObject());
                    state.remove("claim_pending");persist();continue;
                }
                JSONObject job=response.optJSONObject("job");
                if(job==null) {state.remove("claim_pending");persist();Thread.sleep(500);continue;}
                validateJob(job);
                if(!"leased".equals(job.getString("status"))) throw new IllegalStateException("A running lease without its ledger will never replay");
                if(uncertain) {
                    // The uncertain claim never launched native code locally. Recover it as failed, never execute it.
                    JSONObject saved=new JSONObject().put("id",job.getString("id")).put("lease_epoch",job.getLong("lease_epoch"));
                    state.put("ledger",new JSONObject().put("job",saved).put("phase","claimed"));persist();
                    deliver(saved,new JSONObject().put("status","failed").put("error_code","worker_restart"));
                } else execute(job);
                if(maxJobs>0 && ++count>=maxJobs) return;
            }
        } finally {
            try {if(active!=null) {active.cancel();await(active);active=null;}}
            finally {RUNNING.set(false);}
        }
    }
}
