"""
lookup.py - orchestrator: scrape sources, score, rank, and (optionally) export per unit.

What works with NO browser (fast, robust):
    python lookup.py "800 New Jersey Ave SE" Washington DC 20003 --units 1104,1038,1015

Add Cloudflare sources (USPhoneBook + CyberBackgroundChecks) - requires Chrome:
    python lookup.py "800 New Jersey Ave SE" Washington DC 20003 --units 1104 --browser

Batch a whole range to a spreadsheet (one row per unit):
    python lookup.py "800 New Jersey Ave SE" Washington DC 20003 --units 1101-1110 --browser --out building.xlsx

`--units` accepts a single unit, a comma list (637,737,837), a range (1101-1110),
or is omitted for a building-level sweep. ThatsThem is fetched once per building and
reused for every unit, which makes ranges essentially free.

`--out FILE.csv|.xlsx` writes one row per unit (most likely resident, confidence, phones,
corroborating sources, alternates, former residents).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from resident_core import Address, normalize_address, name_key, match_address, MatchLevel
from parsers import parse_thatsthem, parse_source, has_result_markers
from scoring import score_candidates, CONTEXT_SOURCES
from sources import (fetch_thatsthem, fetch_browser_sources, BROWSER_SOURCES,
                     thatsthem_building_url, DETAIL_CRAWL, load_cached_captures)


def parse_units(spec: str) -> list[str]:
    """'637,737' or '1101-1110' or '637' -> ['637', ...]. Empty -> []."""
    if not spec:
        return []
    out: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            out.extend(str(u) for u in range(lo, hi + 1))
        else:
            out.append(part)
    return out


def normalize_street(street: str) -> str:
    return re.sub(r"\b(se|sw|ne|nw)\b",
                  lambda m: m.group().upper(), street.title(), flags=re.IGNORECASE)


def gather_people(street, city, state, zip_code, units, use_browser, browser_sources,
                  proxies="", delay=0.0, session_dir=""):
    """Return all Person records across sources (ThatsThem always; browser optional)."""
    people = []

    # ThatsThem - one browser-free fetch covers the whole building.
    print("\n[ThatsThem] fetching building (browser-free)...")
    proxy_first = proxies.split(",")[0] if proxies else ""
    html = fetch_thatsthem(street, city, state, zip_code, proxy=proxy_first)
    tt = parse_thatsthem(html) if html else []
    print(f"  parsed {len(tt)} associated people")
    people.extend(tt)

    # Browser (Cloudflare) sources - per unit, on the user's machine.
    if use_browser:
        specs = []
        # Fallback: if the browser-free ThatsThem was rate-limited, fetch it via the CDP
        # browser too (real-browser fingerprint often gets through where `requests` is blocked).
        # ThatsThem is inherently BUILDING-level - it ignores any Apt/Unit in the URL and returns
        # every resident tagged with their own unit (we filter by unit downstream). So one fetch
        # covers all units in a range.
        if not tt:
            print("  ThatsThem will be retried via the browser (requests was blocked).")
            specs.append({
                "name": "ThatsThem", "html": True, "street": street, "unit": "",
                "url": thatsthem_building_url(street, city, state, zip_code),
            })
        for unit in (units or [""]):
            for name in browser_sources:
                build_url, wait_css = BROWSER_SOURCES[name]
                specs.append({
                    "name": name,
                    "url": build_url(street, city, state, zip_code, unit),
                    "street": street, "unit": unit, "wait_css": wait_css,
                })
        print(f"\n[Browser] fetching {len(specs)} page(s) "
              f"across {', '.join(browser_sources)}...")
        residents: dict[str, int] = {}
        marked: dict[str, bool] = {}
        for item in fetch_browser_sources(specs, proxies=proxies, delay=delay,
                                           session_dir=session_dir):
            parsed = parse_source(item["name"], item["text"])
            # `searched_unit` (the "returned by this unit's search" association signal, used to
            # surface possible leads) applies only to SUMMARY sources whose page is a
            # people-living-here list. Detail-crawl sources (FPS/TPS/sisters) fetch individual
            # person pages - including RELATIVES - and give real addresses, so tagging them would
            # wrongly float a crawled relative into the "possible" leads.
            if item.get("unit") and item["name"] not in DETAIL_CRAWL:
                for p in parsed:
                    p.searched_unit = item["unit"]
            people.extend(parsed)
            residents[item["name"]] = residents.get(item["name"], 0) + len(parsed)
            if has_result_markers(item["text"]):
                marked[item["name"]] = True
        # Per-source parse yield: a source that returned page(s) but 0 residents either hit a
        # throttled stub, or - if the page has result markers - the PARSER has drifted (loud flag).
        print("\n[Browser] parse yield per source:")
        for name in sorted(set(browser_sources) | set(residents)):
            n = residents.get(name, 0)
            if n == 0 and marked.get(name):
                flag = "  (!) PARSER DRIFT? page has result markers but parsed 0 - layout changed?"
            elif n == 0:
                flag = "  (!) 0 residents parsed - blocked/stub or no match at this address"
            else:
                flag = ""
            print(f"    {name}: {n} resident(s){flag}")

    return people


def gather_from_cache(street, units, verbose=True):
    """Replay archived raw/ captures through the parsers - NO network.

    Mirrors gather_people's parse + `searched_unit` tagging so scoring behaves exactly as it
    would on a fresh scrape. Lets you iterate on parsers/scoring without re-hitting (and getting
    blocked by) the sites, and doubles as a real-data regression check. `verbose=False` silences
    the replay report (used by the eval harness, which runs many units).
    """
    from parsers import PARSERS
    captures = load_cached_captures(street, units)
    if not captures:
        return []
    people = []
    # Aggregate by the MAPPED source: a detail-crawl source's summary capture parses to 0 by
    # design (its residents come from the *Detail captures), so per-label counts would falsely
    # flag it. Counting per mapped source flags only a source that truly yielded nothing.
    residents: dict[str, int] = {}
    captured: dict[str, int] = {}
    marked: dict[str, bool] = {}
    for item in captures:
        label = item["name"]
        # 'TruePeopleSearchDetail' -> 'TruePeopleSearch'; other labels are the source name as-is.
        source = label[:-6] if label.endswith("Detail") else label
        captured[source] = captured.get(source, 0) + 1
        # A detail-crawl SUMMARY parses to 0 by design (residents come from *Detail pages), so it
        # must not count toward drift - only real detail/summary-source pages can signal drift.
        is_detail_summary = source in DETAIL_CRAWL and not label.endswith("Detail")
        if not is_detail_summary and has_result_markers(item["text"]):
            marked[source] = True
        if source not in PARSERS:
            continue
        parsed = parse_source(source, item["text"])
        # Only summary sources carry the unit-search association tag (detail pages give real
        # addresses - tagging them would float a crawled relative into the leads). See gather_people.
        if item.get("unit") and source not in DETAIL_CRAWL:
            for p in parsed:
                p.searched_unit = item["unit"]
        people.extend(parsed)
        residents[source] = residents.get(source, 0) + len(parsed)
    if verbose:
        print(f"\n[cache] replayed {len(captures)} archived capture(s) from raw/:")
        _report_capture_age(captures)
        for source in sorted(captured):
            n = residents.get(source, 0)
            if n == 0 and marked.get(source):
                flag = "  (!) PARSER DRIFT? page has result markers but parsed 0 - layout changed?"
            elif n == 0:
                flag = f"  (!) 0 residents from {captured[source]} capture(s) - stale/stub?"
            else:
                flag = ""
            print(f"    {source}: {n} resident(s){flag}")
    return people


def _report_capture_age(captures, stale_days: int = 90) -> None:
    """Print the newest capture's date/age and warn if the cache is old (data may be stale)."""
    stamps = [c.get("stamp") for c in captures if c.get("stamp")]
    if not stamps:
        return
    from datetime import datetime
    try:
        dt = datetime.strptime(max(stamps), "%Y%m%d_%H%M%S")
    except ValueError:
        return
    age = (datetime.now() - dt).days
    warn = f"  (WARNING: >{stale_days}d old - re-scrape for current data)" if age > stale_days else ""
    print(f"    newest capture: {dt:%Y-%m-%d} ({age} day(s) ago){warn}")


def gather_api_sources(street, city, state, zip_code, api_sources, proxies="", fec_opts=None):
    """Query the INDEPENDENT open-data APIs (apis.py) once per building - no browser, no cache.

    Each returns Person records directly (JSON), tagged with its own source so scoring treats it as
    a distinct evidence family. One building-level query per source covers every unit (per-unit
    classification happens downstream). Absentee/entity owners come back as context (DCPropertyOwner).
    """
    from apis import API_SOURCES
    people = []
    fec_opts = fec_opts or {}
    print(f"\n[APIs] querying {', '.join(api_sources)} (independent open data)...")
    proxy_first = proxies.split(",")[0] if proxies else ""
    for name in api_sources:
        fn = API_SOURCES.get(name)
        if not fn:
            continue
        try:
            opts = {"proxy": proxy_first}
            if name == "OpenFEC":
                opts.update(fec_opts)
            recs = fn(street, city, state, zip_code, "", **opts)
            print(f"    {name}: {len(recs)} record(s)")
            people.extend(recs)
        except Exception as e:
            print(f"    !! {name} error: {type(e).__name__}: {e}")
    return people


def owner_context(people, target: Address, do_unmask: bool = False, proxy: str = "") -> list[str]:
    """Owner-of-record lines (absentee/entity owners) for the target unit - background, not a
    resident. e.g. 'NEW JERSEY AT H LLC - owner of record (entity/rental-owned); mails to ...'."""
    from apis import _is_entity
    out: list[str] = []
    for p in people:
        if p.source in CONTEXT_SOURCES and p.current_address \
                and match_address(target, p.current_address) != MatchLevel.NONE:
            line = p.name + (f" - {p.note}" if p.note else "")
            
            if do_unmask and _is_entity(p.name):
                from enrich import unmask_dc_llc
                llc_data = unmask_dc_llc(p.name, proxy)
                if llc_data:
                    unmasked = []
                    for d in llc_data:
                        unmasked.append(f"Unmasked [{d['status']}]: {d['registered_agent']} at {d['agent_address']}")
                    if unmasked:
                        line += "\n       -> " + "\n       -> ".join(unmasked)
                        
            if line not in out:
                out.append(line)
    return out


def _hist_street(x) -> str:
    """Canonical street key for history (house# + normalized street) - consistent whether we start
    from an Address (per-unit path) or a raw street string (roster / --history path)."""
    a = x if isinstance(x, Address) else normalize_address(x)
    return f"{a.house_number} {a.street}".strip()


def report_unit(people, target: Address, do_delta: bool = True, do_osint: bool = False,
                do_forward: bool = False, do_enrich: bool = False):
    current, possible, former, building_only = score_candidates(people, target)

    delta = {}
    if do_delta:
        try:
            import history
            hs = _hist_street(target)   # canonical street key (Address has .zip, not .zip_code!)
            delta = history.get_delta(hs, target.zip, target.unit, current)
            history.record_run(hs, target.zip, target.unit, current, possible, former)
        except Exception:
            pass

    print(f"\n  Unit {target.unit or '(whole building)'}")
    
    # Print removed candidates
    for r_name, r_tag in delta.items():
        if r_tag == "[REMOVED]":
            print(f"    [REMOVED]: {r_name} (was Current in last run, no longer Current)")
            
    if current:
        top = current[0]     # single most-likely; the rest go to household / other candidates below
        age = f", age {top.age}" if top.age else ""
        since = f", since {top.since}" if top.since else ""
        ph = ("  ph: " + ", ".join(top.phones)) if top.phones else ""
        d_tag = delta.get(top.name.upper(), "")
        tag_str = f" {d_tag} " if d_tag else " "
        print(f"    MOST LIKELY:{tag_str}{top.name}{age}{since}  [{top.confidence} confidence]{ph}")
        for ev in top.evidence:
            print(f"       - {ev}")
        if do_osint:
            try:
                from llm_parser import osint_evaluate
                osint_res = osint_evaluate(top.name, target.city, target.state)
                if osint_res and "No OSINT evidence found" not in osint_res:
                    print(f"       - [OSINT] {osint_res}")
            except Exception as e:
                print(f"       - [OSINT Error] {e}")

        if do_enrich:
            try:
                from enrich import enrich_person, format_enrichment
                for ln in format_enrichment(top.name,
                                            enrich_person(top.name, target.city, target.state)):
                    print(f"       - [enrich] {ln}")
            except Exception as e:
                print(f"       - [enrich error] {type(e).__name__}: {e}")

        if len(current) > 1:
            # Household grouping: a co-listed resident who is a relative of the top pick, or shares
            # a phone with them, is likely the SAME household rather than a competing answer.
            primary_top = current[0]
            top_rel_keys = {name_key(r) for r in primary_top.relatives}
            top_phones = set(primary_top.phones)
            household, others = [], []
            for c in current[1:]:
                same_home = name_key(c.name) in top_rel_keys or bool(top_phones & set(c.phones))
                (household if same_home else others).append(c)
            if household:
                h_strs = []
                for c in household:
                    d_tag = delta.get(c.name.upper(), "")
                    tag_str = f" {d_tag} " if d_tag else " "
                    h_strs.append(f"{tag_str}{c.name} [{c.confidence}]")
                print("    Same household (likely co-residents): " + ", ".join(h_strs))
            if others:
                print("    Other candidates:")
                for c in others:
                    src = ", ".join(c.sources)
                    since_c = f", since {c.since}" if c.since else ""
                    d_tag = delta.get(c.name.upper(), "")
                    tag_str = f" {d_tag} " if d_tag else " "
                    print(f"       .{tag_str}{c.name}{since_c}  [{c.confidence}; {src}; score {c.score}]")
    else:
        print("    MOST LIKELY: (no unit-confirmed current resident in the data)")
        # No confirmed current -> surface unit-search associations as leads (may be stale).
        if possible:
            print("    Possible (unconfirmed - listed address is elsewhere; DB may be stale):")
            for c in possible:
                age = f", age {c.age}" if c.age else ""
                ph = ("  ph: " + ", ".join(c.phones)) if c.phones else ""
                d_tag = delta.get(c.name.upper(), "")
                tag_str = f" {d_tag} " if d_tag else " "
                # Forward trace (derived, no re-query): where the lead's own records place them now.
                fwd = (f"  ->  currently listed at {c.moved_to}"
                       if do_forward and getattr(c, "moved_to", "") else "")
                print(f"       ?{tag_str}{c.name}{age}  [{', '.join(c.sources)}]{ph}{fwd}")

    if former:
        # Move-chain: show where each former resident went (their current address elsewhere).
        for c in former:
            d_tag = delta.get(c.name.upper(), "")
            tag = f"{d_tag} " if d_tag else ""
            dest = f"  ->  now at {c.moved_to}" if getattr(c, "moved_to", "") else ""
            print(f"    Former (moved out): {tag}{c.name}{dest}")
    for line in owner_context(people, target, do_unmask=do_enrich):
        print(f"    Owner of record: {line}")
    if building_only:
        print(f"    (+{building_only} people associated with the building, unit unknown)")
    return current, possible, former, building_only


def report_roster(people, street, city, state, zip_code):
    """Building-level view (Feature 1): per-unit roster + household clusters + move-chains.
    Also records each unit to history and tags changes vs the previous run (Feature 2)."""
    import graph, history
    rows = graph.build_roster(people, street, city, state, zip_code)
    print(f"\n  BUILDING ROSTER - {len(rows)} unit(s) present in the data")
    print(f"    {'Unit':<8}{'Status':<11}{'Confidence':<13}Most likely resident")
    print("    " + "-" * 66)
    for r in rows:
        top = r["top"]
        name = top.name if top else "(none)"
        conf = (top.confidence if top else "-")
        tag = ""
        try:
            hs = _hist_street(street)
            d = history.get_delta(hs, zip_code, r["unit"], r["current"])
            history.record_run(hs, zip_code, r["unit"], r["current"], r["possible"], r["former"])
            if top:
                tag = d.get(top.name.upper(), "")
        except Exception:
            pass
        print(f"    {r['unit']:<8}{r['status']:<11}{conf:<13}{name}{(' ' + tag) if tag else ''}")

    clusters = graph.household_clusters(people)
    if clusters:
        print(f"\n  HOUSEHOLDS - {len(clusters)} cluster(s) of linked people (shared phone/relative)")
        for grp in sorted(clusters, key=len, reverse=True):
            print(f"    - {', '.join(c.name for c in grp)}")

    chains = graph.move_chains(people, street, city, state, zip_code)
    if chains:
        print(f"\n  MOVE-CHAINS - {len(chains)} former resident(s) traced")
        for ch in chains:
            print(f"    - {ch['name']}: unit {', '.join(ch['from_units'])}  ->  now at {ch['now_at']}")
            
    # Dossier generation hook
    # Passed dynamically via kwargs to avoid changing signature
    return rows, clusters, chains


def show_history(street, zip_code, units):
    """Read-only monitoring dashboard (Feature 2): resident timeline for given units, or the
    building change log (NEW/REMOVED between the two latest runs per unit). No scraping."""
    import history
    street = _hist_street(street)
    if units:
        for unit in units:
            tl = history.get_timeline(street, zip_code, unit)
            print(f"\n  Unit {unit} - resident timeline ({len(tl)} recorded run(s), newest first):")
            if not tl:
                print("    (no history yet - run a scrape for this unit first)")
            for ts, names in tl:
                print(f"    {ts[:19].replace('T', ' ')}   {', '.join(names) or '(none)'}")
    else:
        changes = history.building_changes(street, zip_code)
        print(f"\n  Building change log - {len(changes)} change(s) between the two latest runs per unit:")
        if not changes:
            print("    (no changes, or not enough history yet - scrape the building at least twice)")
        for ch in sorted(changes, key=lambda c: (c["unit"], c["change"])):
            print(f"    [{ch['change']:<7}] unit {ch['unit']}: {ch['name']}")


def _utf8_console() -> None:
    """Make stdout/stderr tolerate non-ASCII (arrows, box-drawing, curly quotes) so a stray Unicode
    char in a print can't crash the run on a legacy cp1252 Windows console (errors are replaced)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None) -> None:
    _utf8_console()
    ap = argparse.ArgumentParser(description="OSINT Address Research Platform.")
    ap.add_argument("street", nargs="?", default="",
                    help="street address (required for address lookup, optional for --phone/--name)")
    ap.add_argument("city", nargs="?", default="Washington")
    ap.add_argument("state", nargs="?", default="DC")
    ap.add_argument("zip", nargs="?", default="20003")
    ap.add_argument("--units", default="", help="e.g. 637  |  637,737,837  |  1101-1110")
    ap.add_argument("--browser", action="store_true",
                    help="also scrape Cloudflare sources (needs Chrome)")
    ap.add_argument("--from-cache", action="store_true",
                    help="replay archived raw/ captures instead of scraping (no network)")
    ap.add_argument("--proxies", default="",
                    help="comma-separated list of proxies (host:port or user:pass@host:port); "
                         "used for rotation during browser scraping to bypass IP blocks")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="polite base seconds (+ jitter) between page loads to avoid rate limits")
    ap.add_argument("--keep-session", action="store_true",
                    help="reuse a persistent Chrome profile (./.chrome_profile) so a solved "
                         "Cloudflare/Turnstile clearance carries over between runs (fewer challenges)")
    ap.add_argument("--sources",
                    default="FastPeopleSearch,USPhoneBook,CyberBackgroundChecks,TruePeopleSearch",
                    help="comma list of browser sources to fetch (with --browser). FPS/TPS each "
                         "crawl detail pages (authoritative) and add time per unit.")
    ap.add_argument("--apis", action="store_true",
                    help="also query the INDEPENDENT open-data APIs (no browser): FEC donors, "
                         "DC property owner, SEC EDGAR")
    ap.add_argument("--api-sources", default="OpenFEC,DCProperty,DCVacant",
                    help="comma list of API sources (with --apis): OpenFEC, DCProperty, DCVacant, SECEdgar")
    ap.add_argument("--fec-key", default="",
                    help="openFEC API key (else FEC_API_KEY env, else rate-limited DEMO_KEY)")
    ap.add_argument("--fec-zip9", default="",
                    help="9-digit ZIP of the building - needed to reach a specific building in a "
                         "dense ZIP (FEC can't filter by street)")
    ap.add_argument("--fec-pages", type=int, default=5,
                    help="max 100-record pages to scan from FEC (DEMO_KEY is rate-limited)")
    ap.add_argument("--llm", action="store_true",
                    help="add an optional plain-English summary via local Ollama")
    ap.add_argument("--osint", action="store_true",
                    help="perform heavy web-search OSINT on top candidates via DuckDuckGo + LLM")
    ap.add_argument("--enrich", action="store_true",
                    help="enrich the top resident via independent APIs: FEC occupation/employer, "
                         "DC property owned, OpenCorporates officerships")
    ap.add_argument("--forward", action="store_true",
                    help="show a forward trace (where each 'Possible' lead is currently listed)")
    ap.add_argument("--roster", action="store_true",
                    help="building view: per-unit roster + household clusters + move-chains "
                         "(instead of the per-unit report)")
    ap.add_argument("--history", action="store_true",
                    help="monitoring: show the recorded change log for this address (no scrape) - "
                         "a unit's resident timeline with --units, else the building change log")
    ap.add_argument("--out", default="",
                    help="write one row per unit to a spreadsheet (.csv or .xlsx)")
    # ── OSINT expansion flags ──
    ap.add_argument("--phone", default="",
                    help="reverse phone lookup: find the person behind a phone number")
    ap.add_argument("--name", default="",
                    help="forward name search: find addresses for a person (format: 'First Last')")
    ap.add_argument("--dossier", action="store_true",
                    help="generate a self-contained HTML dossier report")
    ap.add_argument("--monitor", action="store_true",
                    help="monitoring mode: scrape, compare to last run, write change report if deltas")
    args = ap.parse_args(argv)

    street = normalize_street(args.street)
    city, state = args.city.title(), args.state.upper()
    units = parse_units(args.units)

    browser_sources = [s.strip() for s in args.sources.split(",")
                       if s.strip() in BROWSER_SOURCES]
    from apis import API_SOURCES
    api_sources = [s.strip() for s in args.api_sources.split(",") if s.strip() in API_SOURCES]

    if args.phone:
        print(f"Reverse Phone Lookup: {args.phone}")
        print("=" * 64)
        from sources import PHONE_SOURCES, fetch_browser_sources
        from parsers import parse_source
        specs = []
        for src_name, url_fn in PHONE_SOURCES.items():
            url = url_fn(args.phone)
            specs.append({"name": src_name, "url": url, "street": "", "unit": ""})
        session_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chrome_profile") \
            if args.keep_session else ""
        results = fetch_browser_sources(specs, proxies=args.proxies, delay=args.delay,
                                         session_dir=session_dir)
        people = []
        for item in results:
            parsed = parse_source(item["name"], item["text"])
            people.extend(parsed)
        if not people:
            print("  No results found for this phone number.")
            return
        print("\nRESULTS")
        seen = set()
        for p in people:
            if p.name.upper() in seen:
                continue
            seen.add(p.name.upper())
            age = f", age {p.age}" if p.age else ""
            addr = f"  Address: {p.current_address.display()}" if p.current_address else ""
            phones = f"  Phones: {', '.join(p.phones)}" if p.phones else ""
            print(f"  {p.name}{age} (via {p.source})")
            if addr:
                print(f"    {addr}")
            if phones:
                print(f"    {phones}")
            if p.relatives:
                print(f"    Relatives: {', '.join(p.relatives[:5])}")
        print("=" * 64)
        return

    if args.name:
        name_parts = args.name.strip().split()
        if len(name_parts) < 2:
            print("Error: --name requires 'First Last' format.")
            return
        first, last = name_parts[0], name_parts[-1]
        print(f"Forward Name Search: {first} {last} in {city}, {state}")
        print("=" * 64)
        from sources import NAME_SOURCES, fetch_browser_sources
        from parsers import parse_source
        specs = []
        for src_name, url_fn in NAME_SOURCES.items():
            url = url_fn(first, last, city, state)
            specs.append({"name": src_name, "url": url, "street": "", "unit": ""})
        session_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chrome_profile") \
            if args.keep_session else ""
        results = fetch_browser_sources(specs, proxies=args.proxies, delay=args.delay,
                                         session_dir=session_dir)
        people = []
        for item in results:
            parsed = parse_source(item["name"], item["text"])
            people.extend(parsed)
        if not people:
            print(f"  No results found for {first} {last}.")
            return
        print("\nRESULTS")
        for p in people:
            age = f", age {p.age}" if p.age else ""
            addr = p.current_address.display() if p.current_address else "unknown"
            print(f"  {p.name}{age} (via {p.source})")
            print(f"    Current: {addr}")
            if p.prior_addresses:
                print(f"    Prior: {'; '.join(a.display() for a in p.prior_addresses[:3])}")
            if p.phones:
                print(f"    Phones: {', '.join(p.phones)}")
        print("=" * 64)
        return

    if not args.street:
        ap.error("street is required for address lookup (or use --phone/--name)")

    print(f"Researching: {street}, {city}, {state} {args.zip}")
    print(f"Units: {', '.join(units) if units else '(whole building)'}")

    if args.history:      # read-only monitoring dashboard - no scraping
        print("Source: recorded run history (no scrape)")
        print("=" * 64)
        show_history(street, args.zip, units)
        print("=" * 64)
        return

    if args.from_cache:
        print("Source: archived raw/ captures (no network)")
    elif args.browser:
        print(f"Browser sources: {', '.join(browser_sources)}")
    if args.apis:
        print(f"API sources (independent): {', '.join(api_sources)}")
    print("=" * 64)

    session_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chrome_profile") \
        if args.keep_session else ""
    if args.from_cache:
        people = gather_from_cache(street, units)
    else:
        people = gather_people(street, city, state, args.zip, units, args.browser, browser_sources,
                               proxies=args.proxies, delay=args.delay, session_dir=session_dir)
    if args.apis:
        people += gather_api_sources(street, city, state, args.zip, api_sources, proxies=args.proxies,
                                     fec_opts={"api_key": args.fec_key, "zip9": args.fec_zip9,
                                               "max_pages": args.fec_pages})
    if not people:
        print("\nNo data collected.")
        if args.from_cache:
            print("  No archived captures matched this address in raw/. Do a live --browser run "
                  "first to populate the cache.")
        elif not args.browser:
            print("  Tip: only the browser-free ThatsThem source was tried and it's blocked. "
                  "Re-run with --browser to use USPhoneBook / CyberBackgroundChecks / "
                  "TruePeopleSearch / FastPeopleSearch.")
        return

    print("\n" + "=" * 64)
    print("RESULTS")

    if args.roster:
        rows, clusters, chains = report_roster(people, street, city, state, args.zip)
        if args.dossier:
            from dossier import generate_building_dossier, write_dossier
            from graph import generate_mermaid
            mc = generate_mermaid(clusters, chains, rows)
            html_content = generate_building_dossier(
                street, city, state, args.zip,
                rows, clusters, chains, mermaid_code=mc)
            path = write_dossier(html_content, street, unit="BUILDING")
            print(f"\n  📄 Building Dossier: {path}")
        print("=" * 64)
        return

    rows = []
    for unit in (units or [""]):
        label = (f"{street} Unit {unit}, {city}, {state} {args.zip}"
                 if unit else f"{street}, {city}, {state} {args.zip}")
        target = normalize_address(label)
        current, possible, former, building_only = report_unit(
            people, target, do_delta=True, do_osint=args.osint, do_forward=args.forward,
            do_enrich=args.enrich)
        if args.llm:
            from summarize import summarize
            text = summarize(label, current, former, building_only)
            if text:
                print(f"    Summary: {text}")
                
        if args.dossier:
            from dossier import generate_unit_dossier, write_dossier
            import history
            delta = {}
            try:
                hs = _hist_street(street)
                delta = history.get_delta(hs, args.zip, unit, current)
            except Exception:
                pass
            enrichment = {}
            if args.enrich:
                from enrich import enrich_person, format_enrichment
                for c in current[:3]:
                    data = enrich_person(c.name, city=city, state=state)
                    lines = format_enrichment(c.name, data)
                    if lines:
                        enrichment[c.name] = lines
            html_content = generate_unit_dossier(
                street, unit, city, state, args.zip,
                current, possible, former,
                delta=delta, enrichment=enrichment,
                owner_lines=owner_context(people, target, do_unmask=args.enrich))
            path = write_dossier(html_content, street, unit)
            print(f"    📄 Dossier: {path}")
            
        if args.monitor:
            import history
            hs = _hist_street(street)
            delta = history.get_delta(hs, args.zip, unit, current)
            history.record_run(hs, args.zip, unit, current, possible, former)
            changes = [k for k, v in delta.items() if v in ("[NEW]", "[REMOVED]")]
            if changes:
                print(f"    ⚠️  CHANGES DETECTED: {len(changes)} change(s)")
                for name, tag in delta.items():
                    if tag in ("[NEW]", "[REMOVED]"):
                        print(f"       {tag} {name}")
                if args.dossier:
                    print(f"    📄 Change report saved to reports/ directory.")
            else:
                print(f"    ✓ No changes since last run.")
                
        if args.out:
            from export import build_row
            rows.append(build_row(unit, current, possible, former, building_only,
                                  owner="; ".join(owner_context(people, target, do_unmask=args.enrich))))
    print("=" * 64)

    if args.out and rows:
        from export import write_results
        path = write_results(args.out, rows)
        print(f"\nWrote {len(rows)} row(s) to: {path}")


if __name__ == "__main__":
    main()
