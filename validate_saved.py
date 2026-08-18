"""
validate_saved.py — regression-test the deterministic engine against saved runs.

Re-parses every `results_*.txt` file in this folder (the raw scraped data captured
by earlier runs), runs the new structured extractor + classifier, and prints the
current/former residents it derives — so we can measure accuracy on real data
*before* changing any scrapers.

Usage:
    python validate_saved.py
"""

from __future__ import annotations

import glob
import os
import re

from resident_core import Address, Classification, classify, normalize_address
from parsers import parse_source
from scoring import score_candidates

RAW_HEADER = "=== RAW SCRAPED DATA ==="
ANALYSIS_HEADER = "=== ANALYSIS ==="
SOURCE_RE = re.compile(r"^=== (.+?) ===\s*$")


def parse_target(text: str) -> Address:
    m = re.search(r"^Address:\s*(.+)$", text, re.MULTILINE)
    return normalize_address(m.group(1).strip()) if m else Address()


def split_sources(text: str) -> dict[str, str]:
    """Return {source_name: raw_text} for the RAW SCRAPED DATA region."""
    start = text.find(RAW_HEADER)
    if start == -1:
        return {}
    body = text[start + len(RAW_HEADER):]
    end = body.find(ANALYSIS_HEADER)
    if end != -1:
        body = body[:end]

    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in body.splitlines():
        m = SOURCE_RE.match(line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def fmt_addr(a: Address | None) -> str:
    if not a:
        return "(none)"
    bits = [a.house_number, a.street]
    if a.unit:
        bits.append(f"#{a.unit}")
    s = " ".join(b for b in bits if b)
    return s or a.raw


def run_file(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        text = f.read()

    target = parse_target(text)
    sources = split_sources(text)

    print("=" * 78)
    print(f"FILE   : {os.path.basename(path)}")
    print(f"TARGET : {fmt_addr(target)}   (raw: {target.raw})")
    print(f"SOURCES: {', '.join(sources) or '(none)'}")

    all_people = []
    for name, raw in sources.items():
        all_people.extend(parse_source(name, raw))

    if not all_people:
        print("  -> no structured people parsed (source(s) returned no usable data)")
        return

    verdicts = [classify(p, target) for p in all_people]
    current = [v for v in verdicts if v.classification == Classification.CURRENT]
    former = [v for v in verdicts if v.classification == Classification.FORMER]
    other = [v for v in verdicts if v.classification == Classification.OTHER]

    print(f"\n  CURRENT RESIDENT(S):")
    if current:
        for v in current:
            age = f"age {v.person.age}" if v.person.age else "age ?"
            print(f"    * {v.person.name} ({age}) [{v.current_match.name}] - {v.reason}")
    else:
        print("    (none found)")

    if former:
        print(f"\n  Former (target is a prior address):")
        for v in former:
            print(f"    - {v.person.name} -> now at {fmt_addr(v.person.current_address)}")

    print(f"\n  Parsed {len(all_people)} people total "
          f"({len(current)} current, {len(former)} former, {len(other)} other/noise).")

    # Full scoring pass (not just classify) — this is where corroboration/supersession/recency
    # live, so the eyeball tool exercises the same ranking the real pipeline produces.
    scored_current, possible, scored_former, _ = score_candidates(all_people, target)
    print("  SCORED most-likely current:")
    if scored_current:
        top = scored_current[0]
        since = f", since {top.since}" if top.since else ""
        print(f"    -> {top.name} [{top.confidence}{since}; score {top.score}]")
        for c in scored_current[1:]:
            print(f"       . {c.name} [{c.confidence}; score {c.score}]")
    elif possible:
        print(f"    -> (no confirmed current) leads: {', '.join(c.name for c in possible)}")
    else:
        print("    -> (none)")


def main() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    files = sorted(glob.glob(os.path.join(here, "results_*.txt")))
    if not files:
        print("No results_*.txt files found.")
        return
    for path in files:
        run_file(path)
    print("=" * 78)
    print(f"Done. Validated {len(files)} saved file(s).")


if __name__ == "__main__":
    main()
