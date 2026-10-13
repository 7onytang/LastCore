package ai.lastcore.android;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.AtomicFile;
import java.io.File;
import java.io.FileOutputStream;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Stores encrypted bytes in app-private no-backup storage; never logs secrets. */
final class CredentialVault {
    private final String alias;
    private final AtomicFile file;
    CredentialVault(Context context) { this(context, "lastcore-node-v1", "node-credentials.enc"); }
    CredentialVault(Context context, String alias, String filename) {
        this.alias=alias; file=new AtomicFile(new File(context.getNoBackupFilesDir(), filename));
    }
    private SecretKey key(boolean create) throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore"); store.load(null);
        if (!store.containsAlias(alias)) {
            if (!create) throw new java.security.GeneralSecurityException("Credential key is unavailable");
            KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());
            generator.generateKey();
        }
        return (SecretKey) store.getKey(alias, null);
    }
    synchronized void save(byte[] credentials) throws Exception {
        if (credentials.length > 131072) throw new IllegalArgumentException("Credential state exceeds budget");
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding"); cipher.init(Cipher.ENCRYPT_MODE, key(true));
        byte[] iv = cipher.getIV(); byte[] encrypted = cipher.doFinal(credentials);
        FileOutputStream output = null;
        try {
            output = file.startWrite(); output.write(iv.length); output.write(iv); output.write(encrypted); file.finishWrite(output);
        } catch (Exception error) { if (output != null) file.failWrite(output); throw error; }
    }
    synchronized void clear() throws Exception {
        file.delete();
        KeyStore store = KeyStore.getInstance("AndroidKeyStore"); store.load(null);
        if (store.containsAlias(alias)) store.deleteEntry(alias);
    }
    synchronized byte[] load() throws Exception {
        byte[] bytes = file.readFully();
        if (bytes.length < 1 || (bytes[0] & 255) != 12 || bytes.length < 29) throw new IllegalStateException("Invalid credential envelope");
        byte[] iv = java.util.Arrays.copyOfRange(bytes, 1, 13);
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding"); cipher.init(Cipher.DECRYPT_MODE, key(false), new GCMParameterSpec(128, iv));
        return cipher.doFinal(bytes, 13, bytes.length - 13);
    }
}
