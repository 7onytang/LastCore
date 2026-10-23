package ai.lastcore.android;

/** UI-thread binding generations. A flags=0 registration is not evidence that a Service exists. */
final class CloudBindingCoordinator {
    interface Peer {long start(String origin,boolean enroll,String credential);}
    interface Port {
        boolean bind(long generation,boolean autoCreate);
        void unbind(long generation);
        void submitted(long generation,long workerGeneration);
        void rejected();
    }
    static final class Command {
        final String origin;
        final boolean enroll;
        private String credential;
        Command(String origin,boolean enroll,String credential) {this.origin=origin;this.enroll=enroll;this.credential=credential;}
        void clear() {credential=null;}
        boolean cleared() {return credential==null;}
    }
    private static final class Binding {
        final long generation;
        final boolean autoCreate;
        boolean registered;
        Binding(long generation,boolean autoCreate) {this.generation=generation;this.autoCreate=autoCreate;}
    }
    private final Port port;
    private long sequence;
    private boolean visible;
    private Binding binding;
    private Peer peer;
    private Command pending;
    CloudBindingCoordinator(Port port) {this.port=port;}
    boolean hasPending() {return pending!=null;}
    boolean accepts(long generation) {return visible && binding!=null && binding.generation==generation;}
    void resume() {visible=true;if(binding==null) bind(false);}
    void pause() {visible=false;clearPending();dropBinding();}
    boolean request(Command command) {
        if(!visible || pending!=null) {command.clear();return false;}
        pending=command;
        if(peer!=null) deliver();
        else if(binding==null || !binding.autoCreate) {
            // A passive flags=0 bind can return true without any callback. Replace it explicitly.
            dropBinding();bind(true);
        }
        return pending!=null || peer!=null;
    }
    boolean connected(long generation,Peer value) {
        if(!accepts(generation)) return false;
        peer=value;deliver();return true;
    }
    void disconnected(long generation) {if(accepts(generation)) peer=null;}
    void died(long generation) {
        if(!accepts(generation)) return;
        clearPending();dropBinding();if(visible) bind(false);
    }
    void cancelPending() {
        clearPending();
        if(binding!=null && binding.autoCreate && peer==null) {dropBinding();if(visible) bind(false);}
    }
    private void clearPending() {if(pending!=null) {pending.clear();pending=null;}}
    private void dropBinding() {
        Binding old=binding;binding=null;peer=null; // Invalidate BEFORE unbind can dispatch a late callback.
        if(old!=null && old.registered) port.unbind(old.generation);
    }
    private void bind(boolean create) {
        Binding value=new Binding(++sequence,create);binding=value;
        boolean registered;
        try {registered=port.bind(value.generation,create);} catch(RuntimeException error) {registered=false;}
        value.registered=registered;
        if(!registered && binding==value) {
            boolean requested=pending!=null;binding=null;peer=null;clearPending();
            if(requested) port.rejected(); // No Service on a passive observation is not a failed user operation.
        }
    }
    private void deliver() {
        if(!visible || peer==null || pending==null || binding==null) return;
        Command command=pending;pending=null;
        long generation=binding.generation, result=-1;
        try {result=peer.start(command.origin,command.enroll,command.credential);}
        catch(RuntimeException ignored) { /* No provider/credential text leaves the control boundary. */ }
        finally {command.clear();}
        if(accepts(generation)) port.submitted(generation,result);
    }
}
