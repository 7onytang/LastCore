package ai.lastcore.android;

import java.io.File;
import java.nio.charset.StandardCharsets;

/** Screen-bound whole-model reference. No Controller or distributed-layer executor. */
final class LocalRuntime implements AutoCloseable {
    static { System.loadLibrary("lastcore_local"); }
    private volatile long handle = nativeCreate();
    String generate(File verifiedModel, String prompt, int tokens) {
        return generate(verifiedModel, prompt, tokens, 90000);
    }
    String generate(File verifiedModel, String prompt, int tokens, int timeoutMillis) {
        if (prompt == null || prompt.trim().isEmpty()) throw new IllegalArgumentException("Enter a prompt");
        // SmolLM2's pinned model uses ChatML. UTF-8 bytes avoid JNI modified-UTF-8 output corruption.
        String chat = "<|im_start|>user\n" + prompt + "<|im_end|>\n<|im_start|>assistant\n";
        byte[] output = nativeGenerate(handle, verifiedModel.getAbsolutePath().getBytes(StandardCharsets.UTF_8),
            chat.getBytes(StandardCharsets.UTF_8), tokens, timeoutMillis);
        return new String(output, StandardCharsets.UTF_8);
    }
    void cancel() { nativeCancel(handle); }
    @Override public synchronized void close() { long old = handle; handle = 0; nativeClose(old); }
    private static native long nativeCreate();
    private static native void nativeCancel(long id);
    private static native void nativeClose(long id);
    private static native byte[] nativeGenerate(long id, byte[] path, byte[] prompt, int tokens, int timeoutMillis);
}
