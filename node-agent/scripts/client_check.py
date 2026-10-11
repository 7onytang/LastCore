"""Client check: send a prompt to the first stage, compare distributed logits
against the single-machine reference computed on the same checkpoint."""
import hashlib
import json
import socket
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "inference-engine"))
sys.path.insert(0, str(REPO_ROOT / "communication"))

from inference_engine import PipelineClient, forward_full
from inference_engine.config import tiny_config

first_host, first_port, weights_path, task_id = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]

tokens = np.asarray([[1, 2, 3, 4]], dtype=np.int64)
last_error = None
for _ in range(30):
    try:
        with socket.create_connection((first_host, first_port), timeout=1):
            pass
        break
    except OSError as exc:
        last_error = exc
        time.sleep(1)
else:
    raise SystemExit(f"first stage never accepted: {last_error}")

logits = PipelineClient((first_host, first_port), model_id="lastcore/tiny-cpu",
                        task_id=task_id).run(tokens, task_id=task_id)
config = tiny_config()
reference = forward_full(tokens, dict(np.load(weights_path)), config)
digest = hashlib.sha256(np.ascontiguousarray(logits, dtype=np.float32).tobytes()).hexdigest()
ref_digest = hashlib.sha256(np.ascontiguousarray(reference, dtype=np.float32).tobytes()).hexdigest()
print(json.dumps({
    "logits_shape": list(logits.shape),
    "logits_sha256": digest,
    "reference_sha256": ref_digest,
    "byte_identical": bool(np.array_equal(logits, reference)),
    "next_token": int(np.argmax(logits[0, -1])),
}))
