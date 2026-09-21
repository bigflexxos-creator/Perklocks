"""P0 Root Closure Runtime Proof (2026-06)

Runs against the LIVE backend via httpx (not curl). Produces:
1. CFB sign-fix runtime verification: fetch current CFB canonical picks and
   report P(Over), Win Expected, Lock Score for the Indiana / South Alabama /
   Oregon fixtures.
2. List <-> Detail parity per CFB pick.
3. Historical Intelligence multi-sport delivery test — hits the SAME
   /api/picks/{id}/historical-intelligence endpoint used by Expo Go and
   reports: canonical_pick_id, subject_display_name, sample_size,
   hits/misses, opponent_summary presence, provenance, served_by.

Result is written to /tmp/p0_root_closure_proof.json.
"""
from __future__ import annotations
import json, os, sys, time
import asyncio
import httpx

BASE = os.environ.get("BACKEND_URL", "http://localhost:8001")
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PWD   = "demo123"

REFERENCE_PICK_IDS = {
    # from /app/memory/test_credentials.md (Sessions 5 & 142)
    "NFL_pass_yds":  "c800012c-9f1b-5199-a862-be83b52864db",
    "NFL_pass_yds2": "6d76acfa-6c03-560f-b0b8-8a1626adeee6",  # Trevor Lawrence
    "MLB_hits":      "86ed5d23-a5a6-4963-b8c3-36170a449b9f",
    "NFL_team_ml":   "78257671-a7af-5272-be4f-3ba50321d7e2",
    "CFB_spread":    "b0fff9e0-bbdd-5a4f-9e09-df9f70197439",
    "Tennis_ml":     "fe9dc637-ef7e-55a0-9971-8b64290ce7da",
    "NFL_alt":       "715e6aef-a566-53af-8f6f-81a54bb8418a",
}


async def _login(client: httpx.AsyncClient) -> str:
    r = await client.post(f"{BASE}/api/auth/login",
                          json={"email": DEMO_EMAIL, "password": DEMO_PWD})
    r.raise_for_status()
    return r.json()["access_token"]


async def _picks_by_sport(client: httpx.AsyncClient, token: str, sport: str):
    r = await client.get(
        f"{BASE}/api/picks/today",
        params={"sport": sport, "lite": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    picks = body.get("picks") or []
    return picks, body


async def _pick_detail(client: httpx.AsyncClient, token: str, pick_id: str):
    r = await client.get(
        f"{BASE}/api/picks/{pick_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if r.status_code >= 400:
        return {"error": r.status_code, "body": r.text[:400]}
    return r.json()


async def _hi(client: httpx.AsyncClient, token: str, pick_id: str, sample="L10"):
    try:
        r = await client.get(
            f"{BASE}/api/picks/{pick_id}/historical-intelligence",
            params={"sample_scope": sample, "venue_scope": "ALL"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
    except Exception as exc:
        return {"error": f"exc:{type(exc).__name__}:{exc}"}
    if r.status_code >= 400:
        try:
            return {"error": r.status_code, "body": r.json()}
        except Exception:
            return {"error": r.status_code, "text": r.text[:400]}
    return r.json()


async def main():
    out = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "base": BASE, "reference_ids": REFERENCE_PICK_IDS}
    async with httpx.AsyncClient() as client:
        token = await _login(client)
        out["auth"] = "PASS"

        # === CFB current published board ===
        cfb_picks, cfb_body = await _picks_by_sport(client, token, "CFB")
        out["cfb_pick_count"] = len(cfb_picks)
        out["cfb_pick_date"] = cfb_body.get("pick_date")

        target_names = [
            ("Northwestern", "Indiana"),
            ("South Alabama", "Kentucky"),
            ("Oregon", "Boise State"),
        ]
        out["cfb_fixtures"] = []
        for away, home in target_names:
            candidates = []
            for p in cfb_picks:
                ev = (p.get("event") or "").lower()
                if away.lower() in ev and home.lower() in ev:
                    candidates.append(p)
            for c in candidates[:5]:
                pid = c.get("id") or c.get("canonical_pick_id")
                detail = await _pick_detail(client, token, pid) if pid else None
                out["cfb_fixtures"].append({
                    "list": {
                        "id": pid,
                        "event": c.get("event"),
                        "market": c.get("market"),
                        "selection": c.get("selection"),
                        "line": c.get("line"),
                        "book_odds": c.get("book_odds"),
                        "win_probability": c.get("win_probability"),
                        "implied_probability": c.get("implied_probability"),
                        "edge_percent": c.get("edge_percent"),
                        "lock_score": c.get("lock_score"),
                        "cfb_engine_version": c.get("cfb_engine_version"),
                        "generation_id": c.get("generation_id"),
                    },
                    "detail_summary": None if not isinstance(detail, dict) else {
                        "id": detail.get("id"),
                        "market": detail.get("market"),
                        "line": detail.get("line"),
                        "book_odds": detail.get("book_odds"),
                        "win_probability": detail.get("win_probability"),
                        "implied_probability": detail.get("implied_probability"),
                        "edge_percent": detail.get("edge_percent"),
                        "lock_score": detail.get("lock_score"),
                        "cfb_engine_version": detail.get("cfb_engine_version"),
                        "generation_id": detail.get("generation_id"),
                        "parity_ok": (
                            detail.get("line") == c.get("line")
                            and detail.get("book_odds") == c.get("book_odds")
                            and abs((detail.get("win_probability") or 0)
                                    - (c.get("win_probability") or 0)) < 0.01
                            and abs((detail.get("lock_score") or 0)
                                    - (c.get("lock_score") or 0)) < 0.51
                        ),
                    },
                })

        # === Multi-sport HI probes ===
        out["hi_probes"] = {}
        for label, pid in REFERENCE_PICK_IDS.items():
            resp = await _hi(client, token, pid, sample="L10")
            if not isinstance(resp, dict):
                out["hi_probes"][label] = {"error": "non_dict", "raw": str(resp)[:200]}
                continue
            if "error" in resp:
                out["hi_probes"][label] = resp
                continue
            opp = resp.get("opponent_summary") or {}
            out["hi_probes"][label] = {
                "pick_id": pid,
                "sport": resp.get("sport"),
                "market_family": resp.get("market_family"),
                "entity_type": resp.get("entity_type"),
                "entity_name": resp.get("entity_name"),
                "opponent": (resp.get("pick") or {}).get("opponent"),
                "sample_size": resp.get("sample_size"),
                "hits": resp.get("hits"),
                "misses": resp.get("misses"),
                "hit_rate": resp.get("hit_rate"),
                "games_returned": len((resp.get("games") or [])),
                "vs_opp_sample": opp.get("n"),
                "vs_opp_hits": opp.get("hits"),
                "provenance": resp.get("provenance"),
                "status": resp.get("status"),
                "latency_ms": resp.get("latency_ms"),
                "served_by": resp.get("served_by"),
                "coverage_total_observations":
                    (resp.get("data_coverage") or {}).get("total_observations"),
                "coverage": resp.get("data_coverage"),
            }

    with open("/tmp/p0_root_closure_proof.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str)[:8000])


if __name__ == "__main__":
    asyncio.run(main())
