package ai.lastcore.android;

/** Pure URI safety checks; optional host HTTPS probe is never Android TLS evidence. */
public final class ControllerEndpointCheck {
    public static void main(String[] args) throws Exception {
        String origin = ControllerEndpoint.origin("https://8.134.161.123/");
        if (!origin.equals("https://8.134.161.123")) throw new AssertionError("origin normalization");
        String[] invalid = {"http://8.134.161.123", "https://user:secret@example.com", "https://example.com/api", "https://example.com/?token=secret", "https://example.com/#token", "https://example.com:0", "https://example.com:65536", "https:///missing-host"};
        for (String value : invalid) {
            try { ControllerEndpoint.origin(value); throw new AssertionError("unsafe origin accepted"); }
            catch (IllegalArgumentException expected) { }
        }
        System.out.println("PASS: 9 HTTPS origin checks (pure Java, no device TLS)");
        if (args.length == 1) {
            String body = ControllerEndpoint.health(args[0]);
            if (!body.contains("\"status\":\"ok\"") && !body.contains("\"status\": \"ok\"")) throw new AssertionError("health status");
            System.out.println("PASS: host-JDK default TLS + bounded public health response; not Android device TLS");
        }
    }
}
