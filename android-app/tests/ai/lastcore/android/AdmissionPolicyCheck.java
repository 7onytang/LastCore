package ai.lastcore.android;
public final class AdmissionPolicyCheck {
    private static void check(String expected, String actual) { if (!expected.equals(actual)) throw new AssertionError(actual); }
    public static void main(String[] args) {
        check("BACKEND_UNAVAILABLE", AdmissionPolicy.rejection(false, 1024, 0, 100, true));
        check("DEVICE_METADATA_UNAVAILABLE", AdmissionPolicy.rejection(true, 0, 0, 100, true));
        check("DEVICE_METADATA_UNAVAILABLE", AdmissionPolicy.rejection(true, 1024, 0, -1, false));
        check("THERMAL_LIMIT", AdmissionPolicy.rejection(true, 1024, 3, 100, true));
        check("BATTERY_LOW", AdmissionPolicy.rejection(true, 1024, 0, 19, false));
        check("MODEL_METADATA_UNAVAILABLE", AdmissionPolicy.rejection(true, 1024, 0, 100, true));
        check("MODEL_METADATA_UNAVAILABLE", AdmissionPolicy.rejection(true, 1024, 0, 19, true));
        System.out.println("PASS: 7 native fail-closed admission checks (no inference)");
    }
}
