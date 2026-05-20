"""
engine.eval — Offline recall evaluation harness for Sovereign Memory.

Usage:
    python -m engine.eval.harness run --config baseline,with-expand,with-hyde
    python -m engine.eval.harness record --query "auth migration" --expected-ids 8412,8413

Gate rule: A feature may flip its default only after the harness shows
>=+5% recall@5 on the seed set with no regression on any query class.
"""
