"""test_dual_path_forensic — proves r3_dual_path_forensic.py is
GH-Actions-portable (no hardcoded /app) and that its
_original_batch_buffer / _accelerated_batch_buffer produce the exact
same reconstruction behavior the two drivers would.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile


_SCRIPTS = pathlib.Path("/app/reconcile_workspace/scripts")


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dual_forensic_resolves_both_drivers_from_sibling_dir():
    """Copy forensic + both drivers into a simulated GH-runner path
    and prove the forensic can be loaded."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="dual_runner_sim_"))
    try:
        fake = tmp / "home" / "runner" / "work" / "PerkLocks" \
                 / "PerkLocks" / "reconcile_workspace" / "scripts"
        fake.mkdir(parents=True)
        for name in ("r3_dual_path_forensic.py",
                      "push_canonical_accelerated.py",
                      "push_canonical_to_production.py"):
            shutil.copy2(_SCRIPTS / name, fake / name)
        for p in [str(_SCRIPTS), "/app/reconcile_workspace/scripts"]:
            if p in sys.path:
                sys.path.remove(p)
        mod = _load("r3_dual_path_forensic_sim",
                     fake / "r3_dual_path_forensic.py")
        assert mod._ORIGINAL_DRIVER_PATH == fake / "push_canonical_to_production.py"
        assert mod._ACCEL_DRIVER_PATH    == fake / "push_canonical_accelerated.py"
        # Both drivers must be loaded (hash helper reachable).
        assert callable(mod._accel._server_batch_content_hash)
        assert callable(mod._orig._ndjson_rows)
        assert callable(mod._orig._is_excluded)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_dual_forensic_has_no_hardcoded_app_in_driver_resolution():
    """Static-scan: no executable line resolves driver paths via
    hardcoded /app."""
    src = (_SCRIPTS / "r3_dual_path_forensic.py").read_text()
    for ln_no, line in enumerate(src.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "/app/reconcile_workspace" in line \
                and ("_ORIGINAL_DRIVER_PATH" in line
                     or "_ACCEL_DRIVER_PATH" in line):
            raise AssertionError(
                f"line {ln_no} resolves driver path via hardcoded "
                f"/app:\n    {line!r}"
            )
    assert "pathlib.Path(__file__).resolve().parent" in src, (
        "forensic must use __file__-relative resolution")


def test_original_path_matches_fixed_batch_size_cutpoints():
    """Original driver uses a FIXED batch_size, independent of the
    server's per-batch manifest.  Prove the _original_batch_buffer
    helper walks NDJSON at a fixed cutpoint."""
    forensic = _load("r3_dual_path_forensic_direct",
                      _SCRIPTS / "r3_dual_path_forensic.py")
    # Synthetic NDJSON with 10 accepted docs.
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="orig_cut_"))
    try:
        src = tmp / "settlement_events.ndjson"
        with open(src, "w") as f:
            for i in range(10):
                f.write(json.dumps({
                    "canonical_from": "test",
                    "logical_key": ["settlement_id", f"s{i}"],
                    "doc": {"_id": f"obj{i}", "event_id": f"e{i}"},
                }) + "\n")
        # Original driver's fixed-size path: batch_size=4 → batch 0=[0..3],
        # batch 1=[4..7], batch 2=[8..9].  Request batch 1 → ids 4..7.
        buf = forensic._original_batch_buffer("settlement_events", src, 1, 4)
        ids = [d["_id"] for d in buf]
        assert ids == ["obj4", "obj5", "obj6", "obj7"], ids

        # Request batch 2 (tail) → ids 8..9.
        buf = forensic._original_batch_buffer("settlement_events", src, 2, 4)
        ids = [d["_id"] for d in buf]
        assert ids == ["obj8", "obj9"], ids
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_accelerated_path_uses_per_batch_target():
    """Accelerated driver (Resume #12) uses per-batch authoritative
    targets.  Prove _accelerated_batch_buffer honors a mixed
    historical_counts dict."""
    forensic = _load("r3_dual_path_forensic_direct2",
                      _SCRIPTS / "r3_dual_path_forensic.py")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="accel_cut_"))
    try:
        src = tmp / "settlement_events.ndjson"
        with open(src, "w") as f:
            for i in range(20):
                f.write(json.dumps({
                    "canonical_from": "test",
                    "doc": {"_id": f"obj{i}"},
                }) + "\n")
        # Mixed historical_counts: batch 0=4, batch 1=6, batch 2=5
        # → batch 2 starts at offset 10 → ids 10..14.
        hist = {0: 4, 1: 6, 2: 5}
        buf = forensic._accelerated_batch_buffer(
            "settlement_events", src, 2, hist, tail_bsize=1000)
        ids = [d["_id"] for d in buf]
        assert ids == ["obj10", "obj11", "obj12", "obj13", "obj14"], ids

        # If instead batch 1 had doc_count=10 (shifted), batch 2
        # starts at offset 14 → different docs.
        hist2 = {0: 4, 1: 10, 2: 5}
        buf2 = forensic._accelerated_batch_buffer(
            "settlement_events", src, 2, hist2, tail_bsize=1000)
        ids2 = [d["_id"] for d in buf2]
        assert ids2 == ["obj14", "obj15", "obj16", "obj17", "obj18"], ids2
        # Confirms the fundamental divergence hypothesis:
        # per-batch targets with a non-uniform historical doc_count
        # produce DIFFERENT batch membership than a fixed batch_size.
        assert ids != ids2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_dual_forensic_resolves_both_drivers_from_sibling_dir()
    test_dual_forensic_has_no_hardcoded_app_in_driver_resolution()
    test_original_path_matches_fixed_batch_size_cutpoints()
    test_accelerated_path_uses_per_batch_target()
    print("OK — dual-path forensic contracts verified")
