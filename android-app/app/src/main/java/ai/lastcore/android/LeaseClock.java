package ai.lastcore.android;

import java.util.Objects;
import java.util.function.LongSupplier;

/** One injected monotonic basis; Android supplies elapsedRealtimeNanos (including deep sleep). */
final class LeaseClock {
    private final LongSupplier source;
    LeaseClock(LongSupplier source) {this.source=Objects.requireNonNull(source);}
    long now() {return source.getAsLong();}
    static long deadline(double absolute, double serverTime, long sent) {
        double remaining=absolute-serverTime;
        if(!Double.isFinite(remaining) || remaining>91 || remaining<=0)
            throw new IllegalStateException("Lease budget is invalid");
        try {return Math.subtractExact(Math.addExact(sent,(long)(remaining*1_000_000_000L)),500_000_000L);}
        catch(ArithmeticException error) {throw new IllegalStateException("Lease budget is invalid");}
    }
}
