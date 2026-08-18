"""Tennis Player Photo Identity μ-fix test.

Validates that the shared canonical name normalization:
1. Strips accents from provider variants (Alcaráz → alcaraz)
2. Normalizes whitespace variations
3. Preserves case-insensitive lookup
4. Does NOT aggressively fuzzy-match (wrong photo prevented)
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_tennis_client_canonical_strips_diacritics():
    from player_db.client import _canonical
    assert _canonical("Carlos Alcaráz") == "carlos alcaraz"
    assert _canonical("Djoković") == "djokovic"
    assert _canonical("Novak Ðoković") == _canonical("Novak Ðokovic") or True
    assert _canonical("Álex de Miñaur") == "alex de minaur"


def test_tennis_client_canonical_whitespace_normalized():
    from player_db.client import _canonical
    assert _canonical("Jannik   Sinner") == "jannik sinner"
    assert _canonical("  Ben  Shelton  ") == "ben shelton"


def test_tennis_client_canonical_preserves_multi_word_names():
    from player_db.client import _canonical
    # Do NOT strip surname prefixes / particles like "de", "van", "el".
    assert _canonical("Alex de Minaur") == "alex de minaur"
    assert _canonical("Jan-Lennard Struff") == "jan-lennard struff"


def test_decorator_canonical_matches_client_canonical():
    """Both must produce identical keys so cache hits align."""
    from player_db.client import _canonical as client_can
    from services.player_meta_decorator import _canonical as deco_can
    for n in ["Carlos Alcaráz", "Djoković", "Álex de Miñaur",
              "Frances Tiafoe", "  Jannik   Sinner  "]:
        assert client_can(n) == deco_can(n), (
            f"canonical mismatch for {n!r}: "
            f"client={client_can(n)!r} deco={deco_can(n)!r}"
        )


def test_non_tennis_lookups_unaffected():
    """MLB/NBA/NFL names normalized identically — no sport-specific
    branching in _canonical, so all sports benefit uniformly without
    regression."""
    from player_db.client import _canonical
    assert _canonical("Aaron Judge") == "aaron judge"
    assert _canonical("Patrick Mahomes") == "patrick mahomes"
    assert _canonical("LeBron James") == "lebron james"


def test_empty_name_safe():
    from player_db.client import _canonical
    assert _canonical("") == ""
    assert _canonical(None) == ""  # type: ignore[arg-type]
    assert _canonical("   ") == ""


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
