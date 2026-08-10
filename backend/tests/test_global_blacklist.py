"""Verify global blacklist blocks all the banned families."""
import sys, os
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from quality_gate import _block_reason


def test_global_blacklist():
    tests = [
    # Should be BLOCKED (return a reason)
    ({"sport":"MLB","market":"Aaron Judge Hits + Runs + RBI Over 1.5","lock_score":95,"book_odds":-150}, False, "H+R+RBI NOT auto-blocked (Phase 4 removal)"),
    ({"sport":"MLB","market":"NRFI Yes","lock_score":90,"book_odds":-160}, True, "NRFI banned"),
    ({"sport":"Soccer","market":"Mbappe To Score or Assist","lock_score":92,"book_odds":-160}, True, "Score-or-assist banned"),
    ({"sport":"Soccer","market":"Kane Anytime Goal Scorer","lock_score":93,"book_odds":+140}, True, "Anytime GS banned"),
    ({"sport":"Soccer","market":"Bellingham Hat Trick","lock_score":92,"book_odds":+800}, True, "Hat trick banned"),
    ({"sport":"MLB","market":"Yankees Moneyline","lock_score":92,"book_odds":-160}, True, "MLB ML banned"),
    ({"sport":"MLB","market":"Judge Over 0.5 Hits","lock_score":82,"book_odds":-160}, True, "Lock dead-zone 82"),
    ({"sport":"MLB","market":"Judge Over 0.5 Hits","lock_score":92,"book_odds":-130}, False, "Odds -130 NOT auto-blocked (Phase 4 removal)"),
    # Should PASS (return None)
    ({"sport":"MLB","market":"Judge Over 0.5 Hits","lock_score":94,"book_odds":-200}, False, "Hits pass"),
    ({"sport":"MLB","market":"Cole Over 6.5 Strikeouts","lock_score":95,"book_odds":-115}, False, "Strikeouts -115 NOT auto-blocked (Phase 4 removal)"),
    ({"sport":"MLB","market":"Cole Over 6.5 Strikeouts","lock_score":95,"book_odds":-180}, False, "Strikeouts pass"),
    ({"sport":"Soccer","market":"Total Goals Over 2.5","lock_score":89,"book_odds":-160}, False, "Total Goals pass"),
    ({"sport":"Tennis","market":"Alcaraz Moneyline","lock_score":94,"book_odds":-250}, False, "Tennis ML pass"),
    ]
    print(f"{'':<3}{'Got':<7} {'Want':<7} {'Reason':<50}  Test")
    print('-'*100)
    fails = 0
    for p, should_block, name in tests:
        reason = _block_reason(p)
        got = 'BLOCK' if reason else 'PASS'
        want = 'BLOCK' if should_block else 'PASS'
        ok = '✓' if (bool(reason) == should_block) else '✗'
        if ok == '✗': fails += 1
        print(f"{ok:<3}{got:<7} {want:<7} {str(reason)[:48]:<50}  {name}")
    print(f'\n{fails} failures')
    assert fails == 0, f"{fails} global-blacklist assertions failed"
