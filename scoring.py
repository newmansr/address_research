"""
scoring.py - Phase 2: cross-source merge, scoring & recency ranking.

Turns the per-source `Person` records into a single ranked answer for a unit:
the most likely CURRENT resident, with a confidence level and the evidence behind it.

Signal model (why a weight is what it is):
  - USPhoneBook explicitly states a person's CURRENT address ("Lives at ...") and lists
    the unit as a PRIOR address when they've moved out. So it carries the strongest
    current signal AND the only real *former* (moved-out) signal.
  - ThatsThem associates a person with a unit but is UNDATED - it can't tell current from
    former, and lists everyone ever linked to the unit. Medium-weight, corroborating.
  - Agreement across independent sources raises confidence (corroboration bonus).
  - Recency / move-chain: a USPhoneBook "former" verdict actively pushes a candidate down,
    and a confirmed USPhoneBook current resident supersedes ThatsThem-only names (likely
    prior tenants) for the same unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional
import difflib

from resident_core import (Address, Classification, MatchLevel, Person, classify,
                           match_address)

# Per-(source, classification) score contributions.
# Authoritative sources state a person's exact CURRENT address ("Lives at <unit>");
# association sources (ThatsThem, TPS) only link a person to the searched address/building.
SIGNAL_WEIGHTS = {
    ("USPhoneBook", Classification.CURRENT): 100,
    ("USPhoneBook", Classification.FORMER): -70,
    ("USPhoneBook", Classification.BUILDING): 10,
    ("CyberBackgroundChecks", Classification.CURRENT): 85,   # authoritative (real "Lives at")
    ("CyberBackgroundChecks", Classification.FORMER): -55,
    ("TruePeopleSearch", Classification.CURRENT): 90,   # authoritative (detail-page address)
    ("TruePeopleSearch", Classification.FORMER): -50,
    ("FastPeopleSearch", Classification.CURRENT): 110,  # highest - user rates FPS most accurate
    ("FastPeopleSearch", Classification.FORMER): -65,
    ("SearchPeopleFree", Classification.CURRENT): 88,   # FPS-family sister (detail-page address)
    ("SearchPeopleFree", Classification.FORMER): -55,
    ("FastBackgroundCheck", Classification.CURRENT): 88,
    ("FastBackgroundCheck", Classification.FORMER): -55,
    ("ThatsThem", Classification.CURRENT): 45,
    ("ThatsThem", Classification.BUILDING): 5,
    # Independent open-data sources (apis.py) - genuinely different lineages.
    ("DCProperty", Classification.CURRENT): 85,   # owner-occupant of record (authoritative, gov)
    ("DCProperty", Classification.BUILDING): 15,
    ("OpenFEC", Classification.CURRENT): 60,      # self-reported donor address (dated, independent)
    ("OpenFEC", Classification.BUILDING): 10,
    ("SECEdgar", Classification.CURRENT): 35,     # low-yield tie-breaker
    ("SECEdgar", Classification.BUILDING): 5,
}

# Sources that authoritatively confirm a unit-level current address.
CONFIRMING_SOURCES = {"USPhoneBook", "CyberBackgroundChecks", "TruePeopleSearch", "FastPeopleSearch",
                      "SearchPeopleFree", "FastBackgroundCheck", "DCProperty", "OpenFEC"}
# Association-only sources - a confirmed current resident supersedes these for the same unit.
ASSOCIATION_SOURCES = {"ThatsThem"}
# Context-only sources: surfaced as background (e.g. absentee/entity owner of record), never ranked
# as a resident. Filtered out before scoring; displayed separately by report_unit.
CONTEXT_SOURCES = {"DCPropertyOwner", "DCBusinessLicense", "DC311", "DCCrime", "OSMContext"}
DEFAULT_CURRENT_WEIGHT = 40   # unknown source that asserts current
CORROBORATION_BONUS = 30      # ≥2 independent sources agree on CURRENT
SUPERSEDED_PENALTY = -50      # ThatsThem-only name when a confirmed current exists
RECENCY_SUPERSEDED_PENALTY = -80  # a NEWER source dates them at a different current address
RECENCY_CONFLICT_YEARS = 0.5  # "newer elsewhere" must lead the unit tenure by at least this
RECENT_WITHIN_YEARS = 6       # a move-in this recent lets a single source read as High confidence
CONFLICT_PENALTY = -40        # sources disagree on the person's CURRENT address (undated case)
STALE_AFTER_YEARS = 2         # a unit "last reported" older than this flags a possible move-out
STALE_PENALTY = -25

# Evidence families: sites that resell the SAME upstream data must not count as independent
# corroboration. TruePeopleSearch / FastPeopleSearch and their sisters share one backend, so their
# agreement is ONE piece of evidence, not four. USPhoneBook, CyberBackgroundChecks and ThatsThem are
# treated as separate families (best current assumption; tighten if more overlap is confirmed).
SOURCE_FAMILY = {
    "TruePeopleSearch": "TPS-FPS",
    "FastPeopleSearch": "TPS-FPS",
    "SearchPeopleFree": "TPS-FPS",
    "FastBackgroundCheck": "TPS-FPS",
    "USPhoneBook": "USPhoneBook",
    "CyberBackgroundChecks": "CyberBackgroundChecks",
    "ThatsThem": "ThatsThem",
    "DCProperty": "DCProperty",     # independent government lineage
    "OpenFEC": "OpenFEC",           # independent federal lineage
    "SECEdgar": "SECEdgar",         # independent federal lineage
}


def _families(sources) -> set:
    """Collapse source names to their evidence families (so resold data isn't double-counted)."""
    return {SOURCE_FAMILY.get(s, s) for s in sources}


@dataclass
class Candidate:
    """One real person, merged across sources."""
    name: str
    records: list[Person] = field(default_factory=list)

    @property
    def ages(self) -> list[int]:
        return sorted({r.age for r in self.records if r.age})

    @property
    def phones(self) -> list[str]:
        out: list[str] = []
        for r in self.records:
            for p in r.phones:
                if p not in out:
                    out.append(p)
        return out

    @property
    def sources(self) -> list[str]:
        return sorted({r.source for r in self.records if r.source})

    @property
    def relatives(self) -> list[str]:
        out: list[str] = []
        for r in self.records:
            for rel in r.relatives:
                if rel not in out:
                    out.append(rel)
        return out

    def name_key(self) -> str:
        return self.records[0].name_key() if self.records else self.name.upper()

    def age_compatible(self, p: Person) -> bool:
        """A record belongs to this person only if its age doesn't conflict (≤1 yr apart)."""
        known = [r.age for r in self.records if r.age]
        if not known or p.age is None:
            return True
        return any(abs(a - p.age) <= 1 for a in known)

    def aka_keys(self) -> set:
        keys: set = set()
        for r in self.records:
            keys |= r.aka_keys()
        return keys


def _ages_compatible(a_ages, b_ages) -> bool:
    if not a_ages or not b_ages:
        return True
    return any(abs(x - y) <= 1 for x in a_ages for y in b_ages)


def _first_name(key: str) -> str:
    return key.split()[0] if key else ""


@dataclass
class ScoredCandidate:
    name: str
    score: int
    confidence: str            # High / Medium-High / Medium / Low
    sources: list[str]
    phones: list[str]
    age: Optional[int]
    evidence: list[str]
    is_former: bool = False    # target appears only as a prior address
    since: str = ""            # raw move-in date for the confirmed unit address (if a source gave one)
    since_year: Optional[float] = None  # comparable form of `since`, for recency tie-breaking
    relatives: list[str] = field(default_factory=list)  # for household grouping among co-residents
    moved_to: str = ""         # for a FORMER resident: where they now live (move-chain)
    prior_addresses: list[Address] = field(default_factory=list)  # Address objs (migration map / roster)
    current_address: Optional[Address] = None  # best concrete current address (map: former's moved-to pin)


NICKNAMES = {
    "ROBERT": "BOB", "BOB": "ROBERT",
    "WILLIAM": "BILL", "BILL": "WILLIAM",
    "SAMANTHA": "SAM", "SAM": "SAMANTHA",
    "KATHERINE": "KATE", "KATE": "KATHERINE",
    "RICHARD": "DICK", "DICK": "RICHARD",
    "CHARLES": "CHUCK", "CHUCK": "CHARLES",
    "JAMES": "JIM", "JIM": "JAMES",
    "MICHAEL": "MIKE", "MIKE": "MICHAEL",
    "THOMAS": "TOM", "TOM": "THOMAS",
}

def _fuzzy_match(name1: str, name2: str) -> bool:
    """Fuzzy match first names, exact match last names."""
    n1_toks = name1.upper().split()
    n2_toks = name2.upper().split()
    if not n1_toks or not n2_toks:
        return False
    
    # Must have same last name (or one is a single name, which we won't fuzzy match)
    if len(n1_toks) < 2 or len(n2_toks) < 2:
        return name1.upper() == name2.upper()
        
    last1, last2 = n1_toks[-1], n2_toks[-1]
    if last1 != last2:
        return False
        
    first1, first2 = n1_toks[0], n2_toks[0]
    if first1 == first2:
        return True
        
    if NICKNAMES.get(first1) == first2 or NICKNAMES.get(first2) == first1:
        return True
        
    ratio = difflib.SequenceMatcher(None, first1, first2).ratio()
    return ratio > 0.85

def merge_people(people: list[Person]) -> list[Candidate]:
    """Combine duplicate people across sources.

    Merges by fuzzy first name + exact last name, but keeps two DIFFERENT people with the same name apart when
    their ages conflict (e.g. a Jr./Sr., or two unrelated 'John Smith's in one building) - so
    one person's unit match can't absorb another's addresses/phones.
    """
    cands: list[Candidate] = []
    for p in people:
        key = p.name_key()
        matched = False
        for cand in cands:
            if _fuzzy_match(cand.name_key(), key) and cand.age_compatible(p):
                cand.records.append(p)
                if len(p.name) > len(cand.name):
                    cand.name = p.name
                matched = True
                break
        if not matched:
            cands.append(Candidate(name=p.name, records=[p]))

    # AKA reconciliation: fold together candidates linked by a shared alias (e.g. a maiden name
    # that one source lists under 'Also Seen As'), but ONLY when they share the same first name and
    # their ages don't conflict. The first-name guard stops a household member listed in someone's
    # "observed names" (a spouse) from being absorbed; the age guard keeps two different people who
    # happen to share one alias apart.
    i = 0
    while i < len(cands):
        a = cands[i]
        j = i + 1
        while j < len(cands):
            b = cands[j]
            if (a.aka_keys() & b.aka_keys()
                    and _first_name(a.name_key()) == _first_name(b.name_key())
                    and _ages_compatible(a.ages, b.ages)):
                a.records.extend(b.records)
                if len(b.name) > len(a.name):
                    a.name = b.name
                cands.pop(j)
            else:
                j += 1
        i += 1
    return cands


def _confidence(score: int, corroborated: bool = False, since_recent: bool = False) -> str:
    """Confidence from the score, tightened by corroboration/recency.

    A single authoritative "Lives at" (score ≥ 85) only reads as **High** when it's either
    corroborated by a 2nd source OR backed by a recent move-in date. A lone, undated/old
    authoritative source is real but uncorroborated → **Medium-High** (don't overstate it).
    """
    if score >= 85 and (corroborated or since_recent):
        return "High"
    if score >= 85:      # single authoritative source, undated or old - real but uncorroborated
        return "Medium-High"
    if score >= 40:      # a lone association source (ThatsThem 45) → Medium
        return "Medium"
    return "Low"


def _recency_split(cand: Candidate, target: Address) -> tuple[Optional[float], Optional[float]]:
    """(newest dated move-in AT the target unit, newest dated move-in ELSEWHERE) for a candidate.

    Only records that carry a move-in date and a real (house+street) current address count.
    A newer "elsewhere" than "here" is the strongest signal that a source's unit-current claim
    is stale - the person's most recent known move was away from the unit.
    """
    here: list[float] = []
    away: list[float] = []
    for r in cand.records:
        y = r.since_year()
        if y is None or not r.current_address:
            continue
        lvl = match_address(target, r.current_address)
        if lvl == MatchLevel.EXACT:
            here.append(y)
        elif (lvl == MatchLevel.NONE and r.current_address.house_number
              and r.current_address.street):
            away.append(y)
    return (max(here) if here else None, max(away) if away else None)


def _best_source_verdicts(cand: Candidate, target: Address) -> dict[str, Classification]:
    """Each source's single strongest verdict for this candidate (dedups duplicate captures)."""
    rank = {Classification.CURRENT: 3, Classification.BUILDING: 2,
            Classification.OTHER: 1, Classification.FORMER: 0}
    best: dict[str, Classification] = {}
    for r in cand.records:
        c = classify(r, target).classification
        if r.source not in best or rank[c] > rank[best[r.source]]:
            best[r.source] = c
    return best


def score_candidates(people: list[Person], target: Address
                     ) -> tuple[list[ScoredCandidate], list[ScoredCandidate],
                                list[ScoredCandidate], int]:
    """Return (current, possible, former, building_only_count).

    - current : unit-confirmed current resident(s), ranked by weighted/corroborated score.
    - possible: people a unit-specific source SEARCH returned but whose listed current
      address is elsewhere - a lead only (recent move-in the DB hasn't updated, or an
      unlisted former). Surfaced when there's no confirmed current resident.
    - former  : the unit appears only as a prior address.
    Building-level-only associations (unit unknown) are excluded and counted separately.
    """
    # Context-only sources (absentee/entity owners of record) are background, not residents.
    candidates = merge_people([p for p in people if p.source not in CONTEXT_SOURCES and p.source != "DCVacant"])
    is_vacant = any(p.source == "DCVacant" for p in people)

    verdicts = [(cand, _best_source_verdicts(cand, target)) for cand in candidates]
    has_confirmed_current = any(
        cls == Classification.CURRENT and src in CONFIRMING_SOURCES
        for _, bsv in verdicts for src, cls in bsv.items())

    current: list[ScoredCandidate] = []
    possible: list[ScoredCandidate] = []
    former: list[ScoredCandidate] = []
    building_only = 0
    today_year = date.today().year

    for cand, bsv in verdicts:
        current_sources = {s for s, c in bsv.items() if c == Classification.CURRENT}
        former_signal = any(c == Classification.FORMER for c in bsv.values())
        building_signal = any(c == Classification.BUILDING for c in bsv.values())
        # Sources whose unit-specific search returned this person (association, address may lag).
        unit_search_sources = {r.source for r in cand.records
                               if target.unit and r.searched_unit == target.unit}
        # Sources that list this person's CURRENT address as somewhere OTHER than the unit.
        current_elsewhere = {r.source for r in cand.records
                             if r.current_address and r.current_address.house_number
                             and r.current_address.street
                             and match_address(target, r.current_address) == MatchLevel.NONE}

        # Address objects for the migration map / roster: every distinct prior address across the
        # candidate's records, plus the best concrete current address (has house# + street).
        prior_addrs: list[Address] = []
        _seen_pa: set = set()
        for r in cand.records:
            for pa in r.prior_addresses:
                d = pa.display()
                if d and d not in _seen_pa:
                    _seen_pa.add(d)
                    prior_addrs.append(pa)
        cur_addr = next((r.current_address for r in cand.records
                         if r.current_address and r.current_address.house_number
                         and r.current_address.street), None)

        if not current_sources and not former_signal:
            if unit_search_sources:
                n_fam = len(_families(unit_search_sources))
                lead_addr = next((r.current_address.display() for r in cand.records
                                  if r.current_address and r.current_address.house_number
                                  and match_address(target, r.current_address) == MatchLevel.NONE), "")
                possible.append(ScoredCandidate(
                    name=cand.name, score=10 * n_fam, confidence="Unconfirmed",
                    sources=cand.sources, phones=cand.phones,
                    age=(cand.ages[0] if cand.ages else None),
                    evidence=[f"returned by {n_fam} source family(ies)' search for this unit, but "
                              f"their listed current address is elsewhere - a lead only (possible "
                              f"recent move-in the database hasn't updated, or a former resident)"],
                    moved_to=lead_addr, prior_addresses=prior_addrs, current_address=cur_addr))
            elif building_signal:
                building_only += 1
            continue

        # Score by evidence FAMILY, not by site: within a family (resold data) take the single
        # strongest weight, so N sister-sites agreeing can't out-score two independent sources.
        fam_best: dict[str, int] = {}
        evidence: list[str] = []
        for src, cls in bsv.items():
            w = SIGNAL_WEIGHTS.get((src, cls))
            if w is None and cls == Classification.CURRENT:
                w = DEFAULT_CURRENT_WEIGHT
            if w is None:
                continue
            fam = SOURCE_FAMILY.get(src, src)
            fam_best[fam] = max(fam_best.get(fam, w), w)
            if cls == Classification.CURRENT:
                evidence.append(f"{src}: lists as current resident of the unit")
            elif cls == Classification.FORMER:
                rec = next((r for r in cand.records if r.source == src), None)
                where = rec.current_address.raw if rec and rec.current_address else "elsewhere"
                evidence.append(f"{src}: unit is a PRIOR address (now at {where})")
        score = sum(fam_best.values())

        current_families = _families(current_sources)
        if len(current_families) >= 2:
            score += CORROBORATION_BONUS
            evidence.append(f"corroborated by {len(current_families)} independent source families")

        confirmed_here = bool(current_sources & CONFIRMING_SOURCES)
        if (has_confirmed_current and not confirmed_here and not former_signal
                and current_sources and current_sources <= ASSOCIATION_SOURCES):
            score += SUPERSEDED_PENALTY
            evidence.append("likely prior tenant (another resident is confirmed current here)")

        # Recency: a source dating this person at a NEWER current address elsewhere than the
        # unit tenure is the strongest "moved out" signal - demote them out of `current`.
        here_since, away_since = _recency_split(cand, target)
        moved_out_by_recency = (here_since is not None and away_since is not None
                                and away_since >= here_since + RECENCY_CONFLICT_YEARS)
        if moved_out_by_recency:
            score += RECENCY_SUPERSEDED_PENALTY
            evidence.append(f"a source dates a newer current address elsewhere ({int(away_since)}) "
                            f"than the unit tenure (since {int(here_since)}) - likely moved out")
        elif current_sources and current_elsewhere:
            # Undated disagreement: some source says the unit is current, another says elsewhere.
            score += CONFLICT_PENALTY
            evidence.append("sources disagree on this person's current address "
                            "(also listed as living elsewhere) - lower confidence")

        # Staleness: if the newest "last reported" date for the unit address is well in the past,
        # the person may have moved (records stopped updating here). A mild flag, not a demotion.
        until_years = [r.until_year() for r in cand.records
                       if r.current_until and r.current_address
                       and match_address(target, r.current_address) == MatchLevel.EXACT]
        until_here = max((y for y in until_years if y is not None), default=None)
        if (until_here is not None and until_here < today_year - STALE_AFTER_YEARS
                and not moved_out_by_recency):
            score += STALE_PENALTY
            evidence.append(f"last reported at the unit in {int(until_here)} - may have moved since")

        # Newest dated move-in among the sources that confirm the unit address (for display).
        dated_here = [r for r in cand.records if r.current_since and r.current_address
                      and match_address(target, r.current_address) == MatchLevel.EXACT]
        since_raw, since_year = "", here_since
        if dated_here:
            best = max(dated_here, key=lambda r: r.since_year() or 0)
            since_raw = best.current_since
            evidence.append(f"resident of the unit since {since_raw}")

        # Surface per-record identifier notes (FEC occupation/employer, owner-of-record, SEC filing).
        for r in cand.records:
            if r.note and r.note not in evidence:
                evidence.append(r.note)

        # Move-chain: where this person's records place their CURRENT address if not the unit.
        moved_to = next((r.current_address.display() for r in cand.records
                         if r.current_address and r.current_address.house_number
                         and match_address(target, r.current_address) == MatchLevel.NONE), "")

        corroborated = len(current_families) >= 2
        since_recent = since_year is not None and since_year >= today_year - RECENT_WITHIN_YEARS
        sc = ScoredCandidate(
            name=cand.name, score=score,
            confidence=_confidence(score, corroborated, since_recent),
            sources=cand.sources, phones=cand.phones,
            age=(cand.ages[0] if cand.ages else None), evidence=evidence,
            is_former=(moved_out_by_recency or (former_signal and score <= 0)),
            since=since_raw, since_year=since_year, relatives=cand.relatives, moved_to=moved_to,
            prior_addresses=prior_addrs, current_address=cur_addr)

        if sc.is_former:
            former.append(sc)
        elif score > 0:
            current.append(sc)

    # Network link (GATED 2026-08-18): a `possible` lead who is a relative of a confirmed current
    # resident is a STRONGER lead - a plausible co-resident the databases may not have updated. We
    # ANNOTATE it as such but do NOT promote it to `current`: by definition of `possible`, that
    # person's own records place them at a DIFFERENT address, and asserting them as current here
    # reintroduces the founding "lives elsewhere, shown as current" error (e.g. a relative living in
    # Reston shown as current in DC). It stays a labeled lead.
    confirmed_relatives = [rel for c in current for rel in c.relatives]
    if confirmed_relatives:
        note = "relative of a confirmed resident here - stronger lead (not confirmed current)"
        for p_cand in possible:
            if note not in p_cand.evidence and any(_fuzzy_match(p_cand.name, rel)
                                                   for rel in confirmed_relatives):
                p_cand.evidence.insert(0, note)

    # Vacancy Override
    if is_vacant:
        for c in current:
            c.is_former = True
            c.evidence.insert(0, "DC Government lists this property as VACANT (overrides current status)")
            former.append(c)
        current.clear()
        for p in possible:
            p.is_former = True
            p.evidence.insert(0, "DC Government lists this property as VACANT (overrides possible status)")
            former.append(p)
        possible.clear()

    # Rank by score, then by most-recent confirmed move-in (undated sorts last at equal score).
    current.sort(key=lambda c: (c.score, c.since_year if c.since_year is not None else -1.0),
                 reverse=True)
    possible.sort(key=lambda c: c.score, reverse=True)
    former.sort(key=lambda c: c.name)
    return current, possible, former, building_only
