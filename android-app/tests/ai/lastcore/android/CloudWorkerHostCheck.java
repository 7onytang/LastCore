package ai.lastcore.android;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

/** Actual production Host/Clock with controlled work only: no Android, socket, JNI or model loading. */
public final class CloudWorkerHostCheck {
    private static int checks;
    private static void require(boolean value,String message) {if(!value) throw new AssertionError(message);}
    private static void await(CountDownLatch latch) throws Exception {require(latch.await(3,TimeUnit.SECONDS),"fixture did not finish");}
    private static void until(java.util.function.BooleanSupplier condition) throws Exception {
        long deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(3);
        while(!condition.getAsBoolean()) {require(System.nanoTime()<deadline,"state transition timed out");Thread.sleep(1);}
    }
    private static final class Work implements CloudWorkerHost.Session {
        final CountDownLatch entered=new CountDownLatch(1),finish=new CountDownLatch(1),stopped=new CountDownLatch(1);
        final AtomicInteger runs=new AtomicInteger(),closes=new AtomicInteger();
        final AtomicBoolean closeFails=new AtomicBoolean();
        final CloudWorkerHost.Progress progress;
        Work(CloudWorkerHost.Progress progress) {this.progress=progress;}
        @Override public void execute() throws Exception {runs.incrementAndGet();progress.executing(true);entered.countDown();await(finish);}
        @Override public void stop() {stopped.countDown();}
        @Override public void close() throws Exception {closes.incrementAndGet();if(closeFails.get()) throw new Exception("synthetic checkpoint failure");}
    }
    private static Work start(CloudWorkerHost host) throws Exception {
        AtomicReference<Work> work=new AtomicReference<>();
        require(host.start((stop,progress) -> {Work value=new Work(progress);work.set(value);return value;},generation -> {})>0,"start rejected");
        until(() -> work.get()!=null);await(work.get().entered);return work.get();
    }
    private static void finish(CloudWorkerHost host,Work work) throws Exception {
        work.finish.countDown();until(() -> !host.snapshot().busy);
    }
    private static void dedupAndDelayedCleanup() throws Exception {
        CloudWorkerHost host=new CloudWorkerHost(s -> {});Work work=start(host);
        try {
            require(host.start((stop,p) -> {throw new AssertionError("duplicate factory");},g -> {})==-1,"duplicate started");
            host.stop();host.stop();await(work.stopped);
            require(host.snapshot().phase==CloudWorkerHost.Phase.STOPPING && host.snapshot().busy,"early stopped state");
            require(work.closes.get()==0,"resource closed before actual work returned");
            require(CloudWorkerHost.acquireLocal()==null,"local work overlapped cloud stopping");
            require(!host.canFinish(host.snapshot().generation),"notification/service could finish early");
        } finally {finish(host,work);}
        require(work.runs.get()==1 && work.closes.get()==1,"work was replayed or cleanup duplicated");checks++;
    }
    private static void checkpointFailureRetainsOwnership() throws Exception {
        CloudWorkerHost host=new CloudWorkerHost(s -> {});Work work=start(host);work.closeFails.set(true);
        work.finish.countDown();until(() -> host.snapshot().cleanupPending);
        require(host.snapshot().busy && host.snapshot().phase==CloudWorkerHost.Phase.STOPPING,"checkpoint failure became stopped");
        require(CloudWorkerHost.acquireLocal()==null,"checkpoint failure released gate");
        CloudWorkerHost other=new CloudWorkerHost(s -> {});
        require(other.start((stop,p) -> {throw new AssertionError("restarted through barrier");},g -> {})==-1,"restart bypassed checkpoint");
        work.closeFails.set(false);host.stop();until(() -> !host.snapshot().busy);
        require(work.runs.get()==1 && work.closes.get()==2,"cleanup retry reexecuted work");checks++;
    }
    private static void cancelBeforeFactory() throws Exception {
        AtomicInteger factories=new AtomicInteger();CloudWorkerHost host=new CloudWorkerHost(s -> {});
        host.start((stop,p) -> {factories.incrementAndGet();throw new AssertionError("late launch");},g -> host.stop());
        until(() -> !host.snapshot().busy);require(factories.get()==0,"stop during promotion launched work");checks++;
    }
    private static void rejectedForegroundNeverCreatesWork() throws Exception {
        AtomicInteger factories=new AtomicInteger();CloudWorkerHost host=new CloudWorkerHost(s -> {});
        long gen=host.start((stop,p) -> {factories.incrementAndGet();throw new AssertionError("foreground failed");},g -> {throw new SecurityException("synthetic");});
        require(!host.snapshot().busy && "START_REJECTED".equals(host.snapshot().error),"foreground failure hidden");
        require(factories.get()==0 && host.canFinish(gen),"rejected foreground launched or leaked");checks++;
    }
    private static void observerDetachAndFailureDoNotCancel() throws Exception {
        AtomicReference<CloudWorkerHost.Observer> observer=new AtomicReference<>(s -> {});
        CloudWorkerHost host=new CloudWorkerHost(s -> {CloudWorkerHost.Observer current=observer.get();if(current!=null) current.changed(s);});
        Work work=start(host);
        observer.set(null);work.progress.executing(false);
        require(work.stopped.getCount()==1 && host.snapshot().phase==CloudWorkerHost.Phase.WAITING,"observer detach stopped worker");
        observer.set(s -> {throw new IllegalStateException("synthetic UI failure");});
        work.progress.executing(true);require(host.snapshot().busy,"observer failure changed execution");
        host.stop();finish(host,work);checks++;
    }
    private static void staleGenerationCannotFinishNewSession() throws Exception {
        CloudWorkerHost first=new CloudWorkerHost(s -> {});Work old=start(first);long oldGen=first.snapshot().generation;
        finish(first,old);require(first.canFinish(oldGen),"completed owner cannot finish");
        CloudWorkerHost second=new CloudWorkerHost(s -> {});Work current=start(second);
        try {
            long revision=second.snapshot().revision;old.progress.executing(false);
            require(second.snapshot().revision==revision,"old progress changed new session");
            require(!first.canFinish(oldGen),"old cleanup could remove new service/notification");
            require(!second.canFinish(oldGen),"wrong start generation accepted");
        } finally {second.stop();finish(second,current);}
        checks++;
    }
    private static void recreatedActivityCannotBypassLocalGate() throws Exception {
        CloudWorkerHost.DeviceUse oldActivity=CloudWorkerHost.acquireLocal();require(oldActivity!=null,"local acquire");
        CloudWorkerHost host=new CloudWorkerHost(s -> {});
        require(host.start((stop,p) -> {throw new AssertionError("overlap");},g -> {})==-1,"cloud bypassed old Activity");
        require(CloudWorkerHost.acquireLocal()==null,"new Activity overlapped old cleanup");
        oldActivity.close();Work work=start(host);
        try {oldActivity.close();require(CloudWorkerHost.deviceBusy(),"stale release stole new owner");}
        finally {host.stop();finish(host,work);}
        checks++;
    }
    private static void oldNotificationStopCannotCancelNewRun() throws Exception {
        CloudWorkerHost host=new CloudWorkerHost(s -> {});Work old=start(host);long oldGeneration=host.snapshot().generation;
        finish(host,old);Work current=start(host);long generation=host.snapshot().generation;
        try {
            require(!host.stop(oldGeneration) && !host.stop(-1),"old/unknown notification stop accepted");
            require(current.stopped.getCount()==1 && host.snapshot().phase==CloudWorkerHost.Phase.EXECUTING,"old STOP cancelled current worker");
            require(host.stop(generation),"current notification could not stop");await(current.stopped);
        } finally {finish(host,current);}
        checks++;
    }
    private static void clockAccountsForInjectedSleepAndBudgets() {
        AtomicLong elapsed=new AtomicLong(1_000_000_000L);LeaseClock clock=new LeaseClock(elapsed::get);
        long deadline=LeaseClock.deadline(110,100,clock.now());
        require(deadline==10_500_000_000L,"server delta/margin changed");
        elapsed.addAndGet(20_000_000_000L);require(clock.now()>=deadline,"elapsed sleep extended lease");
        long sameBasis=LeaseClock.deadline(205,200,clock.now());
        require(sameBasis==25_500_000_000L,"renew used a different clock basis");
        double[] invalid={Double.NaN,Double.POSITIVE_INFINITY,0,-1,91.1};
        for(double remaining:invalid) {
            try {LeaseClock.deadline(remaining,0,0);throw new AssertionError("invalid lease accepted");}
            catch(IllegalStateException expected) { }
        }
        try {LeaseClock.deadline(10,0,Long.MAX_VALUE);throw new AssertionError("overflow accepted");}
        catch(IllegalStateException expected) { }
        checks++;
    }
    public static void main(String[] args) throws Exception {
        dedupAndDelayedCleanup();checkpointFailureRetainsOwnership();cancelBeforeFactory();
        rejectedForegroundNeverCreatesWork();observerDetachAndFailureDoNotCancel();
        staleGenerationCannotFinishNewSession();recreatedActivityCannotBypassLocalGate();
        oldNotificationStopCannotCancelNewRun();
        clockAccountsForInjectedSleepAndBudgets();
        require(!CloudWorkerHost.deviceBusy(),"fixture leaked ownership");
        System.out.println("Foreground Host/Clock: "+checks+" pure Java checks passed; no Android/JNI/model/service executed.");
    }
}
