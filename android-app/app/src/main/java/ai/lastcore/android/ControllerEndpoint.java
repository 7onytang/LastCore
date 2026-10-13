package ai.lastcore.android;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.net.ssl.HttpsURLConnection;

/** HTTPS origin validation and bounded public health check. No credentials or job protocol. */
final class ControllerEndpoint {
    static String origin(String input) throws Exception {
        URI uri = new URI(input.trim());
        String path = uri.getRawPath();
        if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null
            || uri.getRawUserInfo() != null || uri.getRawQuery() != null || uri.getRawFragment() != null
            || (path != null && !path.isEmpty() && !path.equals("/"))
            || (uri.getPort() != -1 && (uri.getPort() < 1 || uri.getPort() > 65535)))
            throw new IllegalArgumentException("Enter an HTTPS Controller origin without credentials or path");
        return new URI("https", null, uri.getHost(), uri.getPort(), null, null, null).toASCIIString();
    }
    private static void check(AtomicBoolean cancelled, long deadline) {
        if (cancelled.get() || System.nanoTime() >= deadline)
            throw new IllegalStateException("Connection check cancelled or timed out");
    }
    static String health(String input) throws Exception { return health(input, new AtomicBoolean()); }
    static String health(String input, AtomicBoolean cancelled) throws Exception {
        long deadline = System.nanoTime() + 10_000_000_000L;
        check(cancelled, deadline);
        HttpsURLConnection connection = (HttpsURLConnection)new java.net.URL(origin(input) + "/healthz").openConnection();
        // Keep Android's default certificate chain and hostname verification. Never follow redirects.
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(5000); connection.setReadTimeout(5000);
        connection.setRequestProperty("Accept", "application/json");
        try {
            check(cancelled, deadline);
            if (connection.getResponseCode() != 200) throw new IllegalStateException("Controller health check failed");
            check(cancelled, deadline);
            try (InputStream stream = connection.getInputStream(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[1024]; int count;
                while (true) {
                    check(cancelled, deadline);
                    count = stream.read(buffer);
                    if (count == -1) break;
                    if (out.size() + count > 4096) throw new IllegalStateException("Controller health response exceeds limit");
                    out.write(buffer, 0, count);
                }
                check(cancelled, deadline);
                return new String(out.toByteArray(), StandardCharsets.UTF_8);
            }
        } finally { connection.disconnect(); }
    }
}
