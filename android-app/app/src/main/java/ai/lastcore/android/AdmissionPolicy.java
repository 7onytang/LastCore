package ai.lastcore.android;

/** Native shell gates only; a backend remains unavailable until independently integrated. */
final class AdmissionPolicy {
    static String rejection(boolean backendReady, long availableBytes, int thermal, int battery, boolean charging) {
        if (!backendReady) return "BACKEND_UNAVAILABLE";
        if (availableBytes <= 0 || thermal < 0 || battery < 0 || battery > 100) return "DEVICE_METADATA_UNAVAILABLE";
        if (thermal >= 3) return "THERMAL_LIMIT";
        if (!charging && battery < 20) return "BATTERY_LOW";
        // A future adapter must additionally supply measured peak/shard metadata.
        return "MODEL_METADATA_UNAVAILABLE";
    }
}
