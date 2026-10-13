package ai.lastcore.android;

import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.security.NetworkSecurityPolicy;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.io.File;
import java.nio.file.Files;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import static org.junit.Assert.*;

/** Real-device Keystore tests. Compilation is not execution evidence. */
@RunWith(AndroidJUnit4.class)
public class CredentialSecurityTest {
    private Context context() { return InstrumentationRegistry.getInstrumentation().getTargetContext(); }
    @Test public void encryptedRoundTripAndClear() throws Exception {
        Context context = context(); CredentialVault vault = new CredentialVault(context);
        byte[] secret = "fixture-secret-never-a-real-token".getBytes(StandardCharsets.UTF_8);
        File file = new File(context.getNoBackupFilesDir(), "node-credentials.enc");
        try {
            vault.clear(); vault.save(secret); assertArrayEquals(secret, vault.load());
            assertFalse(new String(Files.readAllBytes(file.toPath()), StandardCharsets.ISO_8859_1).contains(new String(secret, StandardCharsets.UTF_8)));
            KeyStore keys = KeyStore.getInstance("AndroidKeyStore"); keys.load(null);
            assertNull(keys.getKey("lastcore-node-v1", null).getEncoded());
            vault.clear(); assertFalse(file.exists()); assertFalse(keys.containsAlias("lastcore-node-v1"));
        } finally { vault.clear(); }
    }
    @Test public void ciphertextTamperFailsClosed() throws Exception {
        Context context = context(); CredentialVault vault = new CredentialVault(context);
        File file = new File(context.getNoBackupFilesDir(), "node-credentials.enc");
        try {
            vault.save("synthetic-test-value".getBytes(StandardCharsets.UTF_8));
            byte[] encrypted = Files.readAllBytes(file.toPath()); encrypted[encrypted.length - 1] ^= 1;
            Files.write(file.toPath(), encrypted);
            try { vault.load(); fail("tampered data must never decrypt"); } catch (java.security.GeneralSecurityException expected) { }
        } finally { vault.clear(); }
    }
    @Test public void manifestRejectsBackupAndCleartext() {
        assertEquals(0, context().getApplicationInfo().flags & ApplicationInfo.FLAG_ALLOW_BACKUP);
        assertFalse(NetworkSecurityPolicy.getInstance().isCleartextTrafficPermitted());
    }
    @Test public void unavailableBackendRejectsEvenHealthyDevice() {
        assertEquals("BACKEND_UNAVAILABLE", AdmissionPolicy.rejection(false, 1024L*1024*1024, 0, 100, true));
    }
}
