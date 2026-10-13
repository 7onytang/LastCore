package ai.lastcore.android;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import javax.net.ssl.HttpsURLConnection;
import org.json.JSONObject;

/** Fixed Controller contract transport; platform TLS/hostname defaults, no redirects or secret diagnostics. */
final class CloudApi {
    static final String MODEL = "SmolLM2-135M-Instruct-Q4_K_M";
    static final String MODEL_SHA = "bda484992f9655d22504b14e57985257fa6a86937c61f957cf99c10a3bcae169";
    static final String BACKEND = "8345f333951c661d166b00e6f9362e553768f292";
    final String origin;
    CloudApi(String origin) throws Exception { this.origin = ControllerEndpoint.origin(origin); }
    static final class Failure extends Exception {
        final int status;
        Failure(int status) { super(status == 0 ? "Controller connection failed" : "Controller rejected request"); this.status = status; }
    }
    static String id(String value) throws Exception {
        if (!java.util.UUID.fromString(value).toString().equals(value)) throw new IllegalArgumentException("Invalid Controller identity");
        return value;
    }
    static String credential(String value) {
        if (value == null || !value.matches("[A-Za-z0-9._~+/\\-]{32,512}={0,3}")) throw new IllegalArgumentException("Invalid credential format");
        return value;
    }
    JSONObject post(String path, String token, JSONObject body, int expected) throws Exception {
        if (!path.matches("/v1/(nodes/register|nodes/[a-f0-9\\-]{36}/heartbeat|inference/workers/[a-f0-9\\-]{36}/(capability|claim)|inference/jobs/[a-f0-9\\-]{36}/(renew|report|stop-ack))"))
            throw new IllegalArgumentException("Unsupported worker route");
        byte[] payload = body.toString().getBytes(StandardCharsets.UTF_8);
        if (payload.length > 131072) throw new IllegalArgumentException("Request budget exceeded");
        HttpsURLConnection connection = (HttpsURLConnection)new java.net.URL(origin + path).openConnection();
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(3000); connection.setReadTimeout(3000);
        connection.setRequestMethod("POST"); connection.setDoOutput(true);
        connection.setRequestProperty("Content-Type", "application/json");
        connection.setRequestProperty("Accept", "application/json");
        connection.setRequestProperty("Authorization", "Bearer " + credential(token));
        connection.setFixedLengthStreamingMode(payload.length);
        long deadline = System.nanoTime() + 7_000_000_000L;
        try {
            try (OutputStream stream = connection.getOutputStream()) { stream.write(payload); }
            if (connection.getResponseCode() != expected) throw new Failure(connection.getResponseCode());
            try (InputStream in = connection.getInputStream(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[4096]; int count;
                while (true) {
                    if (System.nanoTime() >= deadline) throw new Failure(0);
                    count = in.read(buffer); if (count == -1) break;
                    if (out.size() + count > 65536) throw new Failure(0);
                    out.write(buffer, 0, count);
                }
                if (System.nanoTime() >= deadline) throw new Failure(0);
                return new JSONObject(new String(out.toByteArray(), StandardCharsets.UTF_8));
            }
        } catch (Failure error) { throw error; }
        catch (Exception error) { throw new Failure(0); }
        finally { java.util.Arrays.fill(payload, (byte)0); connection.disconnect(); }
    }
    JSONObject node(String nodeId, String action, String token, JSONObject body) throws Exception {
        return post("/v1/inference/workers/" + id(nodeId) + "/" + action, token, body, 200);
    }
    JSONObject lease(JSONObject job, String nodeId, String token, String action, JSONObject extra) throws Exception {
        JSONObject body = new JSONObject(extra.toString()).put("node_id", id(nodeId)).put("lease_epoch", job.getLong("lease_epoch"));
        if (job.has("execution_key")) body.put("execution_key", job.getString("execution_key"));
        JSONObject response = post("/v1/inference/jobs/" + id(job.getString("id")) + "/" + action, token, body, 200);
        if (!response.getString("id").equals(job.getString("id")) || response.getLong("lease_epoch") != job.getLong("lease_epoch")
            || !response.getString("worker_node_id").equals(nodeId)) throw new Failure(0);
        return response;
    }
}
