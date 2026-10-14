"""Deterministic, bounded workloads used by the browser demonstration."""

PI_ID = "lastcore/pi-monte-carlo-v1"
LOG_ID = "lastcore/log-statistics-v1"


def _random_unit(seed: int, index: int, stream: int) -> float:
    value = (seed + index * 0x9E3779B97F4A7C15 + stream * 0xD1B54A32D192ED03) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    value ^= value >> 31
    return value / 0x10000000000000000


def pi_demo(total_samples: int, seed: int, chunks: int, start: int, end: int) -> dict[str, int]:
    first = total_samples * start // chunks
    last = total_samples * end // chunks
    inside = sum(
        _random_unit(seed, index, 0) ** 2 + _random_unit(seed, index, 1) ** 2 <= 1.0
        for index in range(first, last)
    )
    return {"inside": inside, "samples": last - first}


def log_stats(lines: list[str], start: int, end: int) -> dict[str, int]:
    counts = {"ERROR": 0, "WARN": 0, "INFO": 0, "OTHER": 0}
    for line in lines[start:end]:
        upper = line.upper()
        level = next((candidate for candidate in ("ERROR", "WARN", "INFO") if candidate in upper), "OTHER")
        counts[level] += 1
    return counts
