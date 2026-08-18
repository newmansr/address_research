"""
test_pipeline.py — assertion-based regression tests for the whole engine.

Runs with plain `python test_pipeline.py` (no pytest needed). Exits non-zero if any
assertion fails, so it doubles as a CI gate.

Coverage (the layers where every historical bug lived):
  - normalize_address / match_address / classify        (unit-exact matching)
  - since_to_year                                        (recency parsing)
  - the per-source parsers                               (USPhoneBook / CBC / ThatsThem / TPS / FPS)
  - merge_people                                         (age-aware, no cross-person bleed)
  - score_candidates                                     (corroboration, supersession, recency,
                                                          confidence tiers, possible/former tiers)

The scoring tests build `Person` objects directly (not scraped text) so they pin the ranking
logic precisely; the parser tests use compact fixtures matching the REAL page anchors.
"""

from __future__ import annotations

import json
import sys
from datetime import date

import apis

from resident_core import (Address, Classification, MatchLevel, Person, classify,
                           match_address, name_key, normalize_address, since_to_year)
from parsers import parse_source, parse_thatsthem
from scoring import score_candidates, merge_people, _confidence

# ── tiny test harness ────────────────────────────────────────────────────────

_PASS = 0
_FAIL = 0


def check(cond: bool, msg: str) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  FAIL: {msg}")


def eq(got, want, msg: str) -> None:
    check(got == want, f"{msg} (got {got!r}, want {want!r})")


TARGET_637 = normalize_address("800 New Jersey Ave SE Unit 637, Washington, DC 20003")


def P(name, source, addr=None, since="", priors=None, searched_unit="", age=None, phones=None):
    """Build a Person the way a parser would."""
    return Person(
        name=name, source=source, age=age,
        current_address=normalize_address(addr) if addr else None,
        current_since=since,
        prior_addresses=[normalize_address(a) for a in (priors or [])],
        searched_unit=searched_unit, phones=list(phones or []),
    )


def names(cands):
    return [c.name for c in cands]


# ── address normalization & matching ─────────────────────────────────────────

def test_normalize_units():
    for raw, unit in [
        ("800 New Jersey Ave SE, APT 637, Washington, DC 20003", "637"),
        ("800 New Jersey Ave SE #637", "637"),
        ("800 New Jersey Ave SE Unit 0637", "637"),      # leading zeros stripped
        ("800 New Jersey Ave APT 637 SE, Washington, DC", "637"),
        ("800 New Jersey Ave SE", ""),                   # no unit
    ]:
        eq(normalize_address(raw).unit, unit, f"unit of {raw!r}")
    a = normalize_address("800 New Jersey Ave SE, APT 637, Washington, DC 20003-3993")
    eq(a.house_number, "800", "house number")
    eq(a.zip, "20003", "zip")


def test_match_levels():
    exact = normalize_address("800 New Jersey Ave SE #637, Washington, DC")
    other_unit = normalize_address("800 New Jersey Ave SE #938, Washington, DC")
    no_unit = normalize_address("800 New Jersey Ave SE, Washington, DC")
    diff = normalize_address("12 Faraway Rd, Reston, VA")
    eq(match_address(TARGET_637, exact), MatchLevel.EXACT, "same unit -> EXACT")
    eq(match_address(TARGET_637, other_unit), MatchLevel.NONE, "diff unit -> NONE")
    eq(match_address(TARGET_637, no_unit), MatchLevel.BUILDING, "no unit -> BUILDING")
    eq(match_address(TARGET_637, diff), MatchLevel.NONE, "diff street -> NONE")


def test_classify():
    cur = P("A B", "FastPeopleSearch", "800 New Jersey Ave SE #637, Washington, DC")
    eq(classify(cur, TARGET_637).classification, Classification.CURRENT, "current match")
    former = P("C D", "USPhoneBook", "12 Faraway Rd, Reston, VA",
               priors=["800 New Jersey Ave SE #637, Washington, DC"])
    eq(classify(former, TARGET_637).classification, Classification.FORMER, "prior-only -> FORMER")
    other = P("E F", "USPhoneBook", "800 New Jersey Ave SE #938, Washington, DC")
    eq(classify(other, TARGET_637).classification, Classification.OTHER, "diff unit -> OTHER")


def test_since_to_year():
    for s, want in [("Jul 2018", 2018.5), ("2015", 2015.0), ("07/2018", 2018.5),
                    ("2018-07", 2018.5), ("", None), ("(434)", None)]:
        got = since_to_year(s)
        got = None if got is None else round(got, 3)
        eq(got, None if want is None else round(want, 3), f"since_to_year({s!r})")


# ── parsers (compact fixtures matching the real page anchors) ─────────────────

def test_parse_usphonebook():
    txt = ("People Living at 800 New Jersey Ave SE Washington, DC "
           "Kathryn Tenkhoff Kathryn P Tenkhoff , Age 34 Lives at "
           "800 New Jersey Ave SE, APT 637, Washington, DC 20003-3993 "
           "Prior addresses: 4455 Madison Ave, APT 211S, Kansas City, MO 64111-4458 "
           "Relatives: John Tenkhoff , Grace Tenkhoff View Report "
           "Jason Dijak Jason M Dijak , Age 51 Lives at 12 Faraway Rd, Reston, VA 20190-1000 "
           "Prior addresses: 800 New Jersey Ave SE, APT 1104, Washington, DC 20003-1000 "
           "Relatives: Mary Dijak View Report")
    ppl = parse_source("USPhoneBook", txt)
    eq(len(ppl), 2, "USPhoneBook people count")
    t = next((p for p in ppl if "Tenkhoff" in p.name), None)
    check(t is not None, "USPhoneBook parsed Tenkhoff")
    eq(t.name, "Kathryn P Tenkhoff", "USPhoneBook name")
    eq(t.age, 34, "USPhoneBook age")
    eq(t.current_address.unit, "637", "USPhoneBook current unit")
    eq(classify(t, TARGET_637).classification, Classification.CURRENT, "UPB Tenkhoff current@637")


def test_parse_cyberbackgroundchecks():
    txt = ("results for 800 New Jersey Ave Se Apt 637 Washington, DC "
           "Kathryn P Tenkhoff Age: 34 Lives at "
           "800 New Jersey Ave APT 637 SE, Washington, DC 20003 3993 [District Of Columbia County] "
           "Used to live 1331 Maryland Ave APT 213 SW, Washington, DC 20024 2840 [District Of Columbia County] "
           "Phones | (615) 491-0165 | (615) 599-2618 | Related to | Anne M Tenkhoff | VIEW DETAILS "
           "Amy Williams Amy K Williams Age: 47 Lives at "
           "800 New Jersey Ave APT 938 SE, Washington, DC 20003 4081 [District Of Columbia County] "
           "Phones | (816) 472-1381 | Related to | Gordie Davidson | VIEW DETAILS")
    ppl = parse_source("CyberBackgroundChecks", txt)
    eq(len(ppl), 2, "CBC people count")
    t = next((p for p in ppl if "Tenkhoff" in p.name), None)
    check(t is not None, "CBC parsed Tenkhoff")
    eq(t.current_address.unit, "637", "CBC current unit")
    eq(len(t.phones), 2, "CBC phone count")
    a = next((p for p in ppl if "Amy" in p.name), None)
    eq(a.current_address.unit, "938", "CBC Amy current unit 938")


def test_parse_thatsthem():
    # Real ThatsThem pages wrap Person nodes in an @graph list.
    html = ('<script type="application/ld+json">'
            '{"@context":"https://schema.org","@graph":[{"@type":"Person","name":"Rahsaan Bernard",'
            '"homeLocation":{"@type":"Place","address":{"@type":"PostalAddress",'
            '"streetAddress":"800 New Jersey Ave SE #1104","addressLocality":"Washington",'
            '"addressRegion":"DC","postalCode":"20003"}},"telephone":["(202) 555-0100"]}]}'
            '</script>')
    ppl = parse_thatsthem(html)
    eq(len(ppl), 1, "ThatsThem people count")
    eq(ppl[0].name, "Rahsaan Bernard", "ThatsThem name")
    eq(ppl[0].current_address.unit, "1104", "ThatsThem unit")
    eq(ppl[0].source, "ThatsThem", "ThatsThem source tag")


def test_parse_tps_detail_with_since():
    txt = ("Home / T / Tenkhoff / Kathryn Tenkhoff Kathryn Tenkhoff Age 34, Born Feb 1992 "
           "Lives in Washington, DC Current Address This is the most recently reported address "
           "for Kathryn Tenkhoff. 800 New Jersey Ave SE #637 Washington, DC 20003 "
           "District Of Columbia County (Mar 2021 - Jun 2026) "
           "Previous Addresses 4455 Madison Ave #211S Kansas City, MO 64111 Jackson County "
           "(Jan 2015 - Mar 2021) Phone Numbers "
           "Includes the current and past phone numbers for Kathryn Tenkhoff. (615) 491-0165 - Wireless")
    ppl = parse_source("TruePeopleSearch", txt)
    eq(len(ppl), 1, "TPS people count")
    eq(ppl[0].name, "Kathryn Tenkhoff", "TPS name")
    eq(ppl[0].current_address.unit, "637", "TPS current unit")
    eq(ppl[0].current_since, "Mar 2021", "TPS since (range start)")
    eq(ppl[0].current_until, "Jun 2026", "TPS until (range end)")
    eq(ppl[0].age, 34, "TPS age")
    eq(classify(ppl[0], TARGET_637).classification, Classification.CURRENT, "TPS current@637")


def test_parse_fps_detail_with_since():
    txt = ("Home W Williams Amy Williams Age 47 Current Address (Since Jul 2018) "
           "800 New Jersey Ave SE #938 Washington, DC 20003 District Of Columbia County "
           "Full Name: Amy K Williams Phone Numbers (816) 472-1381")
    ppl = parse_source("FastPeopleSearch", txt)
    eq(len(ppl), 1, "FPS people count")
    eq(ppl[0].name, "Amy K Williams", "FPS name (Full Name, trailing stops stripped)")
    eq(ppl[0].current_address.unit, "938", "FPS current unit")
    eq(ppl[0].current_since, "Jul 2018", "FPS since")
    # The exact historical bug: a #938 person crawled from a 637 search must NOT be current@637.
    eq(classify(ppl[0], TARGET_637).classification, Classification.OTHER,
       "REGRESSION: Amy #938 is not current@637")


# ── merge & scoring ──────────────────────────────────────────────────────────

def test_merge_keeps_ages_apart():
    people = [
        P("John Smith", "FastPeopleSearch", "800 New Jersey Ave SE #637, Washington, DC", age=30),
        P("John Smith", "USPhoneBook", "99 Other Rd, Reston, VA", age=70),
    ]
    merged = merge_people(people)
    eq(len(merged), 2, "two John Smiths with conflicting ages stay separate")


def test_corroborated_current_is_high():
    people = [
        P("Kathryn Tenkhoff", "FastPeopleSearch", "800 New Jersey Ave SE #637, Washington, DC",
          since="Mar 2021", age=34, phones=["(615) 491-0165"]),
        P("Kathryn Tenkhoff", "USPhoneBook", "800 New Jersey Ave SE #637, Washington, DC", age=34),
        P("Kathryn Tenkhoff", "CyberBackgroundChecks", "800 New Jersey Ave SE #637, Washington, DC",
          age=34),
    ]
    current, possible, former, _ = score_candidates(people, TARGET_637)
    eq(names(current)[:1], ["Kathryn Tenkhoff"], "corroborated current is #1")
    eq(current[0].confidence, "High", "3-source corroboration -> High")
    eq(current[0].is_former, False, "not former")
    eq(current[0].since, "Mar 2021", "since surfaced on the winner")
    check(any("since Mar 2021" in e for e in current[0].evidence), "since appears in evidence")


def test_single_undated_source_is_medium_high():
    people = [P("Solo Resident", "USPhoneBook", "800 New Jersey Ave SE #637, Washington, DC")]
    current, _, _, _ = score_candidates(people, TARGET_637)
    eq(current[0].confidence, "Medium-High",
       "lone authoritative source, undated -> Medium-High (not High)")


def test_single_recent_dated_source_is_high():
    recent = str(date.today().year - 1)
    people = [P("Fresh Mover", "FastPeopleSearch", "800 New Jersey Ave SE #637, Washington, DC",
                since=recent)]
    current, _, _, _ = score_candidates(people, TARGET_637)
    eq(current[0].confidence, "High", "lone source WITH a recent move-in date -> High")


def test_recency_conflict_moves_out():
    # TPS dates them at the unit since 2016; FPS dates a NEWER current address elsewhere (2023).
    people = [
        P("Mover Person", "TruePeopleSearch", "800 New Jersey Ave SE #637, Washington, DC",
          since="Jan 2016"),
        P("Mover Person", "FastPeopleSearch", "500 Far Away Blvd, Reston, VA", since="Jan 2023"),
    ]
    current, possible, former, _ = score_candidates(people, TARGET_637)
    check("Mover Person" not in names(current), "newer-address-elsewhere demotes out of current")
    check("Mover Person" in names(former), "recency conflict -> classified former")


def test_recency_tiebreak_orders_recent_first():
    people = [
        P("Older Tenant", "FastPeopleSearch", "800 New Jersey Ave SE #637, Washington, DC",
          since="Jan 2015"),
        P("Newer Tenant", "FastPeopleSearch", "800 New Jersey Ave SE #637, Washington, DC",
          since="Jan 2022"),
    ]
    current, _, _, _ = score_candidates(people, TARGET_637)
    eq(names(current)[0], "Newer Tenant", "equal score -> more-recent move-in ranks first")


def test_possible_lead_when_no_confirmed_current():
    # Samantha-style: a unit search returned her, but her listed current address is elsewhere.
    people = [P("Samantha Karlin", "USPhoneBook", "800 4th St SW #N414, Washington, DC",
                searched_unit="637", phones=["(202) 555-0123"])]
    current, possible, former, _ = score_candidates(people, TARGET_637)
    eq(names(current), [], "no confirmed current")
    eq(names(possible), ["Samantha Karlin"], "surfaced as an unconfirmed lead")


def test_former_via_prior_address():
    people = [P("Gone Guy", "USPhoneBook", "12 Faraway Rd, Reston, VA",
                priors=["800 New Jersey Ave SE #637, Washington, DC"])]
    current, possible, former, _ = score_candidates(people, TARGET_637)
    eq(names(former), ["Gone Guy"], "prior-address-only -> former")
    eq(names(current), [], "not current")


def test_thatsthem_superseded_by_confirmed_current():
    people = [
        P("Kathryn Tenkhoff", "FastPeopleSearch", "800 New Jersey Ave SE #637, Washington, DC",
          since="Mar 2021"),
        P("Old Tenant", "ThatsThem", "800 New Jersey Ave SE #637, Washington, DC"),
    ]
    current, _, _, _ = score_candidates(people, TARGET_637)
    check("Old Tenant" not in names(current),
          "ThatsThem-only name is superseded when a confirmed current exists")
    eq(names(current), ["Kathryn Tenkhoff"], "confirmed current wins")


def test_confidence_tiers_direct():
    eq(_confidence(110, corroborated=True), "High", "corroborated -> High")
    eq(_confidence(110, since_recent=True), "High", "recent -> High")
    eq(_confidence(110), "Medium-High", "lone/undated -> Medium-High")
    eq(_confidence(45), "Medium", "association -> Medium")
    eq(_confidence(5), "Low", "weak -> Low")


# ── Part 1: locality guard, suffixes, AKA reconciliation ─────────────────────

def test_match_address_locality_guard():
    boston = normalize_address("12 Main St #3, Boston, MA 02101")
    springfield = normalize_address("12 Main St #3, Springfield, IL 62701")
    eq(match_address(boston, springfield), MatchLevel.NONE,
       "same street#unit, different city/state/zip -> NONE")
    terse = normalize_address("12 Main St #3")   # no locality on candidate = wildcard
    eq(match_address(boston, terse), MatchLevel.EXACT, "missing locality on candidate = wildcard")


def test_name_key_suffixes():
    eq(name_key("Robert Kinsler Jr"), "ROBERT KINSLER", "Jr stripped")
    eq(name_key("Robert Kinsler III"), "ROBERT KINSLER", "III stripped")
    eq(name_key("Robert A Kinsler"), "ROBERT KINSLER", "middle initial ignored")


def test_suffix_merge():
    people = [P("Robert Kinsler", "USPhoneBook", "6624 River Rd, Bethesda, MD 20817"),
              P("Robert Kinsler Jr", "FastPeopleSearch", "6624 River Rd, Bethesda, MD 20817")]
    eq(len(merge_people(people)), 1, "'Robert Kinsler Jr' merges with 'Robert Kinsler'")


def test_aka_merge_maiden_name():
    a = P("Amy Williams", "TruePeopleSearch", "800 New Jersey Ave SE #938, Washington, DC", age=47)
    a.aka = ["Amy K Davidson", "Amy Kathleen Williams"]
    b = P("Amy Davidson", "FastPeopleSearch", "800 New Jersey Ave SE #938, Washington, DC", age=47)
    eq(len(merge_people([a, b])), 1, "maiden-name AKA reconciles the two variants")


def test_aka_does_not_absorb_household_member():
    k = P("Kathryn Tenkhoff", "CyberBackgroundChecks",
          "800 New Jersey Ave SE #637, Washington, DC", age=34)
    k.aka = ["Kirk Tenkhoff", "Kirk M Tenkhoff", "Kathryn Tenkhoff"]  # spouse in observed-names
    kirk = P("Kirk Tenkhoff", "FastPeopleSearch",
             "800 New Jersey Ave SE #637, Washington, DC", age=40)
    eq(len(merge_people([k, kirk])), 2,
       "a household member in observed-names is NOT absorbed (different first name)")


def test_extract_aka_parsers():
    cbc = ("Jane P Doe Age: 44 Lives at 1 A St #2, Washington, DC 20003 [District Of Columbia County] "
           "Other observed names | Jane Doe | Jane P Smith | [1] more... "
           "Phones | (202) 555-0000 | VIEW DETAILS")
    ppl = parse_source("CyberBackgroundChecks", cbc)
    check(ppl and "Jane P Smith" in ppl[0].aka, "CBC 'Other observed names' parsed into aka")


# ── Part 2: evidence families & current-address conflict ─────────────────────

def test_same_family_is_not_corroboration():
    # Three FPS-FAMILY sites agreeing = ONE piece of evidence, not three.
    fam = [P("Solo Fam", "FastPeopleSearch", "800 New Jersey Ave SE #637, Washington, DC"),
           P("Solo Fam", "TruePeopleSearch", "800 New Jersey Ave SE #637, Washington, DC"),
           P("Solo Fam", "SearchPeopleFree", "800 New Jersey Ave SE #637, Washington, DC")]
    current, _, _, _ = score_candidates(fam, TARGET_637)
    eq(current[0].confidence, "Medium-High", "same-family agreement isn't independent corroboration")
    check(not any("corroborated" in e for e in current[0].evidence),
          "no corroboration claim for a single family")
    eq(current[0].score, 110, "family score = strongest single weight, not the sum (110 not 288)")


def test_two_families_corroborate_to_high():
    ppl = [P("Two Fam", "FastPeopleSearch", "800 New Jersey Ave SE #637, Washington, DC"),
           P("Two Fam", "CyberBackgroundChecks", "800 New Jersey Ave SE #637, Washington, DC")]
    current, _, _, _ = score_candidates(ppl, TARGET_637)
    eq(current[0].confidence, "High", "two independent families -> High")
    check(any("corroborated by 2" in e for e in current[0].evidence), "corroboration noted")


def test_undated_current_conflict_lowers_confidence():
    ppl = [P("Split Person", "TruePeopleSearch", "800 New Jersey Ave SE #637, Washington, DC"),
           P("Split Person", "CyberBackgroundChecks", "999 Other Rd, Reston, VA 20190")]
    current, _, _, _ = score_candidates(ppl, TARGET_637)
    check(current and any("disagree" in e for e in current[0].evidence),
          "undated current-address conflict is flagged and penalized")


# ── Part 3: move-out end date & phone recency ────────────────────────────────

def test_phone_ranking_and_inactive():
    # Numbers must be NANP-valid: _norm_phone uses the `phonenumbers` library and drops invalids.
    txt = ("Home / D / Doe / John Doe John Doe Age 50 Current Address This is the most recently "
           "reported address for John Doe. 800 New Jersey Ave SE #637 Washington, DC 20003 "
           "District Of Columbia County (Jan 2022 - Jun 2026) Phone Numbers "
           "Includes the current and past phone numbers for John Doe. "
           "(202) 234-5678 - Wireless Possible Primary Phone Last reported May 2026 Verizon "
           "(202) 333-4444 - Landline Last reported Aug 2010 AT&T Inactive "
           "(202) 555-6666 - Wireless Last reported Jan 2020 T-Mobile Email Addresses")
    ppl = parse_source("TruePeopleSearch", txt)
    # Compare by digits so the assertions don't couple to the display format (NATIONAL + carrier).
    digits = ["".join(c for c in p if c.isdigit()) for p in ppl[0].phones]
    check(digits and digits[0] == "2022345678", "primary/most-recently-reported phone ranks first")
    check("2023334444" not in digits, "Inactive phone is dropped")
    check("2025556666" in digits, "active (older) phone kept")


def test_staleness_flag_from_until():
    old = str(date.today().year - 4)
    p = P("Stale Resident", "TruePeopleSearch", "800 New Jersey Ave SE #637, Washington, DC")
    p.current_until = f"Jan {old}"
    current, _, _, _ = score_candidates([p], TARGET_637)
    check(current and any("may have moved" in e for e in current[0].evidence),
          "an old last-reported date flags possible move-out")


# ── Part 5: relatives (household grouping) ───────────────────────────────────

def test_cbc_relatives_parsed():
    cbc = ("Jane P Doe Age: 44 Lives at 1 A St #2, Washington, DC 20003 [District Of Columbia County] "
           "Related to | John A Doe | Mary Doe | [7] more... Associated with | Bob Smith | VIEW DETAILS")
    ppl = parse_source("CyberBackgroundChecks", cbc)
    check(ppl and "John A Doe" in ppl[0].relatives and "Mary Doe" in ppl[0].relatives,
          "CBC 'Related to' parsed into relatives")
    check(ppl and "Bob Smith" not in ppl[0].relatives, "associates are not folded into relatives")


def test_scored_candidate_carries_relatives():
    p = P("Head Person", "USPhoneBook", "800 New Jersey Ave SE #637, Washington, DC")
    p.relatives = ["Spouse Person"]
    current, _, _, _ = score_candidates([p], TARGET_637)
    check(current and "Spouse Person" in current[0].relatives,
          "relatives flow through to the ScoredCandidate (for household grouping)")


def test_relative_lead_not_promoted_to_current():
    # A confirmed resident's relative who is returned by the unit search but LIVES ELSEWHERE must
    # stay a labeled lead, not be asserted as current (the gated "Network Elevation").
    k1 = P("Kathryn Tenkhoff", "CyberBackgroundChecks", "800 New Jersey Ave SE #637, Washington, DC", age=34)
    k2 = P("Kathryn Tenkhoff", "USPhoneBook", "800 New Jersey Ave SE #637, Washington, DC", age=34)
    k1.relatives = ["John Tenkhoff"]
    k2.relatives = ["John Tenkhoff"]
    john = P("John Tenkhoff", "USPhoneBook", "999 Faraway Rd, Reston, VA 20190", searched_unit="637")
    current, possible, former, _ = score_candidates([k1, k2, john], TARGET_637)
    check("John Tenkhoff" not in names(current),
          "elevation gated: a relative living elsewhere is NOT asserted as current")
    check("John Tenkhoff" in names(possible), "the relative remains a labeled lead")


# ── Independent open-data API sources (apis.py) ──────────────────────────────

def _patch_http(payload):
    apis._http_get = lambda url, headers=None, timeout=45, proxy="": (200, json.dumps(payload))


def test_api_name_helpers():
    eq(apis._flip_lastfirst("RICHARDSON, DAVID H. DR. PHD"), "David H Richardson",
       "FEC name flip drops honorifics")
    eq(apis._flip_lastfirst("SMITH, JOHN"), "John Smith", "simple flip")
    check(apis._is_entity("NEW JERSEY AT H LLC"), "LLC detected as entity")
    check(not apis._is_entity("Kathryn Tenkhoff"), "person is not an entity")
    eq(apis._dc_owner_display("TENKHOFF KATHRYN P", False), "Kathryn P Tenkhoff",
       "DC 'LAST FIRST' reordered to 'First Last'")
    eq(apis._dc_owner_display("NEW JERSEY AT H LLC", True), "NEW JERSEY AT H LLC", "entity kept as-is")


def test_fec_fetch_matches_and_filters():
    saved = apis._http_get
    try:
        _patch_http({"pagination": {"pages": 1, "count": 2}, "results": [
            {"contributor_name": "TENKHOFF, KATHRYN P",
             "contributor_street_1": "800 New Jersey Ave SE Apt 637", "contributor_city": "Washington",
             "contributor_state": "DC", "contributor_zip": "200033993",
             "contributor_occupation": "Attorney", "contributor_employer": "Some Firm LLP",
             "contribution_receipt_date": "2024-05-01T00:00:00"},
            {"contributor_name": "SMITH, BOB", "contributor_street_1": "999 Elsewhere Rd",
             "contributor_city": "Reston", "contributor_state": "VA", "contributor_zip": "20190",
             "contribution_receipt_date": "2023-01-01"}]})
        recs = apis.fetch_fec("800 New Jersey Ave SE", "Washington", "DC", "20003", "637", archive=False)
    finally:
        apis._http_get = saved
    eq(len(recs), 1, "FEC keeps only the address match (drops the Reston donor)")
    eq(recs[0].name, "Kathryn P Tenkhoff", "FEC name flipped")
    eq(recs[0].source, "OpenFEC", "FEC source tag")
    eq(recs[0].current_since, "2024", "FEC contribution year -> since")
    check("Attorney" in recs[0].note, "occupation surfaced in note")


def test_dc_property_owner_occupant_vs_entity():
    saved, apis._itspe_url = apis._http_get, "http://test/FeatureServer/0"
    try:
        _patch_http({"features": [
            {"attributes": {"OWNERNAME": "TENKHOFF KATHRYN P",
                            "PREMISEADD": "800 NEW JERSEY AVE SE WASHINGTON DC 20003",
                            "UNITNUMBER": "637", "ADDRESS1": "800 NEW JERSEY AVE SE APT 637",
                            "ADDRESS2": "", "CITYSTZIP": "WASHINGTON DC 20003"}},
            {"attributes": {"OWNERNAME": "NEW JERSEY AT H LLC",
                            "PREMISEADD": "800 NEW JERSEY AVE SE WASHINGTON DC 20003",
                            "UNITNUMBER": "", "ADDRESS1": "1100 NEW JERSEY AVE SE STE 1000",
                            "ADDRESS2": "", "CITYSTZIP": "WASHINGTON DC 20003"}}]})
        recs = apis.fetch_dc_property("800 New Jersey Ave SE", "Washington", "DC", "20003", "637",
                                     archive=False)
    finally:
        apis._http_get, apis._itspe_url = saved, None
    occ = [p for p in recs if p.source == "DCProperty"]
    ctx = [p for p in recs if p.source == "DCPropertyOwner"]
    check(occ and occ[0].name == "Kathryn P Tenkhoff", "owner-occupant person (name reordered)")
    check(ctx and "LLC" in ctx[0].name, "entity/absentee owner is context-only (DCPropertyOwner)")


def test_api_sources_corroborate_as_independent_families():
    from scoring import _families
    eq(len(_families({"OpenFEC", "DCProperty", "CyberBackgroundChecks"})), 3,
       "each API source is its own evidence family")
    ppl = [P("Kathryn Tenkhoff", "CyberBackgroundChecks", "800 New Jersey Ave SE #637, Washington, DC"),
           P("Kathryn Tenkhoff", "OpenFEC", "800 New Jersey Ave SE #637, Washington, DC", since="2024")]
    current, _, _, _ = score_candidates(ppl, TARGET_637)
    eq(current[0].confidence, "High", "aggregator + independent API = 2 families -> High")


def test_context_owner_not_ranked_as_resident():
    ctx = P("New Jersey At H Llc", "DCPropertyOwner", "800 New Jersey Ave SE #637, Washington, DC")
    ctx.note = "owner of record (entity/rental-owned)"
    current, possible, former, _ = score_candidates([ctx], TARGET_637)
    eq(len(current) + len(possible) + len(former), 0,
       "a context-only owner record is never ranked as a resident")


def test_drift_markers_ignore_stubs_and_boilerplate():
    from parsers import has_result_markers
    check(not has_result_markers("Are you human? Please fill out the re-captcha. @context schema.org"),
          "a bot-check/captcha stub is NOT flagged as parser drift (it's a block)")
    check(not has_result_markers('script type="application/ld+json" {"@type":"Organization"}'),
          "boilerplate JSON-LD (on every page) is not a populated-result marker")
    check(has_result_markers("most recently reported address for Jane Doe. 1 A St NE County"),
          "a populated TPS detail page IS a result marker")
    check(has_result_markers("People Living at 800 New Jersey Ave SE ... View Report"),
          "a populated USPhoneBook page IS a result marker")


# ── Feature 1: building roster, households, move-chains (graph.py) ────────────

def test_graph_roster_households_movechain():
    import graph
    ppl = [
        P("Kathryn Tenkhoff", "CyberBackgroundChecks", "800 New Jersey Ave SE #637, Washington, DC",
          phones=["(202) 234-5678"]),
        P("Kathryn Tenkhoff", "USPhoneBook", "800 New Jersey Ave SE #637, Washington, DC",
          phones=["(202) 234-5678"]),
        P("Kirk Tenkhoff", "FastPeopleSearch", "800 New Jersey Ave SE #637, Washington, DC",
          phones=["(202) 234-5678"]),   # spouse - shares the household phone
        P("Tim Kutta", "USPhoneBook", "1877 Gina Dr, Tallahassee, FL 32303",
          priors=["800 New Jersey Ave SE #941, Washington, DC"]),   # former, moved out
    ]
    B = ("800 New Jersey Ave SE", "Washington", "DC", "20003")

    units = graph.discover_units(ppl, graph._building(*B))
    check("637" in units and "941" in units, "discover_units finds both units")

    rows = graph.build_roster(ppl, *B)
    r637 = next(r for r in rows if r["unit"] == "637")
    check(r637["status"] == "current" and r637["top"].name.startswith("Kathryn"),
          "roster: 637 -> Kathryn (current)")

    clusters = graph.household_clusters(ppl)
    kt = [g for g in clusters if any("Kathryn" in c.name for c in g)]
    check(kt and any("Kirk" in c.name for c in kt[0]),
          "household: Kathryn + Kirk clustered via shared phone")

    chains = graph.move_chains(ppl, *B)
    check(any(ch["name"].endswith("Kutta") and "Tallahassee" in ch["now_at"]
              and "941" in ch["from_units"] for ch in chains),
          "move-chain: Kutta unit 941 -> Tallahassee")


def test_address_display_cleanup():
    a = normalize_address("802 10th St UNIT 2 NE, Washington, DC 20002 9155 [District Of Columbia County]")
    d = a.display()
    check("[" not in d and "County" not in d, "display() drops the county bracket")
    check("802" in d and "2" == a.unit, "display() keeps house number and unit")


# ── Feature 2: history / monitoring (history.py) ─────────────────────────────

def test_history_monitoring():
    import history, tempfile, os

    class Rec:  # minimal stand-in for a ScoredCandidate (what record_run reads)
        def __init__(self, name):
            self.name, self.evidence, self.relatives, self.phones = name, [], [], []
            self.score, self.confidence = 90, "High"

    old, tmp = history.DB_PATH, tempfile.mktemp(suffix=".db")
    history.DB_PATH = tmp
    try:
        S, Z, U = "800 New Jersey Ave SE", "20003", "637"
        history.record_run(S, Z, U, [Rec("Alice Smith")], [], [])          # run 1
        delta = history.get_delta(S, Z, U, [Rec("Bob Jones")])             # run 2 vs run 1
        history.record_run(S, Z, U, [Rec("Bob Jones")], [], [])            # run 2
        eq(delta.get("BOB JONES"), "[NEW]", "a new resident is tagged [NEW]")
        eq(delta.get("ALICE SMITH"), "[REMOVED]", "a departed resident is tagged [REMOVED]")

        tl = history.get_timeline(S, Z, U)
        eq(len(tl), 2, "timeline records both runs")
        eq(tl[0][1], ["Bob Jones"], "timeline is newest-first")

        ch = {(c["change"], c["name"]) for c in history.building_changes(S, Z)}
        check(("NEW", "BOB JONES") in ch and ("REMOVED", "ALICE SMITH") in ch,
              "building change log reports NEW Bob + REMOVED Alice")
    finally:
        history.DB_PATH = old
        try:
            os.remove(tmp)
        except OSError:
            pass


# ── Feature 4: person enrichment (enrich.py) ─────────────────────────────────

def test_enrich_fec_name_filter_and_format():
    import enrich
    saved = enrich._json_get
    enrich._json_get = lambda url, headers=None, timeout=45, proxy="": (200, {
        "pagination": {"pages": 1}, "results": [
            {"contributor_name": "TENKHOFF, KATHRYN P", "contributor_occupation": "Attorney",
             "contributor_employer": "Some Firm LLP", "contribution_receipt_date": "2024-05-01",
             "contributor_city": "Washington", "contributor_state": "DC"},
            {"contributor_name": "SMITH, BOB", "contributor_occupation": "Other",
             "contributor_employer": "X"}]})
    try:
        fec = enrich.enrich_fec("Kathryn Tenkhoff", state="DC")
    finally:
        enrich._json_get = saved
    eq(len(fec), 1, "enrich_fec keeps only the same first+last donor (drops Bob Smith)")
    eq(fec[0]["occupation"], "Attorney", "occupation extracted")
    lines = enrich.format_enrichment("Kathryn Tenkhoff", {
        "fec": fec, "dc_property": [{"address": "800 New Jersey Ave SE #637"}], "opencorporates": []})
    check(any("Attorney" in l for l in lines), "formatted FEC occupation line")
    check(any("Owns DC property" in l for l in lines), "formatted property line")


# ── runner ───────────────────────────────────────────────────────────────────

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(tests)} test group(s)...\n")
    for t in tests:
        try:
            t()
        except Exception as e:  # a throw is a failure, not a crash
            global _FAIL
            _FAIL += 1
            print(f"  ERROR in {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{'=' * 50}")
    print(f"  {_PASS} passed, {_FAIL} failed")
    print("=" * 50)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
