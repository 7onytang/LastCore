# LC-10 cost budget supplement

This adds a standalone byte-budget example and two result JSON files. It does
not change `controller-server/`, the scheduler, a production manifest, or the
original LC-10 scripts and JSON. Both reviewed source revisions still expose
the uniform `layer_memory_mb` scheduler interface; this example is not wired
into planning or reservations.

## Fixed source snapshots

| Revision | Tree | Direct parent |
|---|---|---|
| `e0f463e883d18e6fab25795804cd503527686d57` | `ed1b45afc1e195c075e9619c577b674641b2a1ff` | `672b4f7474e424380645deaef2c1e8e68a7c2771` |
| `a878d6388cdd955df4bf3953619e3f056f201391` | `3745d77ded34cfca5409522b8e1f1c16607cf8c7` | `e0f463e883d18e6fab25795804cd503527686d57` |

`cost_budget_validation.py` refuses other commits, mismatched trees/parents,
dirty checkouts, or a different scheduler interface. It records the verified
source revision in each result. All numbers are integer bytes (`MiB =
1,048,576 bytes`).

## Commands and results

The commands were run from the isolated task directory against clean detached
checkouts; the JSON paths are outside those checkouts.

```sh
python3 lc10-cost-supplement/cost_budget_validation.py --repo lc10-cost-supplement/work/source-repo --output lc10-cost-supplement/cost-budget-e0f.json
# exit 0
```

The same command was run after checking out `a878d6388cdd955df4bf3953619e3f056f201391`, with output changed to
`lc10-cost-supplement/cost-budget-a878.json`; it also exited 0.

Exit 0 means the illustrative two-stage case fit and the expected over-budget
case was rejected. It does not mean the production scheduler enforced either
budget.

## Byte derivation

The feasible synthetic fp16 example assigns layers `[0,2)` to node A and
`[2,4)` to node B. Layer weights are `[8,24,12,20] MiB`; the first and last
stages each also receive a 4 MiB embedding or head. With batch 1, sequence
length 2048, 8 KV heads, head dimension 64, and 512-wide activations:

- Each stage has 36 MiB of weights including its endpoint embedding/head.
- KV is `2 × 8 × 64 × 2 bytes × 2 layers × 2048 tokens` = 8 MiB.
- Activation is `1 × 2048 × 512 × 2 bytes` = 2 MiB.
- Peak is 46 MiB per stage, below the 64 MiB budget by 18 MiB.

The fail-fast illustration retains the historical wide-model layer-0
parameters. Layer weights plus embedding are 73,924,608 bytes; KV is
2,097,152 bytes; activation is 1,048,576 bytes. Total is 77,070,336 bytes,
which exceeds the 64 MiB (67,108,864 byte) budget by 9,961,472 bytes. The new
script reports this as an expected rejection and exits 0 when diagnosed
correctly. The original `lc10/cost_model_samples.py` and its recorded exit 1
at layer 0 remain untouched; its original missing cost JSON and the timing
attribution dispute are unchanged.

## Boundaries

These examples demonstrate arithmetic and endpoint cost attribution only.
They do not measure runtime memory, connect a ModelManifest to the scheduler,
test Controller snapshot changes or concurrent reservations, or integrate LC-6
and LC-9. Production KV/activation accounting and final QA remain open.
