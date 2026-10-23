package ai.lastcore.android;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

/** Actual production binding coordinator with fake framework callbacks, not a copy of Activity logic. */
public final class CloudBindingCoordinatorCheck {
    private static void check(boolean value,String message) {if(!value) throw new AssertionError(message);}
    private static final class Port implements CloudBindingCoordinator.Port {
        final List<Long> generations=new ArrayList<>(),unbound=new ArrayList<>();
        final List<Boolean> creates=new ArrayList<>();
        int submits,rejections;
        boolean bindAllowed=true;
        @Override public boolean bind(long generation,boolean create) {generations.add(generation);creates.add(create);return bindAllowed;}
        @Override public void unbind(long generation) {unbound.add(generation);}
        @Override public void submitted(long generation,long worker) {submits++;}
        @Override public void rejected() {rejections++;}
        long latest() {return generations.get(generations.size()-1);}
    }
    private static CloudBindingCoordinator.Command command() {return new CloudBindingCoordinator.Command("https://fixture.invalid",true,"synthetic-enrollment-marker");}
    public static void main(String[] args) {
        int checks=0;
        Port port=new Port();CloudBindingCoordinator owner=new CloudBindingCoordinator(port);AtomicInteger starts=new AtomicInteger();
        owner.resume();long passive=port.latest();
        check(!port.creates.get(0),"resume must not auto-create");
        CloudBindingCoordinator.Command first=command();owner.request(first);long active=port.latest();
        check(active!=passive && port.creates.get(1) && port.unbound.equals(List.of(passive)),"true-without-callback passive bind was not upgraded");
        check(!owner.connected(passive,(a,b,c) -> {throw new AssertionError("old passive callback started");}),"old generation accepted");
        owner.connected(active,(a,b,c) -> {check("synthetic-enrollment-marker".equals(c),"credential was lost before submit");starts.incrementAndGet();return 1;});
        owner.connected(active,(a,b,c) -> {starts.incrementAndGet();return 2;});
        check(starts.get()==1 && port.submits==1 && first.cleared() && !owner.hasPending(),"start duplicated or credential retained");checks++;
        owner.pause();check(port.unbound.equals(List.of(passive,active)),"active binding was not removed");
        check(!owner.connected(active,(a,b,c) -> {throw new AssertionError("pause callback started");}),"callback after pause accepted");checks++;
        Port rotatingPort=new Port();CloudBindingCoordinator oldActivity=new CloudBindingCoordinator(rotatingPort);
        oldActivity.resume();CloudBindingCoordinator.Command rotating=command();oldActivity.request(rotating);long retired=rotatingPort.latest();
        oldActivity.pause();check(rotating.cleared(),"rotation retained pending credential");
        CloudBindingCoordinator recreated=new CloudBindingCoordinator(new Port());recreated.resume();
        check(!oldActivity.connected(retired,(a,b,c) -> {throw new AssertionError("old Activity launched after rotation");}),"retired Activity accepted callback");
        check(!recreated.hasPending(),"new Activity restored operational command");recreated.pause();checks++;
        Port pausedPort=new Port();CloudBindingCoordinator paused=new CloudBindingCoordinator(pausedPort);paused.resume();long beforePause=pausedPort.latest();
        paused.pause();check(!paused.connected(beforePause,(a,b,c) -> {throw new AssertionError("paused callback started");}),"pause accepted callback");
        paused.resume();CloudBindingCoordinator.Command afterPause=command();paused.request(afterPause);
        check(pausedPort.creates.get(pausedPort.creates.size()-1),"pause left passive binding stuck");
        AtomicInteger resumedStarts=new AtomicInteger();paused.connected(pausedPort.latest(),(a,b,c) -> {resumedStarts.incrementAndGet();return 1;});
        check(resumedStarts.get()==1 && afterPause.cleared(),"resume/button failed or retained credential");paused.pause();checks++;
        Port cancelledPort=new Port();CloudBindingCoordinator cancelled=new CloudBindingCoordinator(cancelledPort);cancelled.resume();
        CloudBindingCoordinator.Command pending=command();cancelled.request(pending);long cancelledGeneration=cancelledPort.latest();cancelled.cancelPending();
        check(pending.cleared() && !cancelled.hasPending() && !cancelledPort.creates.get(cancelledPort.creates.size()-1),"cancel retained credential/auto-create");
        check(!cancelled.connected(cancelledGeneration,(a,b,c) -> {throw new AssertionError("cancelled command launched");}),"cancelled binding accepted");cancelled.pause();checks++;
        Port failedPort=new Port();CloudBindingCoordinator failed=new CloudBindingCoordinator(failedPort);failed.resume();failedPort.bindAllowed=false;
        CloudBindingCoordinator.Command rejected=command();failed.request(rejected);
        check(rejected.cleared() && failedPort.rejections==1,"bind failure retained command");failed.pause();checks++;
        Port dyingPort=new Port();CloudBindingCoordinator dying=new CloudBindingCoordinator(dyingPort);dying.resume();
        CloudBindingCoordinator.Command unknown=command();dying.request(unknown);long dead=dyingPort.latest();dying.died(dead);
        check(unknown.cleared() && !dyingPort.creates.get(dyingPort.creates.size()-1),"binding death retried execution");
        check(!dying.connected(dead,(a,b,c) -> {throw new AssertionError("dead binding replayed");}),"dead generation accepted");dying.pause();checks++;
        Port directPort=new Port();CloudBindingCoordinator direct=new CloudBindingCoordinator(directPort);direct.resume();
        AtomicInteger directStarts=new AtomicInteger();direct.connected(directPort.latest(),(a,b,c) -> {directStarts.incrementAndGet();return 1;});
        CloudBindingCoordinator.Command directCommand=command();direct.request(directCommand);
        check(directPort.generations.size()==1 && directStarts.get()==1 && directCommand.cleared(),"existing service unnecessarily recreated or credential retained");direct.pause();checks++;
        Port absentPort=new Port();absentPort.bindAllowed=false;CloudBindingCoordinator absent=new CloudBindingCoordinator(absentPort);absent.resume();
        check(absentPort.rejections==0 && !absent.hasPending(),"passive absence was reported as failed execution");
        absentPort.bindAllowed=true;CloudBindingCoordinator.Command cold=command();absent.request(cold);
        check(absentPort.creates.get(absentPort.creates.size()-1),"failed passive observation blocked explicit creation");
        absent.pause();check(cold.cleared(),"cancelled cold command retained credential");checks++;
        Port throwingPort=new Port();CloudBindingCoordinator throwing=new CloudBindingCoordinator(throwingPort);throwing.resume();
        throwing.connected(throwingPort.latest(),(a,b,c) -> {throw new IllegalStateException("synthetic credential marker must not escape");});
        CloudBindingCoordinator.Command sensitive=command();throwing.request(sensitive);
        check(sensitive.cleared() && throwingPort.submits==1,"failed Binder start retained credential");throwing.pause();
        CloudBindingCoordinator.Command hidden=command();check(!throwing.request(hidden) && hidden.cleared(),"hidden Activity retained or launched command");checks++;
        System.out.println("Binding coordinator: "+checks+" pure Java checks passed; no Activity/Service/network executed.");
    }
}
