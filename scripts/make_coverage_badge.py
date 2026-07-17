"""Generate a shields-style SVG coverage badge from the .coverage data file.

Reads the total percentage via `coverage report --format=total` (so run the
tests with `--cov` first) and writes the badge, by default to
badges/coverage.svg. CI runs this and commits the badge so the readme can
reference it without any external service (the repo is private).
"""

import subprocess
import sys
from pathlib import Path

_COLORS = [  # same thresholds the coverage-badge package used
    (100, "#4c1"),
    (90, "#97CA00"),
    (75, "#a4a61d"),
    (60, "#dfb317"),
    (40, "#fe7d37"),
    (0, "#e05d44"),
]

_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="104" height="20" role="img" aria-label="coverage: {pct}%">
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="104" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="61" height="20" fill="#555"/>
    <rect x="61" width="43" height="20" fill="{color}"/>
    <rect width="104" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
    <text x="31.5" y="14">coverage</text>
    <text x="82.5" y="14">{pct}%</text>
  </g>
</svg>
"""


def main() -> None:
    total = subprocess.run(
        [sys.executable, "-m", "coverage", "report", "--format=total"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    pct = round(float(total))
    color = next(c for threshold, c in _COLORS if pct >= threshold)
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "badges/coverage.svg")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_SVG.format(pct=pct, color=color), encoding="utf-8")
    print(f"{out}: {pct}%")


if __name__ == "__main__":
    main()
