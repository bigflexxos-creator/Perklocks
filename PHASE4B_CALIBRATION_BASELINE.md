# Phase 4B — Calibration Baseline (READ-ONLY)

**Generated:** `2026-08-06T20:02:16.630500+00:00`
**Segmentation version:** `4B.0.0`
**Scanned picks:** 12,092
**Scored picks (post-filter):** 12,092
**Elapsed:** 0.5s
**Min sample for metrics:** 30

Buckets below the min-sample threshold are marked `INSUFFICIENT_SAMPLE` — their raw counts are shown but no ROI / Brier / log-loss / calibration metrics.

## Global
| Bucket | n | W | L | P | V | HitRate | AvgPred | AvgOdds | Brier | LogLoss | ROI | Units | CLV | CalGap |
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
| global | 12092 | 3418 | 2886 | 4 | 5784 | 0.5422 | 0.6485 | -133.7 | 0.22796 | 0.65703 | -9.68% | -609.926 | None | 0.1063 |

## by_sport
| Bucket | n | W | L | P | V | HitRate | AvgPred | AvgOdds | Brier | LogLoss | ROI | Units | CLV | CalGap |
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
| MLB | 2736 | 1691 | 939 | 0 | 106 | 0.643 | 0.7145 | -274.1 | 0.23144 | 0.66222 | -9.7% | -255.241 | None | 0.0716 |
| NBA | 47 | 32 | 14 | 0 | 1 | 0.6957 | 0.8073 | -482.1 | 0.22227 | 0.64946 | -12.38% | -5.697 | None | 0.1116 |
| Soccer | 6339 | 955 | 1555 | 0 | 3829 | 0.3805 | 0.5515 | 75.1 | 0.2288 | 0.66561 | -11.41% | -286.288 | None | 0.171 |
| Tennis | 2930 | 732 | 372 | 4 | 1822 | 0.663 | 0.7054 | -278.3 | 0.21805 | 0.62564 | -5.39% | -59.559 | None | 0.0424 |
| UFC | 40 | 8 | 6 | 0 | 26 | 0.5714 | 0.6444 | -267.7 | 0.22545 | 0.64551 | -22.44% | -3.142 | None | 0.0729 |

## by_sport_market
| Bucket | n | W | L | P | V | HitRate | AvgPred | AvgOdds | Brier | LogLoss | ROI | Units | CLV | CalGap |
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
| MLB|1st | 183 | 81 | 81 | 0 | 21 | 0.5 | 0.535 | None | 0.2395 | 0.6686 | -50.0% | -81.0 | None | 0.035 |
| MLB|moneyline | 6 | 4 | 2 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|other | 137 | 85 | 52 | 0 | 0 | 0.6204 | 0.7057 | -208.0 | 0.23381 | 0.70958 | -7.07% | -9.681 | None | 0.0853 |
| MLB|player_prop | 1948 | 1201 | 667 | 0 | 80 | 0.6429 | 0.7203 | -290.5 | 0.23462 | 0.66632 | -9.41% | -175.686 | None | 0.0774 |
| MLB|spread | 180 | 115 | 62 | 0 | 3 | 0.6497 | 0.7093 | -184.6 | 0.23576 | 0.66781 | 0.55% | 0.981 | None | 0.0596 |
| MLB|totals | 282 | 205 | 75 | 0 | 2 | 0.7321 | 0.7872 | -254.1 | 0.20163 | 0.6044 | 3.65% | 10.214 | None | 0.0551 |
| NBA|other | 47 | 32 | 14 | 0 | 1 | 0.6957 | 0.8073 | -482.1 | 0.22227 | 0.64946 | -12.38% | -5.697 | None | 0.1116 |
| Soccer|double_chance | 151 | 108 | 35 | 0 | 8 | 0.7552 | 0.7873 | -277.5 | 0.17709 | 0.53298 | 3.37% | 4.817 | None | 0.0321 |
| Soccer|moneyline | 44 | 28 | 16 | 0 | 0 | 0.6364 | 0.7776 | -156.1 | 0.2572 | 0.72286 | 5.08% | 2.235 | None | 0.1413 |
| Soccer|other | 2212 | 251 | 565 | 0 | 1396 | 0.3076 | 0.4516 | -14.5 | 0.22842 | 0.69518 | -37.5% | -305.982 | None | 0.144 |
| Soccer|player_prop | 3472 | 295 | 795 | 0 | 2382 | 0.2706 | 0.5206 | 314.1 | 0.23853 | 0.6726 | 2.35% | 25.589 | None | 0.25 |
| Soccer|totals | 460 | 273 | 144 | 0 | 43 | 0.6547 | 0.723 | -228.8 | 0.21883 | 0.62893 | -3.11% | -12.948 | None | 0.0683 |
| Tennis|moneyline | 2089 | 407 | 227 | 0 | 1455 | 0.642 | 0.7022 | -286.2 | 0.22865 | 0.65001 | -10.09% | -63.966 | None | 0.0603 |
| Tennis|other | 408 | 51 | 12 | 0 | 345 | 0.8095 | 0.6629 | -322.4 | 0.18351 | 0.55403 | 13.17% | 8.299 | None | -0.1466 |
| Tennis|spread | 2 | 1 | 1 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Tennis|totals | 431 | 273 | 132 | 4 | 22 | 0.6741 | 0.7172 | -259.8 | 0.20645 | 0.59783 | -0.92% | -3.725 | None | 0.0431 |
| UFC|moneyline | 21 | 6 | 6 | 0 | 9 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|other | 13 | 2 | 0 | 0 | 11 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|totals | 6 | 0 | 0 | 0 | 6 | — | — | — | — | — | — | — | — | INSUFFICIENT |

## by_sport_market_side
| Bucket | n | W | L | P | V | HitRate | AvgPred | AvgOdds | Brier | LogLoss | ROI | Units | CLV | CalGap |
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
| MLB|1st|NRFI | 169 | 74 | 75 | 0 | 20 | 0.4966 | 0.5379 | None | 0.23956 | 0.66834 | -50.34% | -75.0 | None | 0.0413 |
| MLB|1st|YRFI | 14 | 7 | 6 | 0 | 1 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|moneyline|unknown | 6 | 4 | 2 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|other|unknown | 137 | 85 | 52 | 0 | 0 | 0.6204 | 0.7057 | -208.0 | 0.23381 | 0.70958 | -7.07% | -9.681 | None | 0.0853 |
| MLB|player_prop|unknown | 1948 | 1201 | 667 | 0 | 80 | 0.6429 | 0.7203 | -290.5 | 0.23462 | 0.66632 | -9.41% | -175.686 | None | 0.0774 |
| MLB|spread|unknown | 180 | 115 | 62 | 0 | 3 | 0.6497 | 0.7093 | -184.6 | 0.23576 | 0.66781 | 0.55% | 0.981 | None | 0.0596 |
| MLB|totals|unknown | 282 | 205 | 75 | 0 | 2 | 0.7321 | 0.7872 | -254.1 | 0.20163 | 0.6044 | 3.65% | 10.214 | None | 0.0551 |
| NBA|other|unknown | 47 | 32 | 14 | 0 | 1 | 0.6957 | 0.8073 | -482.1 | 0.22227 | 0.64946 | -12.38% | -5.697 | None | 0.1116 |
| Soccer|double_chance|unknown | 151 | 108 | 35 | 0 | 8 | 0.7552 | 0.7873 | -277.5 | 0.17709 | 0.53298 | 3.37% | 4.817 | None | 0.0321 |
| Soccer|moneyline|unknown | 44 | 28 | 16 | 0 | 0 | 0.6364 | 0.7776 | -156.1 | 0.2572 | 0.72286 | 5.08% | 2.235 | None | 0.1413 |
| Soccer|other|away | 40 | 3 | 10 | 0 | 27 | 0.2308 | 0.5346 | 1.6 | 0.34221 | 0.88762 | -42.46% | -5.52 | None | 0.3038 |
| Soccer|other|home | 49 | 5 | 14 | 0 | 30 | 0.2632 | 0.5212 | -12.6 | 0.28044 | 0.77318 | -48.32% | -9.181 | None | 0.258 |
| Soccer|other|unknown | 2123 | 243 | 541 | 0 | 1339 | 0.3099 | 0.4486 | -14.8 | 0.22528 | 0.6901 | -37.15% | -291.28 | None | 0.1386 |
| Soccer|player_prop|away | 16 | 0 | 7 | 0 | 9 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|player_prop|home | 103 | 9 | 6 | 0 | 88 | 0.6 | 0.3679 | 119.1 | 0.27336 | 0.73574 | 123.13% | 18.469 | None | -0.2321 |
| Soccer|player_prop|unknown | 3353 | 286 | 782 | 0 | 2285 | 0.2678 | 0.5242 | 319.3 | 0.23895 | 0.67372 | 1.32% | 14.12 | None | 0.2564 |
| Soccer|totals|unknown | 460 | 273 | 144 | 0 | 43 | 0.6547 | 0.723 | -228.8 | 0.21883 | 0.62893 | -3.11% | -12.948 | None | 0.0683 |
| Tennis|moneyline|unknown | 2089 | 407 | 227 | 0 | 1455 | 0.642 | 0.7022 | -286.2 | 0.22865 | 0.65001 | -10.09% | -63.966 | None | 0.0603 |
| Tennis|other|unknown | 408 | 51 | 12 | 0 | 345 | 0.8095 | 0.6629 | -322.4 | 0.18351 | 0.55403 | 13.17% | 8.299 | None | -0.1466 |
| Tennis|spread|unknown | 2 | 1 | 1 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Tennis|totals|unknown | 431 | 273 | 132 | 4 | 22 | 0.6741 | 0.7172 | -259.8 | 0.20645 | 0.59783 | -0.92% | -3.725 | None | 0.0431 |
| UFC|moneyline|unknown | 21 | 6 | 6 | 0 | 9 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|other|unknown | 13 | 2 | 0 | 0 | 11 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|totals|unknown | 6 | 0 | 0 | 0 | 6 | — | — | — | — | — | — | — | — | INSUFFICIENT |

## by_sport_market_side_line
| Bucket | n | W | L | P | V | HitRate | AvgPred | AvgOdds | Brier | LogLoss | ROI | Units | CLV | CalGap |
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
| MLB|1st|NRFI|None | 169 | 74 | 75 | 0 | 20 | 0.4966 | 0.5379 | None | 0.23956 | 0.66834 | -50.34% | -75.0 | None | 0.0413 |
| MLB|1st|YRFI|None | 14 | 7 | 6 | 0 | 1 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|moneyline|unknown|None | 6 | 4 | 2 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|other|unknown|None | 137 | 85 | 52 | 0 | 0 | 0.6204 | 0.7057 | -208.0 | 0.23381 | 0.70958 | -7.07% | -9.681 | None | 0.0853 |
| MLB|player_prop|unknown|None | 1948 | 1201 | 667 | 0 | 80 | 0.6429 | 0.7203 | -290.5 | 0.23462 | 0.66632 | -9.41% | -175.686 | None | 0.0774 |
| MLB|spread|unknown|None | 180 | 115 | 62 | 0 | 3 | 0.6497 | 0.7093 | -184.6 | 0.23576 | 0.66781 | 0.55% | 0.981 | None | 0.0596 |
| MLB|totals|unknown|None | 282 | 205 | 75 | 0 | 2 | 0.7321 | 0.7872 | -254.1 | 0.20163 | 0.6044 | 3.65% | 10.214 | None | 0.0551 |
| NBA|other|unknown|None | 47 | 32 | 14 | 0 | 1 | 0.6957 | 0.8073 | -482.1 | 0.22227 | 0.64946 | -12.38% | -5.697 | None | 0.1116 |
| Soccer|double_chance|unknown|None | 151 | 108 | 35 | 0 | 8 | 0.7552 | 0.7873 | -277.5 | 0.17709 | 0.53298 | 3.37% | 4.817 | None | 0.0321 |
| Soccer|moneyline|unknown|None | 44 | 28 | 16 | 0 | 0 | 0.6364 | 0.7776 | -156.1 | 0.2572 | 0.72286 | 5.08% | 2.235 | None | 0.1413 |
| Soccer|other|away|None | 40 | 3 | 10 | 0 | 27 | 0.2308 | 0.5346 | 1.6 | 0.34221 | 0.88762 | -42.46% | -5.52 | None | 0.3038 |
| Soccer|other|home|None | 49 | 5 | 14 | 0 | 30 | 0.2632 | 0.5212 | -12.6 | 0.28044 | 0.77318 | -48.32% | -9.181 | None | 0.258 |
| Soccer|other|unknown|None | 2123 | 243 | 541 | 0 | 1339 | 0.3099 | 0.4486 | -14.8 | 0.22528 | 0.6901 | -37.15% | -291.28 | None | 0.1386 |
| Soccer|player_prop|away|None | 16 | 0 | 7 | 0 | 9 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|player_prop|home|None | 103 | 9 | 6 | 0 | 88 | 0.6 | 0.3679 | 119.1 | 0.27336 | 0.73574 | 123.13% | 18.469 | None | -0.2321 |
| Soccer|player_prop|unknown|None | 3353 | 286 | 782 | 0 | 2285 | 0.2678 | 0.5242 | 319.3 | 0.23895 | 0.67372 | 1.32% | 14.12 | None | 0.2564 |
| Soccer|totals|unknown|None | 460 | 273 | 144 | 0 | 43 | 0.6547 | 0.723 | -228.8 | 0.21883 | 0.62893 | -3.11% | -12.948 | None | 0.0683 |
| Tennis|moneyline|unknown|None | 2089 | 407 | 227 | 0 | 1455 | 0.642 | 0.7022 | -286.2 | 0.22865 | 0.65001 | -10.09% | -63.966 | None | 0.0603 |
| Tennis|other|unknown|None | 408 | 51 | 12 | 0 | 345 | 0.8095 | 0.6629 | -322.4 | 0.18351 | 0.55403 | 13.17% | 8.299 | None | -0.1466 |
| Tennis|spread|unknown|None | 2 | 1 | 1 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Tennis|totals|unknown|None | 431 | 273 | 132 | 4 | 22 | 0.6741 | 0.7172 | -259.8 | 0.20645 | 0.59783 | -0.92% | -3.725 | None | 0.0431 |
| UFC|moneyline|unknown|None | 21 | 6 | 6 | 0 | 9 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|other|unknown|None | 13 | 2 | 0 | 0 | 11 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|totals|unknown|None | 6 | 0 | 0 | 0 | 6 | — | — | — | — | — | — | — | — | INSUFFICIENT |

## by_sport_market_side_odds
| Bucket | n | W | L | P | V | HitRate | AvgPred | AvgOdds | Brier | LogLoss | ROI | Units | CLV | CalGap |
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
| MLB|1st|NRFI|None | 169 | 74 | 75 | 0 | 20 | 0.4966 | 0.5379 | None | 0.23956 | 0.66834 | -50.34% | -75.0 | None | 0.0413 |
| MLB|1st|YRFI|None | 14 | 7 | 6 | 0 | 1 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|moneyline|unknown|chalk | 4 | 3 | 1 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|moneyline|unknown|deep_chalk | 1 | 0 | 1 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|moneyline|unknown|light_fav | 1 | 1 | 0 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|other|unknown|chalk | 70 | 46 | 24 | 0 | 0 | 0.6571 | 0.7327 | -231.8 | 0.22671 | 0.64684 | -6.06% | -4.241 | None | 0.0756 |
| MLB|other|unknown|deep_chalk | 23 | 18 | 5 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|other|unknown|even | 1 | 0 | 1 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|other|unknown|light_dog | 8 | 3 | 5 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|other|unknown|light_fav | 19 | 10 | 9 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|other|unknown|moderate_fav | 16 | 8 | 8 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|player_prop|unknown|chalk | 1068 | 634 | 377 | 0 | 57 | 0.6271 | 0.6978 | -224.4 | 0.24179 | 0.68022 | -9.14% | -92.42 | None | 0.0707 |
| MLB|player_prop|unknown|deep_chalk | 533 | 370 | 151 | 0 | 12 | 0.7102 | 0.8104 | -508.3 | 0.2136 | 0.62629 | -14.43% | -75.19 | None | 0.1002 |
| MLB|player_prop|unknown|light_fav | 107 | 59 | 47 | 0 | 1 | 0.5566 | 0.6432 | -131.7 | 0.24661 | 0.68548 | -1.98% | -2.104 | None | 0.0866 |
| MLB|player_prop|unknown|moderate_fav | 240 | 138 | 92 | 0 | 10 | 0.6 | 0.6508 | -160.8 | 0.24517 | 0.68704 | -2.6% | -5.973 | None | 0.0508 |
| MLB|spread|unknown|chalk | 117 | 78 | 36 | 0 | 3 | 0.6842 | 0.7221 | -195.9 | 0.22244 | 0.64025 | 3.66% | 4.168 | None | 0.0379 |
| MLB|spread|unknown|moderate_fav | 63 | 37 | 26 | 0 | 0 | 0.5873 | 0.6861 | -164.0 | 0.25987 | 0.71767 | -5.06% | -3.187 | None | 0.0988 |
| MLB|totals|unknown|chalk | 181 | 126 | 53 | 0 | 2 | 0.7039 | 0.7752 | -235.9 | 0.2133 | 0.62674 | 0.7% | 1.246 | None | 0.0713 |
| MLB|totals|unknown|deep_chalk | 71 | 54 | 17 | 0 | 0 | 0.7606 | 0.8438 | -336.7 | 0.19288 | 0.59828 | -1.41% | -1.001 | None | 0.0832 |
| MLB|totals|unknown|moderate_fav | 30 | 25 | 5 | 0 | 0 | 0.8333 | 0.7254 | -167.2 | 0.15267 | 0.48565 | 33.23% | 9.968 | None | -0.108 |
| NBA|other|unknown|chalk | 7 | 4 | 3 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| NBA|other|unknown|deep_chalk | 37 | 26 | 11 | 0 | 0 | 0.7027 | 0.8396 | -547.2 | 0.21907 | 0.64504 | -16.23% | -6.006 | None | 0.1369 |
| NBA|other|unknown|light_fav | 2 | 1 | 0 | 0 | 1 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| NBA|other|unknown|moderate_fav | 1 | 1 | 0 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|double_chance|unknown|chalk | 87 | 55 | 28 | 0 | 4 | 0.6627 | 0.7639 | -227.1 | 0.23072 | 0.65818 | -4.65% | -3.859 | None | 0.1013 |
| Soccer|double_chance|unknown|deep_chalk | 56 | 49 | 4 | 0 | 3 | 0.9245 | 0.8319 | -369.9 | 0.07799 | 0.30089 | 17.74% | 9.403 | None | -0.0926 |
| Soccer|double_chance|unknown|moderate_fav | 8 | 4 | 3 | 0 | 1 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|moneyline|unknown|chalk | 11 | 7 | 4 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|moneyline|unknown|deep_chalk | 3 | 2 | 1 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|moneyline|unknown|even | 2 | 1 | 1 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|moneyline|unknown|light_fav | 13 | 8 | 5 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|moneyline|unknown|moderate_fav | 15 | 10 | 5 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|other|away|even | 40 | 3 | 10 | 0 | 27 | 0.2308 | 0.5346 | 1.6 | 0.34221 | 0.88762 | -42.46% | -5.52 | None | 0.3038 |
| Soccer|other|home|even | 49 | 5 | 14 | 0 | 30 | 0.2632 | 0.5212 | -12.6 | 0.28044 | 0.77318 | -48.32% | -9.181 | None | 0.258 |
| Soccer|other|unknown|None | 1 | 0 | 0 | 0 | 1 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|other|unknown|chalk | 244 | 35 | 27 | 0 | 182 | 0.5645 | 0.6259 | -224.3 | 0.21854 | 0.75309 | -19.2% | -11.902 | None | 0.0614 |
| Soccer|other|unknown|deep_chalk | 239 | 27 | 21 | 0 | 191 | 0.5625 | 0.4602 | -498.6 | 0.30668 | 1.15173 | -29.52% | -14.172 | None | -0.1023 |
| Soccer|other|unknown|deep_dog | 109 | 5 | 33 | 0 | 71 | 0.1316 | 0.3195 | 869.0 | 0.14621 | 0.46468 | 19.55% | 7.43 | None | 0.1879 |
| Soccer|other|unknown|even | 15 | 2 | 2 | 0 | 11 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|other|unknown|light_dog | 483 | 25 | 120 | 0 | 338 | 0.1724 | 0.3379 | 149.2 | 0.19374 | 0.62743 | -57.22% | -82.97 | None | 0.1655 |
| Soccer|other|unknown|light_fav | 544 | 127 | 245 | 0 | 172 | 0.3414 | 0.5069 | -111.3 | 0.24998 | 0.68981 | -35.0% | -130.202 | None | 0.1655 |
| Soccer|other|unknown|mid_dog | 270 | 9 | 64 | 0 | 197 | 0.1233 | 0.2751 | 272.5 | 0.16096 | 0.59305 | -53.64% | -39.16 | None | 0.1518 |
| Soccer|other|unknown|moderate_fav | 218 | 13 | 29 | 0 | 176 | 0.3095 | 0.4532 | -160.5 | 0.22056 | 0.67591 | -49.63% | -20.843 | None | 0.1437 |
| Soccer|player_prop|away|even | 14 | 0 | 7 | 0 | 7 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|player_prop|away|mid_dog | 2 | 0 | 0 | 0 | 2 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|player_prop|home|even | 43 | 2 | 6 | 0 | 35 | 0.25 | 0.3285 | -39.2 | 0.21105 | 0.60574 | -31.63% | -2.531 | None | 0.0785 |
| Soccer|player_prop|home|light_fav | 20 | 0 | 0 | 0 | 20 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|player_prop|home|mid_dog | 40 | 7 | 0 | 0 | 33 | 1.0 | 0.413 | 300.0 | 0.34457 | 0.88431 | 300.0% | 21.0 | None | -0.587 |
| Soccer|player_prop|unknown|chalk | 506 | 64 | 27 | 0 | 415 | 0.7033 | 0.7554 | -250.0 | 0.20724 | 0.60256 | -2.54% | -2.311 | None | 0.0521 |
| Soccer|player_prop|unknown|deep_chalk | 4 | 0 | 2 | 0 | 2 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|player_prop|unknown|deep_dog | 989 | 111 | 258 | 0 | 620 | 0.3008 | 0.5143 | 614.6 | 0.26765 | 0.74134 | 90.64% | 334.48 | None | 0.2135 |
| Soccer|player_prop|unknown|even | 444 | 2 | 5 | 0 | 437 | 0.2857 | 0.5424 | 63.6 | 0.27847 | 0.7505 | -47.71% | -3.34 | None | 0.2567 |
| Soccer|player_prop|unknown|light_dog | 529 | 13 | 79 | 0 | 437 | 0.1413 | 0.4369 | 144.9 | 0.21219 | 0.61628 | -65.37% | -60.14 | None | 0.2956 |
| Soccer|player_prop|unknown|light_fav | 48 | 5 | 25 | 0 | 18 | 0.1667 | 0.6124 | -120.1 | 0.33572 | 0.87211 | -68.97% | -20.692 | None | 0.4458 |
| Soccer|player_prop|unknown|mid_dog | 665 | 36 | 373 | 0 | 256 | 0.088 | 0.4654 | 337.3 | 0.22912 | 0.64849 | -62.32% | -254.9 | None | 0.3774 |
| Soccer|player_prop|unknown|moderate_fav | 168 | 55 | 13 | 0 | 100 | 0.8088 | 0.6915 | -153.6 | 0.16337 | 0.51129 | 33.86% | 23.023 | None | -0.1173 |
| Soccer|totals|unknown|chalk | 163 | 99 | 43 | 0 | 21 | 0.6972 | 0.7218 | -220.2 | 0.20895 | 0.6077 | 1.76% | 2.493 | None | 0.0247 |
| Soccer|totals|unknown|deep_chalk | 116 | 89 | 23 | 0 | 4 | 0.7946 | 0.8099 | -366.3 | 0.16594 | 0.51886 | 1.36% | 1.528 | None | 0.0153 |
| Soccer|totals|unknown|even | 1 | 1 | 0 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|totals|unknown|light_dog | 2 | 0 | 0 | 0 | 2 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|totals|unknown|light_fav | 81 | 36 | 37 | 0 | 8 | 0.4932 | 0.6525 | -123.2 | 0.27606 | 0.74858 | -9.99% | -7.291 | None | 0.1593 |
| Soccer|totals|unknown|moderate_fav | 97 | 48 | 41 | 0 | 8 | 0.5393 | 0.6748 | -159.8 | 0.25476 | 0.70425 | -12.0% | -10.678 | None | 0.1354 |
| Tennis|moneyline|unknown|chalk | 755 | 137 | 87 | 0 | 531 | 0.6116 | 0.6806 | -227.3 | 0.24329 | 0.68051 | -11.52% | -25.799 | None | 0.069 |
| Tennis|moneyline|unknown|deep_chalk | 783 | 177 | 68 | 0 | 538 | 0.7224 | 0.7584 | -425.9 | 0.19984 | 0.59018 | -10.16% | -24.903 | None | 0.036 |
| Tennis|moneyline|unknown|light_dog | 1 | 0 | 0 | 0 | 1 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Tennis|moneyline|unknown|light_fav | 12 | 2 | 7 | 0 | 3 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Tennis|moneyline|unknown|moderate_fav | 538 | 91 | 65 | 0 | 382 | 0.5833 | 0.6523 | -160.8 | 0.24775 | 0.68966 | -5.09% | -7.945 | None | 0.069 |
| Tennis|other|unknown|chalk | 195 | 16 | 1 | 0 | 178 | 0.9412 | 0.6495 | -244.9 | 0.14407 | 0.47423 | 33.1% | 5.627 | None | -0.2917 |
| Tennis|other|unknown|deep_chalk | 161 | 28 | 7 | 0 | 126 | 0.8 | 0.7464 | -453.8 | 0.16315 | 0.50738 | -1.96% | -0.684 | None | -0.0536 |
| Tennis|other|unknown|light_dog | 4 | 3 | 1 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Tennis|other|unknown|light_fav | 18 | 4 | 3 | 0 | 11 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Tennis|other|unknown|moderate_fav | 30 | 0 | 0 | 0 | 30 | - | - | - | - | - | -% | - | - | - |
| Tennis|spread|unknown|light_fav | 2 | 1 | 1 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Tennis|totals|unknown|chalk | 85 | 53 | 28 | 0 | 4 | 0.6543 | 0.7125 | -229.4 | 0.23182 | 0.65838 | -5.3% | -4.294 | None | 0.0582 |
| Tennis|totals|unknown|deep_chalk | 132 | 107 | 15 | 2 | 8 | 0.877 | 0.833 | -477.0 | 0.11043 | 0.38304 | 7.18% | 8.763 | None | -0.0441 |
| Tennis|totals|unknown|light_fav | 107 | 53 | 48 | 0 | 6 | 0.5248 | 0.6481 | -126.9 | 0.26636 | 0.72806 | -5.93% | -5.993 | None | 0.1233 |
| Tennis|totals|unknown|moderate_fav | 107 | 60 | 41 | 2 | 4 | 0.5941 | 0.6501 | -154.7 | 0.24219 | 0.67847 | -2.18% | -2.201 | None | 0.056 |
| UFC|moneyline|unknown|chalk | 9 | 3 | 2 | 0 | 4 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|moneyline|unknown|deep_chalk | 5 | 2 | 1 | 0 | 2 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|moneyline|unknown|light_fav | 1 | 0 | 0 | 0 | 1 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|moneyline|unknown|moderate_fav | 6 | 1 | 3 | 0 | 2 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|other|unknown|chalk | 2 | 0 | 0 | 0 | 2 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|other|unknown|deep_chalk | 2 | 2 | 0 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|other|unknown|light_fav | 5 | 0 | 0 | 0 | 5 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|other|unknown|moderate_fav | 4 | 0 | 0 | 0 | 4 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|totals|unknown|chalk | 3 | 0 | 0 | 0 | 3 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|totals|unknown|moderate_fav | 3 | 0 | 0 | 0 | 3 | — | — | — | — | — | — | — | — | INSUFFICIENT |

## by_main_alt
| Bucket | n | W | L | P | V | HitRate | AvgPred | AvgOdds | Brier | LogLoss | ROI | Units | CLV | CalGap |
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
| MLB|1st|main | 183 | 81 | 81 | 0 | 21 | 0.5 | 0.535 | None | 0.2395 | 0.6686 | -50.0% | -81.0 | None | 0.035 |
| MLB|moneyline|main | 6 | 4 | 2 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| MLB|other|main | 137 | 85 | 52 | 0 | 0 | 0.6204 | 0.7057 | -208.0 | 0.23381 | 0.70958 | -7.07% | -9.681 | None | 0.0853 |
| MLB|player_prop|main | 1948 | 1201 | 667 | 0 | 80 | 0.6429 | 0.7203 | -290.5 | 0.23462 | 0.66632 | -9.41% | -175.686 | None | 0.0774 |
| MLB|spread|main | 180 | 115 | 62 | 0 | 3 | 0.6497 | 0.7093 | -184.6 | 0.23576 | 0.66781 | 0.55% | 0.981 | None | 0.0596 |
| MLB|totals|main | 282 | 205 | 75 | 0 | 2 | 0.7321 | 0.7872 | -254.1 | 0.20163 | 0.6044 | 3.65% | 10.214 | None | 0.0551 |
| NBA|other|main | 47 | 32 | 14 | 0 | 1 | 0.6957 | 0.8073 | -482.1 | 0.22227 | 0.64946 | -12.38% | -5.697 | None | 0.1116 |
| Soccer|double_chance|main | 151 | 108 | 35 | 0 | 8 | 0.7552 | 0.7873 | -277.5 | 0.17709 | 0.53298 | 3.37% | 4.817 | None | 0.0321 |
| Soccer|moneyline|main | 44 | 28 | 16 | 0 | 0 | 0.6364 | 0.7776 | -156.1 | 0.2572 | 0.72286 | 5.08% | 2.235 | None | 0.1413 |
| Soccer|other|main | 2212 | 251 | 565 | 0 | 1396 | 0.3076 | 0.4516 | -14.5 | 0.22842 | 0.69518 | -37.5% | -305.982 | None | 0.144 |
| Soccer|player_prop|main | 3472 | 295 | 795 | 0 | 2382 | 0.2706 | 0.5206 | 314.1 | 0.23853 | 0.6726 | 2.35% | 25.589 | None | 0.25 |
| Soccer|totals|main | 460 | 273 | 144 | 0 | 43 | 0.6547 | 0.723 | -228.8 | 0.21883 | 0.62893 | -3.11% | -12.948 | None | 0.0683 |
| Tennis|moneyline|main | 2089 | 407 | 227 | 0 | 1455 | 0.642 | 0.7022 | -286.2 | 0.22865 | 0.65001 | -10.09% | -63.966 | None | 0.0603 |
| Tennis|other|main | 408 | 51 | 12 | 0 | 345 | 0.8095 | 0.6629 | -322.4 | 0.18351 | 0.55403 | 13.17% | 8.299 | None | -0.1466 |
| Tennis|spread|main | 2 | 1 | 1 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Tennis|totals|main | 431 | 273 | 132 | 4 | 22 | 0.6741 | 0.7172 | -259.8 | 0.20645 | 0.59783 | -0.92% | -3.725 | None | 0.0431 |
| UFC|moneyline|main | 21 | 6 | 6 | 0 | 9 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|other|main | 13 | 2 | 0 | 0 | 11 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|totals|main | 6 | 0 | 0 | 0 | 6 | — | — | — | — | — | — | — | — | INSUFFICIENT |

## by_lock_band
| Bucket | n | W | L | P | V | HitRate | AvgPred | AvgOdds | Brier | LogLoss | ROI | Units | CLV | CalGap |
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
| MLB|below_65 | 1144 | 706 | 380 | 0 | 58 | 0.6501 | 0.7246 | -349.5 | 0.22544 | 0.64586 | -14.54% | -157.935 | None | 0.0745 |
| MLB|elite_95+ | 258 | 152 | 103 | 0 | 3 | 0.5961 | 0.7618 | -249.6 | 0.27686 | 0.80681 | -10.73% | -27.373 | None | 0.1657 |
| MLB|playable_80-87 | 504 | 286 | 195 | 0 | 23 | 0.5946 | 0.6852 | -189.8 | 0.24655 | 0.68845 | -8.55% | -41.124 | None | 0.0906 |
| MLB|strong_88-94 | 730 | 500 | 220 | 0 | 10 | 0.6944 | 0.7174 | -238.6 | 0.21178 | 0.61409 | 1.19% | 8.594 | None | 0.023 |
| MLB|warmup_65-73 | 96 | 47 | 37 | 0 | 12 | 0.5595 | 0.591 | -227.3 | 0.24791 | 0.68653 | -39.76% | -33.402 | None | 0.0315 |
| MLB|watchlist_74-79 | 4 | 0 | 4 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| NBA|elite_95+ | 3 | 2 | 1 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| NBA|playable_80-87 | 5 | 3 | 2 | 0 | 0 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| NBA|strong_88-94 | 38 | 27 | 11 | 0 | 0 | 0.7105 | 0.8201 | -493.9 | 0.22107 | 0.64738 | -11.78% | -4.476 | None | 0.1095 |
| NBA|warmup_65-73 | 1 | 0 | 0 | 0 | 1 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| Soccer|below_65 | 2290 | 114 | 261 | 0 | 1915 | 0.304 | 0.4648 | -51.7 | 0.18646 | 0.56002 | -50.64% | -189.914 | None | 0.1608 |
| Soccer|elite_95+ | 506 | 116 | 170 | 0 | 220 | 0.4056 | 0.6493 | 54.7 | 0.31132 | 0.9269 | -28.06% | -80.252 | None | 0.2437 |
| Soccer|playable_80-87 | 993 | 256 | 202 | 0 | 535 | 0.559 | 0.6305 | 67.3 | 0.20419 | 0.60437 | 39.31% | 180.05 | None | 0.0716 |
| Soccer|strong_88-94 | 905 | 233 | 243 | 0 | 429 | 0.4895 | 0.6166 | -95.3 | 0.22005 | 0.64878 | -17.3% | -82.334 | None | 0.1271 |
| Soccer|warmup_65-73 | 1018 | 93 | 355 | 0 | 570 | 0.2076 | 0.4584 | 394.7 | 0.22307 | 0.63742 | -0.84% | -3.785 | None | 0.2508 |
| Soccer|watchlist_74-79 | 627 | 143 | 324 | 0 | 160 | 0.3062 | 0.5067 | 64.4 | 0.2508 | 0.69464 | -23.57% | -110.052 | None | 0.2005 |
| Tennis|below_65 | 102 | 44 | 23 | 0 | 35 | 0.6567 | 0.6346 | -246.3 | 0.23284 | 0.65787 | -1.43% | -0.961 | None | -0.0221 |
| Tennis|elite_95+ | 349 | 138 | 28 | 2 | 181 | 0.8313 | 0.8121 | -437.7 | 0.13497 | 0.44079 | 4.68% | 7.761 | None | -0.0192 |
| Tennis|playable_80-87 | 893 | 199 | 109 | 0 | 585 | 0.6461 | 0.6919 | -263.7 | 0.22726 | 0.64609 | -7.63% | -23.491 | None | 0.0458 |
| Tennis|strong_88-94 | 925 | 254 | 164 | 2 | 505 | 0.6077 | 0.6685 | -188.6 | 0.2402 | 0.67437 | -4.81% | -20.109 | None | 0.0609 |
| Tennis|warmup_65-73 | 661 | 97 | 48 | 0 | 516 | 0.669 | 0.7509 | -400.1 | 0.22291 | 0.63849 | -15.7% | -22.759 | None | 0.0819 |
| UFC|below_65 | 23 | 5 | 6 | 0 | 12 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|playable_80-87 | 4 | 3 | 0 | 0 | 1 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|strong_88-94 | 5 | 0 | 0 | 0 | 5 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|warmup_65-73 | 3 | 0 | 0 | 0 | 3 | — | — | — | — | — | — | — | — | INSUFFICIENT |
| UFC|watchlist_74-79 | 5 | 0 | 0 | 0 | 5 | — | — | — | — | — | — | — | — | INSUFFICIENT |

## by_magic_band
| Bucket | n | W | L | P | V | HitRate | AvgPred | AvgOdds | Brier | LogLoss | ROI | Units | CLV | CalGap |
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
| MLB|no_tier | 2736 | 1691 | 939 | 0 | 106 | 0.643 | 0.7145 | -274.1 | 0.23144 | 0.66222 | -9.7% | -255.241 | None | 0.0716 |
| NBA|no_tier | 47 | 32 | 14 | 0 | 1 | 0.6957 | 0.8073 | -482.1 | 0.22227 | 0.64946 | -12.38% | -5.697 | None | 0.1116 |
| Soccer|no_tier | 6339 | 955 | 1555 | 0 | 3829 | 0.3805 | 0.5515 | 75.1 | 0.2288 | 0.66561 | -11.41% | -286.288 | None | 0.171 |
| Tennis|no_tier | 2930 | 732 | 372 | 4 | 1822 | 0.663 | 0.7054 | -278.3 | 0.21805 | 0.62564 | -5.39% | -59.559 | None | 0.0424 |
| UFC|no_tier | 40 | 8 | 6 | 0 | 26 | 0.5714 | 0.6444 | -267.7 | 0.22545 | 0.64551 | -22.44% | -3.142 | None | 0.0729 |

## by_sim_used
| Bucket | n | W | L | P | V | HitRate | AvgPred | AvgOdds | Brier | LogLoss | ROI | Units | CLV | CalGap |
|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|
| MLB|no_sim | 603 | 357 | 220 | 0 | 26 | 0.6187 | 0.6739 | -221.6 | 0.22905 | 0.66295 | -15.04% | -86.798 | None | 0.0552 |
| MLB|sim_used | 2133 | 1334 | 719 | 0 | 80 | 0.6498 | 0.7259 | -284.7 | 0.23211 | 0.66201 | -8.2% | -168.442 | None | 0.0762 |
| NBA|no_sim | 47 | 32 | 14 | 0 | 1 | 0.6957 | 0.8073 | -482.1 | 0.22227 | 0.64946 | -12.38% | -5.697 | None | 0.1116 |
| Soccer|no_sim | 2103 | 242 | 534 | 0 | 1327 | 0.3119 | 0.4546 | -20.9 | 0.22666 | 0.69221 | -37.86% | -293.757 | None | 0.1428 |
| Soccer|sim_used | 4236 | 713 | 1021 | 0 | 2502 | 0.4112 | 0.5949 | 118.1 | 0.22975 | 0.65371 | 0.43% | 7.469 | None | 0.1837 |
| Tennis|no_sim | 192 | 60 | 47 | 0 | 85 | 0.5607 | 0.6777 | -291.9 | 0.28084 | 0.76797 | -20.47% | -21.904 | None | 0.1169 |
| Tennis|sim_used | 2738 | 672 | 325 | 4 | 1737 | 0.674 | 0.7084 | -276.8 | 0.21131 | 0.61037 | -3.78% | -37.655 | None | 0.0344 |
| UFC|no_sim | 39 | 8 | 6 | 0 | 25 | 0.5714 | 0.6444 | -267.7 | 0.22545 | 0.64551 | -22.44% | -3.142 | None | 0.0729 |
| UFC|sim_used | 1 | 0 | 0 | 0 | 1 | — | — | — | — | — | — | — | — | INSUFFICIENT |

---
**Zero production writes performed.**  This report was generated by `scripts/phase4b_calibration_baseline.py` in read-only mode.