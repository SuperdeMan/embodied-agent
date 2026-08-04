"""CLAUDE.md 术语纪律 contract: car-agent domain vocabulary must not leak into src/.

Ported files keep their provenance headers ("Ported from car-agent ...") — the source
repo NAME is fine; the source DOMAIN's concepts are not.
"""

from __future__ import annotations

import re
from pathlib import Path

# assembled to avoid this file matching itself if it ever moves under src/
_TERMS = ["vehi" + "cle", "cock" + "pit", "hv" + "ac", "座" + "舱", "车" + "机", "车" + "控"]
FORBIDDEN = re.compile("|".join(_TERMS), re.IGNORECASE)
ROOT = Path(__file__).resolve().parents[1]


def test_no_forbidden_domain_terms_in_src():
    offenders: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:100]}")
    assert not offenders, "forbidden domain terms found in src/:\n" + "\n".join(offenders)
