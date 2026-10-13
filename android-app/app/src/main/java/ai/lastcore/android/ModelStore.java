package ai.lastcore.android;

import android.content.Context;
import android.util.AtomicFile;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.security.MessageDigest;
import java.util.concurrent.atomic.AtomicBoolean;

/** Accept only the exact reviewed GGUF; copy into app-private no-backup storage. */
final class ModelStore {
    static final long BYTES = 105454144L;
    static final String SHA256 = "bda484992f9655d22504b14e57985257fa6a86937c61f957cf99c10a3bcae169";
    private final File model;
    ModelStore(Context context) { model = new File(context.getNoBackupFilesDir(), "smollm2-135m-q4km.gguf"); }
    private static String hex(byte[] bytes) {
        StringBuilder value = new StringBuilder();
        for (byte b : bytes) value.append(String.format(java.util.Locale.ROOT, "%02x", b & 255));
        return value.toString();
    }
    File verified(AtomicBoolean cancelled) throws Exception {
        if (model.length() != BYTES) throw new IllegalStateException("Import the pinned 105 MB SmolLM2 GGUF first");
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream in = new AtomicFile(model).openRead()) {
            byte[] buffer = new byte[65536]; int count;
            while ((count = in.read(buffer)) != -1) {
                if (cancelled.get()) throw new IllegalStateException("Operation cancelled");
                digest.update(buffer, 0, count);
            }
        }
        if (!SHA256.equals(hex(digest.digest()))) throw new IllegalStateException("Model integrity check failed");
        return model;
    }
    void importModel(InputStream in, AtomicBoolean cancelled) throws Exception {
        AtomicFile atomic = new AtomicFile(model);
        FileOutputStream out = null;
        try {
            out = atomic.startWrite(); MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] buffer = new byte[65536]; int count; long total = 0;
            while ((count = in.read(buffer)) != -1) {
                if (cancelled.get()) throw new IllegalStateException("Operation cancelled");
                total += count;
                if (total > BYTES) throw new IllegalStateException("Model exceeds pinned size");
                out.write(buffer, 0, count); digest.update(buffer, 0, count);
            }
            if (cancelled.get() || total != BYTES || !SHA256.equals(hex(digest.digest())))
                throw new IllegalStateException("Only the pinned SmolLM2 GGUF is accepted");
            atomic.finishWrite(out); out = null;
        } finally { if (out != null) atomic.failWrite(out); }
    }
    void delete() { new AtomicFile(model).delete(); }
}
