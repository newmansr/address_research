"""
resident_core.py — deterministic extraction, address matching & classification.

This is the accuracy core of the address-research tool. It deliberately does NOT
rely on an LLM to decide who currently lives at an address. Instead it:

  1. parses structured people-search output into `Person` records,
  2. normalizes addresses to comparable components,
  3. matches a candidate's CURRENT address against the target at UNIT level,
  4. classifies each person as current / former / other.

Scoring & cross-source merging live in Phase 2 but the data model here is built
to support them (each Person carries its source + addresses).

No third-party dependencies — pure stdlib so the regression validator runs anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── Address normalization ────────────────────────────────────────────────────

_DIRECTIONALS = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}

_SUFFIXES = {
    "AVENUE": "AVE", "AV": "AVE",
    "STREET": "ST", "STR": "ST",
    "ROAD": "RD",
    "DRIVE": "DR",
    "LANE": "LN",
    "COURT": "CT",
    "BOULEVARD": "BLVD", "BLVD.": "BLVD",
    "PLACE": "PL",
    "TERRACE": "TER", "TERR": "TER",
    "CIRCLE": "CIR",
    "PARKWAY": "PKWY",
    "HIGHWAY": "HWY",
    "SQUARE": "SQ",
    "TRAIL": "TRL",
    "PIKE": "PIKE",
    "WAY": "WAY",
}

# Tokens that introduce a secondary unit designator.
_UNIT_DESIGNATORS = {"APT", "UNIT", "STE", "SUITE", "BLDG", "FL", "FLOOR", "NO", "RM", "ROOM"}

# 2-letter USPS state codes — used to strip a state that got glued onto the street when a
# source omits commas (e.g. "... SE Washington DC"). Directionals never collide with these.
_US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
}

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_STATE_RE = re.compile(r"\b([A-Z]{2})\b")


def _clean_token(tok: str) -> str:
    return re.sub(r"[.,]", "", tok).strip().upper()


def _norm_unit_value(val: str) -> str:
    """Compare units case-insensitively, ignoring leading zeros (637 == 0637)."""
    v = val.strip().upper().lstrip("#").strip()
    v = re.sub(r"^0+(?=\d)", "", v)  # strip leading zeros but keep a bare "0"
    return v


@dataclass
class Address:
    raw: str = ""
    house_number: str = ""
    street: str = ""          # normalized street core, e.g. "NEW JERSEY AVE SE"
    unit: str = ""            # normalized unit value, e.g. "637" or "5B"
    city: str = ""
    state: str = ""
    zip: str = ""

    def has_unit(self) -> bool:
        return bool(self.unit)

    def display(self) -> str:
        """Lightly-cleaned one-line address for display: strip '[County]' tags and collapse
        whitespace on `raw`. (Kept as raw-cleanup rather than component-reassembly because the
        normalizer is tuned to the target building, not to arbitrary move-destination formats.)"""
        s = re.sub(r"\s*\[[^\]]*\]", "", self.raw or "")
        s = re.sub(r"\s+", " ", s).strip().strip(",").strip()
        return s or self.raw


def normalize_address(s: str) -> Address:
    """Parse a free-text address into comparable components.

    Handles the comma-delimited people-search style, e.g.:
      "800 New Jersey Ave SE, APT 637, Washington, DC 20003-3993"
    and space-delimited input, e.g.:
      "800 New Jersey Ave SE Unit 637".
    """
    addr = Address(raw=s.strip())
    if not s.strip():
        return addr

    work = s.strip()

    # ZIP (last 5-digit group).
    zips = _ZIP_RE.findall(work)
    if zips:
        addr.zip = zips[-1]
        work = _ZIP_RE.sub(" ", work, count=0)

    # Pull out the unit designator + value anywhere in the string.
    unit_pat = re.compile(
        r"\b(" + "|".join(_UNIT_DESIGNATORS) + r")\b\.?\s*#?\s*([A-Za-z0-9][A-Za-z0-9\-]*)",
        re.IGNORECASE,
    )
    m = unit_pat.search(work)
    if m:
        addr.unit = _norm_unit_value(m.group(2))
        work = work[: m.start()] + " " + work[m.end():]
    else:
        # bare "#637"
        m2 = re.search(r"#\s*([A-Za-z0-9][A-Za-z0-9\-]*)", work)
        if m2:
            addr.unit = _norm_unit_value(m2.group(1))
            work = work[: m2.start()] + " " + work[m2.end():]

    # Split remaining on commas: [street, (city), (state ...)]
    parts = [p.strip() for p in work.split(",") if p.strip()]

    # State: a lone 2-letter uppercase token among the parts (prefer a trailing part).
    for p in reversed(parts):
        st = _STATE_RE.search(p.upper())
        if st and len(p.strip()) <= 3:
            addr.state = st.group(1)
            parts.remove(p)
            break

    if parts:
        street_part = parts[0]
        # City is whatever sits between street and state (best-effort).
        if len(parts) >= 2:
            addr.city = parts[-1].title()
        addr.house_number, addr.street = _normalize_street(street_part)

    return addr


def _normalize_street(street_part: str) -> tuple[str, str]:
    toks = street_part.split()
    if not toks:
        return "", ""

    house = ""
    if re.match(r"^\d+[A-Za-z]?$", toks[0]):
        house = toks[0].upper()
        toks = toks[1:]

    norm: list[str] = []
    for t in toks:
        ct = _clean_token(t)
        if not ct:
            continue
        ct = _DIRECTIONALS.get(ct, ct)
        ct = _SUFFIXES.get(ct, ct)
        norm.append(ct)
    # Drop a trailing state code that leaked in when the source omitted the city/state comma
    # (e.g. "NEW JERSEY AVE SE WASHINGTON DC" -> drop "DC"). Keep ≥2 tokens so we never gut a
    # short street. A remaining glued city is tolerated by `_street_matches`.
    if len(norm) > 2 and norm[-1] in _US_STATE_CODES:
        norm.pop()
    return house, " ".join(norm)


# ── Address matching ─────────────────────────────────────────────────────────

class MatchLevel(Enum):
    NONE = 0            # different street/house, or different unit in same building
    BUILDING = 1        # same house+street, but candidate has no unit (ambiguous)
    EXACT = 2           # same house+street+unit (or target has no unit)


# Tokens that, if they are the *extra* part of a street prefix-match, mean a DIFFERENT street
# (a directional or a street-type suffix) rather than a glued-on city.
_STREET_PARTS = set(_SUFFIXES.values()) | set(_DIRECTIONALS.values())


def _street_matches(a: str, b: str) -> bool:
    """Equal, or one is a prefix of the other where the EXTRA tokens look like a glued-on city
    (not a directional/suffix). So 'NEW JERSEY AVE SE' matches '... SE WASHINGTON' (city leaked)
    but NOT 'MAIN ST' vs 'MAIN ST N' (a genuinely different street)."""
    if a == b:
        return True
    for longer, shorter in ((a, b), (b, a)):
        if longer.startswith(shorter + " "):
            extra = longer[len(shorter) + 1:].split()
            if extra and not any(t in _STREET_PARTS for t in extra):
                return True
    return False


def match_address(target: Address, cand: Address) -> MatchLevel:
    """How well does `cand` match the `target` residence?"""
    if not cand.house_number or not cand.street:
        return MatchLevel.NONE
    if target.house_number != cand.house_number:
        return MatchLevel.NONE
    if not _street_matches(target.street, cand.street):
        return MatchLevel.NONE
    # Locality guard: a same-numbered street in another city/state is NOT the same building.
    # Only enforced when both sides carry the field (missing = wildcard, so we never lose a match
    # to a source that omitted the ZIP). ZIP is the strong discriminator; state is a cheap backstop.
    if target.zip and cand.zip and target.zip != cand.zip:
        return MatchLevel.NONE
    if target.state and cand.state and target.state != cand.state:
        return MatchLevel.NONE

    # Same building from here on.
    if not target.has_unit():
        return MatchLevel.EXACT
    if not cand.has_unit():
        return MatchLevel.BUILDING
    return MatchLevel.EXACT if target.unit == cand.unit else MatchLevel.NONE


# ── Person model ─────────────────────────────────────────────────────────────

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def since_to_year(s: str) -> Optional[float]:
    """Parse a 'resident since' date into a comparable float year (year + (month-1)/12).

    Tolerates the formats people-search sites emit:
      'Jul 2018', 'July 2018', 'Since June 2015', '07/2018', '6/15/2018',
      '2018-07', or a bare '2015'. Returns None if no year is found.
    """
    if not s:
        return None
    s = s.strip()
    # Month-name YYYY  ('Jul 2018', 'September 2016')
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{4})", s)
    if m and m.group(1)[:3].lower() in _MONTHS:
        return int(m.group(2)) + (_MONTHS[m.group(1)[:3].lower()] - 1) / 12
    # MM/YYYY or M/D/YYYY  ('07/2018', '6/15/2018')
    m = re.search(r"\b(\d{1,2})/(?:\d{1,2}/)?(\d{4})\b", s)
    if m:
        mo = int(m.group(1))
        return int(m.group(2)) + (mo - 1) / 12 if 1 <= mo <= 12 else float(int(m.group(2)))
    # YYYY-MM  ('2018-07')
    m = re.search(r"\b(\d{4})-(\d{1,2})\b", s)
    if m:
        mo = int(m.group(2))
        return int(m.group(1)) + (mo - 1) / 12 if 1 <= mo <= 12 else float(int(m.group(1)))
    # Bare year
    m = re.search(r"\b(19|20)\d{2}\b", s)
    if m:
        return float(int(m.group(0)))
    return None


_NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}


def name_key(name: str) -> str:
    """First+last for cross-source merging: drop middle names/initials AND a trailing
    generational suffix, so 'Robert Kinsler Jr' and 'Robert A Kinsler' both key 'ROBERT KINSLER'."""
    toks = [t for t in re.split(r"\s+", name.strip()) if t]
    while len(toks) > 2 and re.sub(r"[.,]", "", toks[-1]).upper() in _NAME_SUFFIXES:
        toks.pop()
    if len(toks) >= 2:
        return (toks[0] + " " + toks[-1]).upper()
    return name.strip().upper()


@dataclass
class Person:
    name: str
    age: Optional[int] = None
    current_address: Optional[Address] = None
    current_since: str = ""   # raw "resident since" date for current_address, if the source gives one
    current_until: str = ""   # raw END of the current-address date range (last-reported), if given
    prior_addresses: list[Address] = field(default_factory=list)
    relatives: list[str] = field(default_factory=list)
    aka: list[str] = field(default_factory=list)   # "also known as" — alternate full names
    phones: list[str] = field(default_factory=list)
    source: str = ""
    searched_unit: str = ""   # the unit whose source search returned this person (if any)
    note: str = ""            # free-text identifier (e.g. FEC occupation/employer, owner-of-record)

    def since_year(self) -> Optional[float]:
        """Comparable move-in year for the current address (None if undated)."""
        return since_to_year(self.current_since)

    def until_year(self) -> Optional[float]:
        """Comparable last-reported year for the current address (None if not given)."""
        return since_to_year(self.current_until)

    def name_key(self) -> str:
        return name_key(self.name)

    def aka_keys(self) -> set:
        """This person's name_key plus one per AKA — for reconciling maiden/alias variants."""
        return {self.name_key()} | {name_key(a) for a in self.aka if a}


class Classification(Enum):
    CURRENT = "current"        # current address matches the target unit exactly
    BUILDING = "building"      # same building, unit unconfirmed (weak — multi-unit targets)
    FORMER = "former"          # target appears only as a prior address
    OTHER = "other"            # no address match (relative/neighbor/noise)


@dataclass
class Verdict:
    person: Person
    classification: Classification
    current_match: MatchLevel
    best_prior_match: MatchLevel
    reason: str = ""


def classify(person: Person, target: Address) -> Verdict:
    cur = match_address(target, person.current_address) if person.current_address else MatchLevel.NONE

    best_prior = MatchLevel.NONE
    for pa in person.prior_addresses:
        lvl = match_address(target, pa)
        if lvl.value > best_prior.value:
            best_prior = lvl

    if cur == MatchLevel.EXACT:
        return Verdict(person, Classification.CURRENT, cur, best_prior,
                       "current address matches target unit")
    if cur == MatchLevel.BUILDING:
        # Same building but the unit is unknown — cannot assert this unit.
        return Verdict(person, Classification.BUILDING, cur, best_prior,
                       "same building, unit not listed (unconfirmed)")
    if best_prior == MatchLevel.EXACT:
        return Verdict(person, Classification.FORMER, cur, best_prior,
                       "target unit appears only as a prior address")

    return Verdict(person, Classification.OTHER, cur, best_prior,
                   "no unit-level address match (likely relative/neighbor/loose association)")
