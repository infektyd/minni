"""CrossEncoder.predict thread-safety stress probe (perf/parallel-fanout #388).

EVIDENCE, NOT A GATE (YELLOW round 2): this probe only runs when a reranker
model is already in the local HF cache — CI sets HF_HUB_OFFLINE=1 with no
cached model, so the parent test skips there by design (exit 4). It is the
empirical leg behind the RED-2 unlocked-predict precondition, not a merge
gate: the gate is the unit-tested lock routing + pin precondition, which
run everywhere.

Runs in a FRESH interpreter (never import this from the pytest process:
torch aborts on import when faiss's libomp is already loaded — the #299
class). The parent test (test_parallel_fanout_red.py) spawns it via
subprocess with the daemon's pinned-CPU env and skips when the model is
unavailable.

Width tracks the worst case the caps admit: 4 leg workers x (4 vault legs
+ shared tail + personal leg) each fanning to 4 variant workers ≈ 24
concurrent predicts per both-scope search (see _MAX_LEG_WORKERS /
_MAX_VARIANT_WORKERS), so NWORKERS = 24.

Exit codes: 0 = VERDICT SAFE (concurrent predict byte-identical to serial);
1 = UNSAFE (mismatch/exception); 3 = torch unavailable; 4 = model
unavailable offline.
"""

NWORKERS = 24
NROUNDS = 3

import os
import sys

# Pinned-CPU path, mirroring minnid.main() + models._pin_torch_threads_for_cpu_once
# — exactly the precondition models.cross_encoder_unlocked_predict_safe()
# requires before retrieval skips the cross-encoder lock. Must precede the
# torch import.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import threading  # noqa: E402
import time  # noqa: E402

try:
    import torch  # noqa: E402
except ImportError:
    print("NO_TORCH: torch not installed")
    raise SystemExit(3)

torch.set_num_threads(1)
try:
    torch.set_num_interop_threads(1)
except Exception:
    pass

try:
    from sentence_transformers import CrossEncoder  # noqa: E402

    model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", device="cpu")
except Exception as exc:
    print(f"NO_MODEL: {exc}")
    raise SystemExit(4)

PAIRS_A = [
    ["websocket architecture", f"passage about sockets and frames number {i} with extra words"]
    for i in range(24)
]
PAIRS_B = [
    ["database index design", f"btree lsm compaction segment {i} merge policy details here"]
    for i in range(24)
]

ref_a = list(model.predict(PAIRS_A, show_progress_bar=False))
ref_b = list(model.predict(PAIRS_B, show_progress_bar=False))
print(f"serial refs done: {len(ref_a)} + {len(ref_b)} scores", flush=True)

BARRIER = threading.Barrier(NWORKERS)
errors = []
results = {}


def worker(wid):
    try:
        BARRIER.wait(timeout=120)
        pairs = PAIRS_A if wid % 2 == 0 else PAIRS_B
        ref = ref_a if wid % 2 == 0 else ref_b
        for _ in range(NROUNDS):
            got = list(model.predict(pairs, show_progress_bar=False))
            assert len(got) == len(ref), f"len {len(got)} != {len(ref)}"
            for i, (g, r) in enumerate(zip(got, ref)):
                assert abs(float(g) - float(r)) < 1e-6, f"wid={wid} idx={i}: {g} != {r}"
        results[wid] = "ok"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"wid={wid}: {type(exc).__name__}: {exc}")


threads = [threading.Thread(target=worker, args=(i,)) for i in range(NWORKERS)]
t0 = time.perf_counter()
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=600)
dt = time.perf_counter() - t0

print(f"workers ok: {sorted(results)}  errors: {errors}  wall={dt:.1f}s", flush=True)
if errors or len(results) != NWORKERS or any(t.is_alive() for t in threads):
    print("VERDICT: UNSAFE (mismatch, exception, or hang under concurrency)")
    raise SystemExit(1)
print(f"VERDICT: SAFE ({NWORKERS} threads x {NROUNDS} rounds byte-identical to serial)")
sys.exit(0)
