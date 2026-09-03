"""SLICE 3 — Image + GPU Performance
======================================

Migrated the two hot-path image components on the board list
(`PickEventRow` — team crests, `PlayerIdentity` — player headshots) from
`react-native`'s built-in `<Image>` to `expo-image`. Every LockBoardCard
mounts these; on a 100-card slate that's ~300 image mounts. `expo-image`
provides:

  * Cross-mount decoded image cache — team logos painted 40× (list
    virtualization recycling) decode exactly ONCE.
  * Native placeholder + smooth 120-140 ms cross-fade transition.
  * `cachePolicy="memory-disk"` persistence across boots.

Slice 3 invariants:
    1. PickEventRow uses `expo-image` (not `react-native`'s Image)
    2. PlayerIdentity uses `expo-image` (not `react-native`'s Image)
    3. Both call sites use `cachePolicy="memory-disk"` so recycled cells
       don't re-decode
    4. Both use `contentFit` (expo-image API) not `resizeMode`
       (a common regression trap when someone reverts)
"""
from __future__ import annotations
import os, re, pytest


def _read(rel: str) -> str:
    p = os.path.join("/app/frontend", rel)
    if not os.path.exists(p):
        pytest.skip(f"{rel} missing")
    with open(p, "r") as f:
        return f.read()


def test_slice_3_pick_event_row_uses_expo_image():
    src = _read("src/components/PickEventRow.tsx")
    assert 'from "expo-image"' in src, (
        "PickEventRow must import Image from expo-image (Slice 3)."
    )
    # And Image must NOT still be imported from react-native.
    rn_import = re.search(r"from\s+['\"]react-native['\"]", src)
    if rn_import:
        # Read the whole import block containing Image from react-native.
        rn_blocks = re.findall(r"import\s*\{([^}]+)\}\s*from\s+['\"]react-native['\"]", src)
        for block in rn_blocks:
            names = [n.strip() for n in block.split(",")]
            assert "Image" not in names, (
                "PickEventRow still imports `Image` from react-native — "
                "Slice 3 requires expo-image."
            )


def test_slice_3_player_identity_uses_expo_image():
    src = _read("src/components/PlayerIdentity.tsx")
    assert 'from "expo-image"' in src, (
        "PlayerIdentity must import Image from expo-image (Slice 3)."
    )
    rn_blocks = re.findall(r"import\s*\{([^}]+)\}\s*from\s+['\"]react-native['\"]", src)
    for block in rn_blocks:
        names = [n.strip() for n in block.split(",")]
        assert "Image" not in names, (
            "PlayerIdentity still imports `Image` from react-native — "
            "Slice 3 requires expo-image."
        )


def test_slice_3_uses_memory_disk_cache_policy():
    for rel in ("src/components/PickEventRow.tsx",
                  "src/components/PlayerIdentity.tsx"):
        src = _read(rel)
        assert 'cachePolicy="memory-disk"' in src, (
            f"{rel}: Image call must use cachePolicy=\"memory-disk\" "
            f"so scroll-recycled cells don't re-decode."
        )


def test_slice_3_uses_content_fit_not_resize_mode():
    for rel in ("src/components/PickEventRow.tsx",
                  "src/components/PlayerIdentity.tsx"):
        src = _read(rel)
        # `resizeMode` is the react-native <Image> prop; expo-image uses
        # `contentFit`. If someone reverts back to <Image>, they'll
        # commonly re-add `resizeMode` and this canary will fire.
        assert "resizeMode=" not in src, (
            f"{rel}: `resizeMode=` present — likely reverted to "
            f"react-native <Image>. Slice 3 requires `contentFit` "
            f"(expo-image API)."
        )
        assert "contentFit=" in src, (
            f"{rel}: expo-image `contentFit=` prop missing."
        )


def test_slice_3_expo_image_available():
    # Package.json must declare expo-image (already installed today).
    with open("/app/frontend/package.json", "r") as f:
        pj = f.read()
    assert '"expo-image"' in pj, (
        "expo-image must be declared in package.json dependencies."
    )
