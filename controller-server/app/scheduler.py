"""Capacity-constrained weighted allocation of contiguous layer ranges."""


def allocate(nodes: list[dict], num_layers: int, layer_memory_mb: int, max_nodes: int):
    candidates = sorted(
        [n for n in nodes if n["available_memory_mb"] >= layer_memory_mb],
        key=lambda n: (-n["compute_weight"], -n["available_memory_mb"], n["id"]),
    )
    # Replace faster low-memory devices if required to fit within the node limit.
    selected = candidates[:max_nodes]
    capacity = lambda n: n["available_memory_mb"] // layer_memory_mb
    if sum(map(capacity, selected)) < num_layers:
        selected = sorted(candidates, key=lambda n: (-capacity(n), -n["compute_weight"], n["id"]))[:max_nodes]
    if sum(map(capacity, selected)) < num_layers:
        raise ValueError("insufficient available node memory within max_nodes")
    counts = [0] * len(selected)
    # Choose the next least loaded device, normalized by its measured weight.
    for _ in range(num_layers):
        index = min(
            (i for i, n in enumerate(selected) if counts[i] < capacity(n)),
            key=lambda i: ((counts[i] + 1) / selected[i]["compute_weight"], i),
        )
        counts[index] += 1
    start = 0
    result = []
    for node, count in zip(selected, counts):
        if count:
            result.append({"node_id": node["id"], "endpoint": node["endpoint"],
                           "layer_start": start, "layer_end": start + count})
            start += count
    return result
