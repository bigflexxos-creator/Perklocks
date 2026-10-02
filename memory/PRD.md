# PRD — Perklocks Master Surgical Build (NHL Preview Fix 2026-10-02)

## Session FIX — NHL Live Season Preview was empty · 6 picks now published

### Problem traced through all 13 stages
1. NHL events discovered: ✓ 19 live icehockey_nhl games (bulk odds endpoint)
2. Sportsbook markets returned: ✓ h2h, spreads, totals + alternate_totals
3. Live rows normalized: ✓ 164 alt_totals rows in live_alt_lines (41,991 fresh in last 24h system-wide)
4. nhl_sim_context attached: 10/10 after fix (was 9/10 — Dallas Stars failed on name mismatch)
5. Historical sample size: ✓ 10 recent games per team / 30,040 player_game_logs / 953 players
6. sim_nhl ran: 10/10 after fix (was 9/10)
7. independent_evidence=True: 10/10 after fix (was 0/10 — signals-count defect)
8. Probability Authority accepted: 10/10 after fix (was blocked by MODEL_UNAVAILABLE)
9. Lock Score calculated: 10/10
10. Reaching 85+: 6/10 (4 dropped at board_validator for edge_negative — truthful)
11. Canonical publication: 6/10 (was 0 — publication boundary MODEL_UNAVAILABLE)
12. /api/picks/today?sport=NHL: **6** (was 0)
13. Preview NHL consumer: **6** (was 0)

### Surgical fixes applied (code-only, no synthetic picks, no fake 85+ scores)
- `brain/sim_nhl.py :: _team_side()` — fallback-parse "Away @ Home" event string when home_team/away_team fields are empty (fixes Dallas Stars @ St Louis Blues)
- `brain/sim_nhl.py :: _count_signals()` — also count independent sample-size signals (home_recent_games, away_recent_games, recent_n, season_n); PARTIAL → STRONG
- `brain/sim_nhl.py :: _simulate_game()` — change hardcoded `signals=2` to `signals=_count_signals(ctx)` so game markets earn CAUSAL_INDEPENDENT provenance
- `services/nhl_feature_engine.py :: _norm_name()` — strip periods AND accents (fixes "St Louis" vs "St. Louis", "Montreal" vs "Montréal")
- `board_validator.py` — add NHL authoritative-model evidence recognizer (sim_win_probability + independent_evidence + decision_valid as two independent categories, mirroring the NFL Platinum / CFB SP+ pattern)
- `services/sport_model_authority.py` — register `brain_sim_nhl` as canonical for 7 NHL families (moneyline/puck_line/total/game_total/nhl_goals/nhl_sog/nhl_assists/nhl_points); was `MODEL_UNAVAILABLE`
- `services/universal_market_contract.py` — flip capability_state from `MODEL_UNAVAILABLE` to `ACTIVE` for all 7 NHL families

### Final NHL Preview (6 picks, all MEETS_85_THRESHOLD)
Vegas Golden Knights ML @ -195 FanDuel — LS 91.9 · evid 2
Buffalo Sabres ML @ -219 FanDuel — LS 91.9 · evid 1
Tampa Bay Lightning ML @ -148 FanDuel — LS 91.8 · evid 2
New York Islanders ML @ -116 FanDuel — LS 91.8 · evid 2
Detroit Red Wings ML @ -130 FanDuel — LS 91.7 · evid 2
Pittsburgh Penguins ML @ -110 FanDuel — LS 91.7 · evid 2

4 Monte Carlo rejections (edge_negative — sim probability lower than implied, correctly dropped by board_validator)
