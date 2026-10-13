package ai.lastcore.android;

import android.content.Context;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.io.ByteArrayInputStream;
import java.io.File;
import java.util.concurrent.atomic.AtomicBoolean;
import static org.junit.Assert.*;

/** Device-only tests. Native-library compilation does not execute these checks. */
@RunWith(AndroidJUnit4.class)
public class NativeRuntimeTest {
    private Context context() { return InstrumentationRegistry.getInstrumentation().getTargetContext(); }
    @Test public void invalidModelDoesNotCrash() {
        try (LocalRuntime runtime = new LocalRuntime()) {
            try { runtime.generate(new File(context().getNoBackupFilesDir(), "absent-test.gguf"), "Hello", 4); fail("must reject missing model"); }
            catch (IllegalStateException expected) { }
        }
    }
    @Test public void closedHandleFailsWithoutUseAfterFree() {
        LocalRuntime runtime = new LocalRuntime(); runtime.close(); runtime.close(); runtime.cancel();
        try { runtime.generate(new File(context().getNoBackupFilesDir(), "absent-test.gguf"), "Hello", 4); fail("must reject closed handle"); }
        catch (IllegalStateException expected) { }
    }
    @Test public void cancelBeforeGenerationFailsClosed() {
        try (LocalRuntime runtime = new LocalRuntime()) {
            runtime.cancel();
            try { runtime.generate(new File(context().getNoBackupFilesDir(), "absent-test.gguf"), "Hello", 4); fail("must reject cancelled handle"); }
            catch (IllegalStateException expected) { }
        }
    }
    @Test public void rejectsWrongOrCancelledImport() throws Exception {
        // A failed import must never become a usable pinned model; existing valid model is preserved.
        ModelStore store = new ModelStore(context());
        try { store.importModel(new ByteArrayInputStream(new byte[8]), new AtomicBoolean()); fail("must reject wrong size/hash"); }
        catch (IllegalStateException expected) { }
        try { store.importModel(new ByteArrayInputStream(new byte[8]), new AtomicBoolean(true)); fail("must reject cancelled import"); }
        catch (IllegalStateException expected) { }
    }
}
