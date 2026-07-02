"""Verify MLB alt-line edge gates (2026-07-02 spec).

  • ALT TEAM TOTALS 2.5-3.5 range → require edge ≥ 8%
  • ALT RUN LINES +1.5 to +3.5   → require edge ≥ 8%
  • Non-alt versions pass unchanged.
"""
import sys
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv; load_dotenv("/app/backend/.env")
from quality_gate import _block_reason

tests = [
    # BLOCKED — alt-line, in range, low edge
    ({"sport":"MLB","market":"Yankees Team Total Over 3.5","is_alt":True,"edge_percent":5.0,"lock_score":90,"book_odds":-160}, True, "Team Total 3.5 edge 5% blocked"),
    ({"sport":"MLB","market":"Rays Team Total Under 2.5","is_alt":True,"edge_percent":6.5,"lock_score":91,"book_odds":-150}, True, "Team Total 2.5 edge 6.5% blocked"),
    ({"sport":"MLB","market":"Astros +2.5 Run Line","is_alt":True,"edge_percent":4.0,"lock_score":92,"book_odds":-160}, True, "Run Line +2.5 edge 4% blocked"),
    ({"sport":"MLB","market":"Braves +1.5 Spread (Alt)","is_alt":True,"edge_percent":7.9,"lock_score":92,"book_odds":-170}, True, "Run Line +1.5 edge 7.9% blocked (just below 8%)"),
    # PASS — alt-line, in range, EDGE MEETS BAR
    ({"sport":"MLB","market":"Dodgers Team Total Over 3.5","is_alt":True,"edge_percent":8.5,"lock_score":92,"book_odds":-160}, False, "Team Total 3.5 edge 8.5% passes"),
    ({"sport":"MLB","market":"Dodgers +2.5 Run Line","is_alt":True,"edge_percent":10.0,"lock_score":91,"book_odds":-170}, False, "Run Line +2.5 edge 10% passes"),
    ({"sport":"MLB","market":"Padres +3.5 Spread","is_alt":True,"edge_percent":12.5,"lock_score":90,"book_odds":-180}, False, "Run Line +3.5 edge 12.5% passes"),
    # PASS — non-alt versions bypass the gate
    ({"sport":"MLB","market":"Rangers Team Total Over 3.5","is_alt":False,"edge_percent":3.0,"lock_score":90,"book_odds":-160}, False, "Main Team Total ignored by alt gate"),
    ({"sport":"MLB","market":"Rangers +1.5 Run Line","is_alt":False,"edge_percent":3.0,"lock_score":90,"book_odds":-160}, False, "Main run line ignored by alt gate"),
    # PASS — alt-line but OUTSIDE the 2.5-3.5 / 1.5-3.5 range
    ({"sport":"MLB","market":"Reds Team Total Over 4.5","is_alt":True,"edge_percent":3.0,"lock_score":90,"book_odds":-160}, False, "Team Total 4.5 outside range"),
    ({"sport":"MLB","market":"Reds +4.5 Run Line","is_alt":True,"edge_percent":3.0,"lock_score":90,"book_odds":-160}, False, "Run Line +4.5 outside range"),
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
sys.exit(1 if fails else 0)
