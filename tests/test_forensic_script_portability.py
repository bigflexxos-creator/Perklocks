"""test_forensic_script_portability — proves ``r3_single_batch_forensic``
can locate ``push_canonical_accelerated.py`` from any repo checkout,
including a GitHub Actions runner path like
``/home/runner/work/PerkLocks/PerkLocks/reconcile_workspace/scripts``.

Why this test exists
────────────────────
Single-Batch Forensic #1 failed with
``FileNotFoundError: '/app/reconcile_workspace/scripts/push_canonical_accelerated.py'``
because the forensic script hardcoded the Emergent container path.
This test simulates a non-``/app`` checkout and asserts the forensic
resolves its sibling driver purely via ``__file__``.
"""
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import sys
import tempfile


def test_forensic_resolves_driver_relative_to_file_in_tmp_checkout():
    """Copy forensic + driver into a non-/app directory and prove the
    forensic can be imported successfully, loading the driver via
    its own ``__file__``-relative path resolution."""
    src_dir = pathlib.Path("/app/reconcile_workspace/scripts")
    forensic = src_dir / "r3_single_batch_forensic.py"
    driver   = src_dir / "push_canonical_accelerated.py"
    assert forensic.exists(), forensic
    assert driver.exists(),   driver

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="runner_sim_"))
    try:
        # Simulate a GH-Actions-style checkout path.
        fake_runner = tmp / "home" / "runner" / "work" / "PerkLocks" \
                        / "PerkLocks" / "reconcile_workspace" / "scripts"
        fake_runner.mkdir(parents=True)
        shutil.copy2(forensic, fake_runner / forensic.name)
        shutil.copy2(driver,   fake_runner / driver.name)

        # ``/app`` MUST NOT be in sys.path for this test — we are
        # proving the forensic doesn't fall back to it.
        for p in [str(src_dir), "/app/reconcile_workspace/scripts"]:
            if p in sys.path:
                sys.path.remove(p)

        # Load the forensic from the simulated runner path.  Its
        # top-level ``_driver_path`` block resolves via ``__file__``
        # and will raise FileNotFoundError if the hardcoded ``/app``
        # path were still used.
        spec = importlib.util.spec_from_file_location(
            "r3_single_batch_forensic_runner_sim",
            fake_runner / "r3_single_batch_forensic.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # The forensic exposes ``_driver_path`` at module scope.
        assert hasattr(mod, "_driver_path"), \
            "forensic should expose _driver_path for sanity"
        assert mod._driver_path == fake_runner / "push_canonical_accelerated.py", (
            f"forensic resolved driver at {mod._driver_path!r} but should have "
            f"resolved it at {fake_runner / 'push_canonical_accelerated.py'!r} "
            "(i.e. sibling of the forensic script via __file__)"
        )

        # Also confirm the driver module was actually loaded via its
        # replicated hash helper.
        assert hasattr(mod, "_mod"), "forensic should expose _mod"
        assert callable(getattr(mod._mod, "_server_batch_content_hash", None)), (
            "driver's _server_batch_content_hash should be callable "
            "after forensic loaded it"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_forensic_does_not_contain_hardcoded_app_in_driver_resolution():
    """The code line that resolves the driver must not reference
    ``/app`` directly.  Comments are fine; the executable statement
    isn't."""
    src = pathlib.Path(
        "/app/reconcile_workspace/scripts/r3_single_batch_forensic.py"
    ).read_text()
    # Walk the file line-by-line and skip comment lines.
    for ln_no, line in enumerate(src.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "/app/reconcile_workspace" in line \
                and "_driver_path" in line:
            raise AssertionError(
                f"line {ln_no} resolves _driver_path with a "
                f"hardcoded /app path:\n    {line!r}"
            )
    # Also assert the __file__-based resolution is present.
    assert "pathlib.Path(__file__).resolve().parent" in src, (
        "forensic must resolve its driver path via "
        "pathlib.Path(__file__).resolve().parent"
    )


if __name__ == "__main__":
    test_forensic_resolves_driver_relative_to_file_in_tmp_checkout()
    test_forensic_does_not_contain_hardcoded_app_in_driver_resolution()
    print("OK — forensic script is portable (no hardcoded /app in driver resolution)")
