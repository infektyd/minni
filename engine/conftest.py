"""Engine test hygiene.

The AFM generation-probe cache now persists across processes under
~/.minni/run/afm-probe-cache.json (see afm_provider.py). Tests must never read
or write live ~/.minni state, so every test gets the MINNI_AFM_PROBE_CACHE
override pointed at a per-test tmpdir. Tests that exercise the persistent
cache itself re-point the same env var at their own fixture file.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_afm_probe_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("MINNI_AFM_PROBE_CACHE", os.fspath(tmp_path / "afm-probe-cache.json"))
    yield
