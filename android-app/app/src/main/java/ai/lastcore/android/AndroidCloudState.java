package ai.lastcore.android;

import android.content.Context;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.RandomAccessFile;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

/** No-backup atomic encrypted credentials/lease ledger, plus process-exclusive worker lock. */
final class AndroidCloudState implements CloudState {
    private final CredentialVault vault;
    private final FileChannel channel;
    private final FileLock lock;
    AndroidCloudState(Context context) throws Exception {
        context=context.getApplicationContext();
        channel=new RandomAccessFile(new File(context.getNoBackupFilesDir(),"cloud-worker.lock"),"rw").getChannel();
        FileLock acquired;
        try {
            acquired=channel.tryLock();
            if(acquired==null) throw new IllegalStateException("A cloud worker is still active or stopping");
        } catch(Exception error) {channel.close();throw error;}
        lock=acquired;
        vault=new CredentialVault(context,"lastcore-cloud-v1","cloud-worker-state.enc");
    }
    @Override public JSONObject load() throws Exception {
        byte[] bytes;
        try {bytes=vault.load();} catch(FileNotFoundException missing) {return new JSONObject();}
        try {
            if(bytes.length>131072) throw new IllegalStateException("Cloud state exceeds budget");
            return new JSONObject(new String(bytes,StandardCharsets.UTF_8));
        } finally {java.util.Arrays.fill(bytes,(byte)0);}
    }
    @Override public void save(JSONObject state) throws Exception {
        byte[] bytes=state.toString().getBytes(StandardCharsets.UTF_8);
        try {vault.save(bytes);} finally {java.util.Arrays.fill(bytes,(byte)0);}
    }
    @Override public void close() throws Exception {try {if(lock.isValid()) lock.release();} finally {if(channel.isOpen()) channel.close();}}
}
