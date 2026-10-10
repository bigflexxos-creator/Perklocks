"""R3 Phase-7 instrumentation test — `RELIABILITY_VERBOSE_500` flag.

Scope (surgical, logging-only):

    1. Flag OFF  → middleware behavior exactly unchanged.
                   • No ``RELIABILITY_VERBOSE_500`` structured log line.
                   • Existing ``UNHANDLED`` unstructured log line still emitted.
                   • Client receives generic safe 500 JSON.
    2. Flag ON   → middleware emits one additional structured JSON log line
                   containing exc_type / exc_msg / traceback / request_id /
                   method / path.
                   • Existing ``UNHANDLED`` log line still emitted.
                   • Client response bytes IDENTICAL to the flag-OFF case
                     (traceback is NEVER returned to the HTTP client).

The test does NOT touch:
    * ``canonical_dedupe`` logic
    * ``dup-census`` route logic
    * migration / session / indexes / Phase 8 / scoring

Approach
--------
To avoid dragging the entire backend's heavy startup side effects into
unit-test scope, we extract the exact source of the
``_ReliabilityMiddleware`` class from ``/app/backend/server.py`` and
exec it inside an isolated namespace that supplies the handful of names
it references (``uuid``, ``_time_mod``, ``_traceback``, ``os``,
``logger``, ``Request``, ``JSONResponse``, ``BaseHTTPMiddleware``). We
then mount the real class on a tiny FastAPI app and assert its runtime
behaviour under both flag values.

This guarantees the assertions run against the true, byte-for-byte
production class — not a hand-written stand-in.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import time as _time_mod
import traceback as _traceback
import unittest
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware


_SERVER_PY = Path("/app/backend/server.py")


def _extract_reliability_class_source() -> str:
    """Return the exact source text of ``class _ReliabilityMiddleware``
    from ``/app/backend/server.py`` without importing the module.
    """
    src = _SERVER_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "_ReliabilityMiddleware":
            # ``ast.get_source_segment`` returns the exact text of the node.
            seg = ast.get_source_segment(src, node)
            if seg is None:  # pragma: no cover - defensive
                raise RuntimeError("could not slice _ReliabilityMiddleware source")
            return seg
    raise RuntimeError(                                        # pragma: no cover
        "_ReliabilityMiddleware class not found in /app/backend/server.py"
    )


def _build_app_with_reliability_middleware(capture_logger: logging.Logger):
    """Build an isolated FastAPI app that mounts a byte-identical copy
    of the real ``_ReliabilityMiddleware`` class.
    """
    class_src = _extract_reliability_class_source()
    namespace: dict = {
        "uuid":                uuid,
        "_time_mod":           _time_mod,
        "_traceback":          _traceback,
        "os":                  os,
        "logger":              capture_logger,
        "Request":             Request,
        "JSONResponse":        JSONResponse,
        "BaseHTTPMiddleware":  BaseHTTPMiddleware,
    }
    exec(compile(class_src, str(_SERVER_PY), "exec"), namespace)
    middleware_cls = namespace["_ReliabilityMiddleware"]

    shim = FastAPI()

    @shim.get("/boom")
    async def _boom():  # pragma: no cover - exercised via TestClient
        raise ValueError("kaboom")

    shim.add_middleware(middleware_cls)
    return shim


class _CaptureHandler(logging.Handler):
    """Captures formatted log records so we can assert on their content."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def formatted(self) -> list[str]:
        return [self.format(r) for r in self.records]


class ReliabilityVerbose500Test(unittest.TestCase):
    """Behavioural guarantees for the ``RELIABILITY_VERBOSE_500`` flag."""

    def setUp(self) -> None:
        self._logger = logging.getLogger("test.reliability_verbose_500")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        for h in list(self._logger.handlers):
            self._logger.removeHandler(h)
        self._capture = _CaptureHandler()
        self._capture.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(self._capture)
        self._saved_flag = os.environ.get("RELIABILITY_VERBOSE_500")

    def tearDown(self) -> None:
        self._logger.removeHandler(self._capture)
        if self._saved_flag is None:
            os.environ.pop("RELIABILITY_VERBOSE_500", None)
        else:
            os.environ["RELIABILITY_VERBOSE_500"] = self._saved_flag

    # ──────────────────────────────────────────────────────────────────
    # 1. Flag OFF → behavior unchanged
    # ──────────────────────────────────────────────────────────────────

    def test_flag_off_behavior_unchanged(self) -> None:
        os.environ.pop("RELIABILITY_VERBOSE_500", None)
        client = TestClient(
            _build_app_with_reliability_middleware(self._logger),
            raise_server_exceptions=False,
        )
        resp = client.get("/boom")

        # Client response — generic safe 500, no traceback leak.
        self.assertEqual(resp.status_code, 500)
        body = resp.json()
        self.assertEqual(body["error"], "Something went wrong — please retry.")
        self.assertIn("request_id", body)
        self.assertNotIn("traceback", body)
        self.assertNotIn("exc_type",  body)
        self.assertNotIn("exc_msg",   body)
        self.assertNotIn("kaboom",    json.dumps(body))

        # X-Request-ID header mirrors body.
        self.assertEqual(resp.headers.get("X-Request-ID"), body["request_id"])

        # Logs: the existing ``UNHANDLED`` line IS emitted, but the
        # structured verbose line is NOT.
        formatted = self._capture.formatted()
        self.assertTrue(
            any("UNHANDLED GET /boom" in m for m in formatted),
            "Existing UNHANDLED log line must still be emitted with flag OFF.",
        )
        self.assertFalse(
            any(m.startswith("RELIABILITY_VERBOSE_500 {") for m in formatted),
            "Structured verbose log line MUST NOT be emitted with flag OFF.",
        )

    # ──────────────────────────────────────────────────────────────────
    # 2. Flag ON → structured traceback logged; client response identical
    # ──────────────────────────────────────────────────────────────────

    def test_flag_on_emits_structured_traceback(self) -> None:
        os.environ["RELIABILITY_VERBOSE_500"] = "1"
        client = TestClient(
            _build_app_with_reliability_middleware(self._logger),
            raise_server_exceptions=False,
        )
        resp = client.get("/boom")

        # Client response — still generic safe 500, zero exposure.
        self.assertEqual(resp.status_code, 500)
        body = resp.json()
        self.assertEqual(body["error"], "Something went wrong — please retry.")
        self.assertIn("request_id", body)
        self.assertNotIn("traceback", body)
        self.assertNotIn("kaboom",    json.dumps(body))

        # Find the single structured JSON line.
        formatted = self._capture.formatted()
        verbose = [m for m in formatted if m.startswith("RELIABILITY_VERBOSE_500 {")]
        self.assertEqual(
            len(verbose), 1,
            f"Exactly ONE structured verbose log line expected; got "
            f"{len(verbose)}: {formatted}",
        )
        payload_json = verbose[0][len("RELIABILITY_VERBOSE_500 "):]
        payload      = json.loads(payload_json)

        # Required diagnostic fields.
        self.assertEqual(payload["evt"],        "RELIABILITY_VERBOSE_500")
        self.assertEqual(payload["method"],     "GET")
        self.assertEqual(payload["path"],       "/boom")
        self.assertEqual(payload["exc_type"],   "ValueError")
        self.assertEqual(payload["exc_msg"],    "kaboom")
        self.assertEqual(payload["request_id"], body["request_id"])

        # Traceback field — must contain the raising frame.
        self.assertIn("ValueError", payload["traceback"])
        self.assertIn("kaboom",     payload["traceback"])
        self.assertIn("_boom",      payload["traceback"])

        # Existing unstructured log line is still emitted (behavior
        # preserved).
        self.assertTrue(
            any("UNHANDLED GET /boom" in m for m in formatted),
            "Existing UNHANDLED log line must coexist with the structured line.",
        )

    # ──────────────────────────────────────────────────────────────────
    # 3. Flag=="0" is treated as OFF (defensive)
    # ──────────────────────────────────────────────────────────────────

    def test_flag_zero_is_off(self) -> None:
        os.environ["RELIABILITY_VERBOSE_500"] = "0"
        client = TestClient(
            _build_app_with_reliability_middleware(self._logger),
            raise_server_exceptions=False,
        )
        resp = client.get("/boom")
        self.assertEqual(resp.status_code, 500)
        formatted = self._capture.formatted()
        self.assertFalse(
            any(m.startswith("RELIABILITY_VERBOSE_500 {") for m in formatted),
            'Flag value "0" must behave identically to flag unset.',
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
