package ai.lastcore.android;

import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.security.MessageDigest;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicBoolean;

/** Real JNI generation; completion means JNI returned and its session was closed, not merely cancelled. */
final class JniBackend implements CloudWorker.Backend {
    private final File model;
    private final String nativeHash;
    JniBackend(File model, String nativeHash) {
        if (!nativeHash.matches("[a-f0-9]{64}")) throw new IllegalArgumentException("Invalid native integrity identity");
        this.model = model; this.nativeHash = nativeHash;
    }
    static String hash(InputStream stream, AtomicBoolean cancelled) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256"); byte[] buffer = new byte[65536]; int count;
        while ((count = stream.read(buffer)) != -1) {
            if (cancelled.get()) throw new IllegalStateException("Operation cancelled");
            digest.update(buffer, 0, count);
        }
        StringBuilder out = new StringBuilder();
        for (byte b : digest.digest()) out.append(String.format(java.util.Locale.ROOT, "%02x", b & 255));
        return out.toString();
    }
    @Override public String identity() { return nativeHash; }
    @Override public void verify(AtomicBoolean cancelled) throws Exception {
        if (model.length() != 105454144L) throw new IllegalStateException("Pinned model size mismatch");
        try (InputStream input = new FileInputStream(model)) {
            if (!CloudApi.MODEL_SHA.equals(hash(input, cancelled))) throw new IllegalStateException("Pinned model hash mismatch");
        }
        // Loading the JNI entry verifies that the installed Android ABI can resolve the real backend.
        try (LocalRuntime runtime = new LocalRuntime()) { runtime.cancel(); }
    }
    @Override public CloudWorker.Execution start(String prompt, int tokens, int timeoutMillis, AtomicBoolean cancelled) {
        return new NativeExecution(prompt, tokens, timeoutMillis, cancelled);
    }
    private final class NativeExecution implements CloudWorker.Execution {
        private final LocalRuntime runtime = new LocalRuntime();
        private final CountDownLatch stopped = new CountDownLatch(1);
        private volatile String text;
        private volatile boolean success;
        private volatile double elapsed;
        NativeExecution(String prompt, int tokens, int timeoutMillis, AtomicBoolean cancelled) {
            Thread thread = new Thread(() -> {
                long start = System.nanoTime();
                try {
                    if (cancelled.get()) runtime.cancel();
                    text = runtime.generate(model, prompt, tokens, timeoutMillis);
                    success = !cancelled.get();
                } catch (Throwable error) { success = false; }
                finally {
                    runtime.close();
                    elapsed = (System.nanoTime() - start) / 1_000_000_000.0;
                    stopped.countDown();
                }
            }, "lastcore-jni-generation");
            thread.start();
        }
        @Override public void cancel() { runtime.cancel(); }
        @Override public boolean stopped() { return stopped.getCount() == 0; }
        @Override public void awaitStopped() throws InterruptedException { stopped.await(); }
        @Override public String text() { return text; }
        @Override public boolean success() { return success; }
        @Override public double elapsed() { return elapsed; }
    }
}
