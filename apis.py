"""
apis.py — INDEPENDENT open-data sources (free JSON APIs, no browser, no Cloudflare).

These break the people-search "resold data" problem: unlike the six aggregator sites (four of
which share one backend), these are genuinely independent lineages, so their agreement is real
corroboration and each can catch a mover the aggregators missed.

  - OpenFEC        : FEC individual campaign contributions. Name + self-reported address + DATE +
                     occupation/employer. api.open.fec.gov (DEMO_KEY works; get a free key for more).
  - DCProperty     : DC Integrated Tax System (opendata.dc.gov) - the OWNER of record per parcel/unit.
                     Owner-occupant => a real residency signal; entity/absentee owner => the unit is a
                     rental and we surface the landlord as context (owner != resident).
  - DCVacant       : DC Vacant and Blighted Building Addresses. Marks the property as VACANT.
  - SECEdgar       : SEC EDGAR full-text search. Filer names tied to an address. Very low yield for
                     residential (addresses are usually business); a tie-breaker at best.

All are public/open data. Records come back as `Person` objects (fetch + parse combined, since the
payloads are JSON), tagged with their own source so scoring treats each as a distinct evidence family.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse

from resident_core import Person, normalize_address, match_address, MatchLevel
from sources import archive_raw

_UA = "address-research/1.0 (personal research; contact via app)"


def _http_get(url: str, headers: dict | None = None, timeout: int = 45, proxy: str = ""):
    """GET returning (status, text). Tries curl_cffi (best TLS) then requests; (0,'') on failure."""
    headers = headers or {"User-Agent": _UA}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, headers=headers, impersonate="chrome", timeout=timeout, proxies=proxies)
        if r.status_code and r.text:
            return r.status_code, r.text
    except Exception:
        pass
    try:
        import requests
        r = requests.get(url, headers=headers, timeout=timeout, proxies=proxies)
        return r.status_code, r.text
    except Exception:
        return 0, ""


def _json_get(url: str, headers=None, timeout=45, proxy=""):
    st, body = _http_get(url, headers=headers, timeout=timeout, proxy=proxy)
    if st != 200 or not body:
        return st, None
    try:
        return st, json.loads(body)
    except ValueError:
        return st, None


def _titlecase(s: str) -> str:
    return " ".join(w.capitalize() for w in s.split())


# ── name normalization for "LAST, FIRST MIDDLE TITLE" formats (FEC, EDGAR) ────

_NAME_TITLES = {"MR", "MRS", "MS", "MISS", "MX", "DR", "PHD", "MD", "DDS", "ESQ", "HON", "REV",
                "PROF", "JR", "SR", "II", "III", "IV", "V", "DVM", "CPA", "RN", "JD"}


def _flip_lastfirst(raw: str) -> str:
    """'RICHARDSON, DAVID H. DR. PHD' -> 'David H Richardson'; drops honorifics/suffixes."""
    raw = raw.strip()
    if not raw:
        return ""
    if "," in raw:
        last, rest = raw.split(",", 1)
    else:
        toks = [t for t in re.sub(r"[.]", " ", raw).split() if t.upper() not in _NAME_TITLES]
        return _titlecase(" ".join(toks))
    rest_toks = [t for t in re.sub(r"[.]", " ", rest).split() if t.upper() not in _NAME_TITLES]
    last_toks = [t for t in re.sub(r"[.]", " ", last).split() if t.upper() not in _NAME_TITLES]
    return _titlecase(" ".join(rest_toks + last_toks))


# ── OpenFEC (individual campaign contributions) ──────────────────────────────

FEC_BASE = "https://api.open.fec.gov/v1/schedules/schedule_a/"


def fetch_fec(street, city, state, zip_code, unit="", proxy="", archive=True,
              api_key="", zip9="", max_pages=5) -> list[Person]:
    """Individual FEC donors whose self-reported address matches the target (indep., dated).

    The API can't filter by street, so we query by contributor ZIP (use the 9-digit `zip9` for a
    dense urban ZIP or you may not reach the building), page most-recent-first, and match street+unit
    client-side. Each donor carries a contribution date (recency) and occupation/employer (identity).
    DEMO_KEY is rate-limited (~30/hr); pass a free api_key or set FEC_API_KEY for real use.
    """
    key = api_key or os.environ.get("FEC_API_KEY") or "DEMO_KEY"
    czip = re.sub(r"\D", "", (zip9 or zip_code or ""))
    czip = czip[:9] if len(czip) >= 9 else czip[:5]
    if not czip:
        return []
    target = _target(street, city, state, zip_code, unit)

    people: list[Person] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        params = {"api_key": key, "contributor_zip": czip, "is_individual": "true",
                  "per_page": "100", "page": str(page), "sort": "-contribution_receipt_date"}
        st, d = _json_get(FEC_BASE + "?" + urllib.parse.urlencode(params), timeout=60, proxy=proxy)
        if not d:
            break
        results = d.get("results", [])
        if not results:
            break
        if archive and page == 1:
            archive_raw("OpenFEC", street, unit, json.dumps(d)[:200000], ext="json")
        for c in results:
            cand = normalize_address(", ".join(x for x in [
                c.get("contributor_street_1") or "", c.get("contributor_city") or "",
                f"{c.get('contributor_state') or ''} {c.get('contributor_zip') or ''}"] if x.strip()))
            if match_address(target, cand) == MatchLevel.NONE:
                continue
            name = _flip_lastfirst(c.get("contributor_name") or "")
            if len(name.split()) < 2 or name.upper() in seen:
                continue
            seen.add(name.upper())
            occ = (c.get("contributor_occupation") or "").strip().title()
            emp = (c.get("contributor_employer") or "").strip().title()
            date = (c.get("contribution_receipt_date") or "")[:10]
            bits = [b for b in [occ, (f"at {emp}" if emp and emp.upper() not in
                                      ("N/A", "None", "Self", "Retired", "") else "")] if b]
            note = "FEC donor: " + (", ".join(bits) if bits else "individual contributor")
            people.append(Person(name=name, source="OpenFEC", current_address=cand,
                                 current_since=(date[:4] if date[:4].isdigit() else ""), note=note))
        if page >= (d.get("pagination", {}).get("pages") or 1):
            break
    return people


# ── DC property owner (Integrated Tax System Public Extract) ──────────────────

_ITSPE_FALLBACK = ("https://services.arcgis.com/neT9SoYxizqTHZPH/arcgis/rest/services/"
                   "OCFO_ITSPE_view_05212026/FeatureServer/53")
_ITSPE_SLUG = "DCGIS%3A%3Aintegrated-tax-system-public-extract"
_itspe_url = None

_ENTITY_WORDS = {"LLC", "LLLP", "PLLC", "INC", "CORP", "CO", "LP", "LLP", "LTD", "TRUST", "TR",
                 "FOUNDATION", "ASSOCIATION", "ASSOC", "CONDOMINIUM", "CONDO", "PARTNERS",
                 "PARTNERSHIP", "COMPANY", "HOLDINGS", "PROPERTIES", "PROPERTY", "TENANTS",
                 "APARTMENTS", "INVESTMENTS", "GROUP", "BANK", "NA", "CHURCH", "REIT", "FUND",
                 "VENTURES", "REALTY", "MANAGEMENT", "ENTERPRISES", "DEVELOPMENT"}


_VACANT_SLUG = "DCGIS%3A%3Avacant-and-blighted-building-addresses"
_vacant_url = None

def _resolve_vacant_url(proxy="") -> str:
    global _vacant_url
    if _vacant_url:
        return _vacant_url
    _, d = _json_get(f"https://hub.arcgis.com/api/v3/datasets?filter%5Bslug%5D={_VACANT_SLUG}",
                     timeout=45, proxy=proxy)
    try:
        _vacant_url = d["data"][0]["attributes"]["url"]
    except (TypeError, KeyError, IndexError):
        _vacant_url = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Property_and_Land_WebMercator/MapServer/60"
    return _vacant_url

def fetch_dc_vacant(street, city, state, zip_code, unit="", proxy="", archive=True) -> list[Person]:
    """DC Vacant and Blighted property registry. Overrides current residents if vacant."""
    base = _resolve_vacant_url(proxy)
    street_up = re.sub(r"\s+", " ", street.strip().upper()).replace("'", "''")
    params = {"where": f"PREMISEADDRESS LIKE '{street_up}%'",
              "outFields": "PREMISEADDRESS",
              "f": "json"}
    st, d = _json_get(base + "/query?" + urllib.parse.urlencode(params), timeout=60, proxy=proxy)
    if not d:
        return []
    feats = d.get("features", [])
    if archive and feats:
        archive_raw("DCVacant", street, unit, json.dumps(d)[:200000], ext="json")
    
    target = _target(street, city, state, zip_code, unit)
    people = []
    seen = set()
    for f in feats:
        a = f.get("attributes", {})
        prem = (a.get("PREMISEADDRESS") or "").strip()
        prem_addr = normalize_address(prem)
        
        # Must match building, and unit (if provided) must not conflict.
        # DC Vacant list usually doesn't have units, but if it does, it must match.
        if target.street_key == prem_addr.street_key:
            if unit and prem_addr.has_unit() and prem_addr.unit_key != target.unit_key:
                continue
            name = "VACANT PROPERTY"
            if name not in seen:
                seen.add(name)
                people.append(Person(name=name, source="DCVacant",
                                     current_address=prem_addr,
                                     note="DC Gov lists this property as Vacant/Blighted."))
    return people


def fetch_dc_permits(street, city, state, zip_code, unit="", proxy="", archive=True) -> list[Person]:
    """DC Building Permits (Last 30 Days). Flags recent construction."""
    # DC ArcGIS layer 4 is 'Building Permits - Last 30 Days'
    base = "https://maps2.dcgis.dc.gov/dcgis/rest/services/FEEDS/DCRA/MapServer/4"
    street_up = re.sub(r"\s+", " ", street.strip().upper()).replace("'", "''")
    params = {"where": f"FULL_ADDRESS LIKE '{street_up}%'",
              "outFields": "FULL_ADDRESS,ISSUE_DATE,PERMIT_TYPE_NAME,DESC_OF_WORK",
              "f": "json"}
    st, d = _json_get(base + "/query?" + urllib.parse.urlencode(params), timeout=60, proxy=proxy)
    if not d:
        return []
    feats = d.get("features", [])
    if archive and feats:
        archive_raw("DCPermit", street, unit, json.dumps(d)[:200000], ext="json")
    
    target = _target(street, city, state, zip_code, unit)
    people = []
    seen = set()
    for f in feats:
        a = f.get("attributes", {})
        addr_str = (a.get("FULL_ADDRESS") or "").strip()
        prem_addr = normalize_address(addr_str)
        
        if target.street_key == prem_addr.street_key:
            if unit and prem_addr.has_unit() and prem_addr.unit_key != target.unit_key:
                continue
            
            p_type = a.get("PERMIT_TYPE_NAME") or "Permit"
            desc = a.get("DESC_OF_WORK") or ""
            # Issue date is a timestamp in ms
            issue_ts = a.get("ISSUE_DATE")
            date_str = ""
            if issue_ts:
                from datetime import datetime
                try:
                    date_str = datetime.fromtimestamp(issue_ts / 1000.0).strftime("%Y-%m-%d")
                except Exception:
                    pass
            
            note = f"Active Construction Permit ({date_str}): {p_type}. {desc}"[:200]
            name = "CONSTRUCTION / PERMIT"
            key = (name, note)
            if key not in seen:
                seen.add(key)
                people.append(Person(name=name, source="DCPropertyOwner", # Treat as context
                                     current_address=prem_addr,
                                     note=note))
    return people


def _is_entity(name: str) -> bool:
    """True if the owner name looks like an organization, not a person."""
    toks = re.sub(r"[.,]", " ", name.upper()).split()
    return any(t in _ENTITY_WORDS for t in toks)


def _dc_owner_display(owner: str, entity: bool) -> str:
    """DC OCFO owner names are 'LASTNAME FIRSTNAME MIDDLE'. Reorder people to 'First Middle Last'
    so their name_key matches the aggregators; leave entity names as-is."""
    if entity:
        return owner
    if "," in owner:
        return _flip_lastfirst(owner)
    toks = owner.split()
    if len(toks) >= 2:
        return _titlecase(" ".join(toks[1:] + toks[:1]))   # LAST FIRST MID -> FIRST MID LAST
    return _titlecase(owner)


def _resolve_itspe_url(proxy="") -> str:
    """Resolve the current ITSPE FeatureServer URL via ArcGIS Hub (its layer name is date-versioned),
    falling back to a known-good URL if the Hub lookup fails."""
    global _itspe_url
    if _itspe_url:
        return _itspe_url
    _, d = _json_get(f"https://hub.arcgis.com/api/v3/datasets?filter%5Bslug%5D={_ITSPE_SLUG}",
                     timeout=45, proxy=proxy)
    try:
        url = d["data"][0]["attributes"]["url"]
        _itspe_url = url or _ITSPE_FALLBACK
    except (TypeError, KeyError, IndexError):
        _itspe_url = _ITSPE_FALLBACK
    return _itspe_url


def fetch_dc_property(street, city, state, zip_code, unit="", proxy="", archive=True) -> list[Person]:
    """DC owner of record for the parcel/unit (authoritative, independent).

    Owner-occupant (a PERSON whose mailing address is the unit) is a real residency signal ->
    source 'DCProperty'. An entity or absentee owner means the unit is a RENTAL and the owner is
    NOT a resident -> source 'DCPropertyOwner', surfaced as context (never ranked as a resident).
    """
    base = _resolve_itspe_url(proxy)
    street_up = re.sub(r"\s+", " ", street.strip().upper()).replace("'", "''")
    params = {"where": f"PREMISEADD LIKE '{street_up}%'",
              "outFields": "OWNERNAME,PREMISEADD,UNITNUMBER,ADDRESS1,ADDRESS2,CITYSTZIP,SSL,ASSESSMENT,SALEPRICE,SALEDATE",
              "f": "json", "resultRecordCount": "400"}
    st, d = _json_get(base + "/query?" + urllib.parse.urlencode(params), timeout=60, proxy=proxy)
    if not d:
        return []
    feats = d.get("features", [])
    if archive and feats:
        archive_raw("DCProperty", street, unit, json.dumps(d)[:200000], ext="json")
    target = _target(street, city, state, zip_code, unit)

    people: list[Person] = []
    seen: set = set()
    for f in feats:
        a = f.get("attributes", {})
        owner = (a.get("OWNERNAME") or "").strip()
        if not owner:
            continue
        rec_unit = (a.get("UNITNUMBER") or "").strip()
        prem = (a.get("PREMISEADD") or "").strip()
        prem_addr = normalize_address(prem + (f" Unit {rec_unit}" if rec_unit else ""))
        if match_address(target, prem_addr) == MatchLevel.NONE:
            continue
        dedup = (owner.upper(), rec_unit)
        if dedup in seen:
            continue
        seen.add(dedup)

        mail_city = (a.get("CITYSTZIP") or "elsewhere").strip()
        mail_addr = normalize_address(" ".join(x for x in [a.get("ADDRESS1"), a.get("ADDRESS2"),
                                                           a.get("CITYSTZIP")] if x))
        entity = _is_entity(owner)
        owner_occupied = (not entity) and match_address(target, mail_addr) != MatchLevel.NONE
        
        # Feature 4: Property Intelligence
        assessment = a.get("ASSESSMENT", 0)
        sale_price = a.get("SALEPRICE", 0)
        sale_ts = a.get("SALEDATE")
        stats = []
        if assessment:
            stats.append(f"Assessed: ${assessment:,.0f}")
        if sale_price:
            stats.append(f"Last Sale: ${sale_price:,.0f}")
        if sale_ts:
            from datetime import datetime
            try:
                stats.append(f"Sale Date: {datetime.fromtimestamp(sale_ts / 1000.0).strftime('%Y-%m-%d')}")
            except Exception:
                pass
        stats_str = f" | {', '.join(stats)}" if stats else ""

        if owner_occupied:
            people.append(Person(name=_dc_owner_display(owner, False), source="DCProperty",
                                 current_address=prem_addr,
                                 note=f"owner-occupant of record (DC tax records){stats_str}"))
        else:
            kind = "entity/rental-owned" if entity else "absentee owner"
            people.append(Person(name=_dc_owner_display(owner, entity), source="DCPropertyOwner",
                                 current_address=prem_addr,
                                 note=f"owner of record ({kind}); mails to {mail_city} "
                                      f"- owner is not the resident{stats_str}"))
    return people


# ── SEC EDGAR full-text search ────────────────────────────────────────────────

def fetch_sec_edgar(street, city, state, zip_code, unit="", proxy="", archive=True,
                    max_hits=10) -> list[Person]:
    """SEC filers whose filing text mentions the address. Independent but very low yield for
    residential addresses (filing addresses are usually business) - a tie-breaker only."""
    q = f'"{street}"' + (f' "{unit}"' if unit else "")
    url = "https://efts.sec.gov/LATEST/search-index?q=" + urllib.parse.quote(q)
    st, d = _json_get(url, headers={"User-Agent": _UA}, timeout=30, proxy=proxy)
    if not d:
        return []
    hits = d.get("hits", {}).get("hits", [])
    if archive and hits:
        archive_raw("SECEdgar", street, unit, json.dumps(d)[:200000], ext="json")
    building = _target(street, city, state, zip_code, "")

    people: list[Person] = []
    seen: set = set()
    for h in hits[:max_hits]:
        src = h.get("_source", {})
        form = src.get("root_form") or (src.get("file_type") or "")
        date = (src.get("file_date") or "")[:10]
        for nm in (src.get("display_names") or []):
            clean = re.sub(r"\s*\(.*?\)\s*", "", nm).strip()   # strip "(CIK ...)" suffix
            if "," in clean:
                clean = _flip_lastfirst(clean)
            if len(clean.split()) < 2 or _is_entity(clean) or clean.upper() in seen:
                continue
            seen.add(clean.upper())
            people.append(Person(name=clean, source="SECEdgar", current_address=building,
                                 current_since=(date[:4] if date[:4].isdigit() else ""),
                                 note=f"SEC {form} filing {date}".strip()))
    return people


def _target(street, city, state, zip_code, unit):
    label = (f"{street} Unit {unit}, {city}, {state} {zip_code}" if unit
             else f"{street}, {city}, {state} {zip_code}")
    return normalize_address(label)


def fetch_dc_bbl(street, city, state, zip_code, unit="", proxy="", archive=True) -> list[Person]:
    """DC Business Licenses. Identifies commercial units/businesses."""
    base = "https://maps2.dcgis.dc.gov/dcgis/rest/services/FEEDS/DCRA/MapServer/0"
    street_up = re.sub(r"\s+", " ", street.strip().upper()).replace("'", "''")
    params = {"where": f"FULL_ADDRESS LIKE '{street_up}%'",
              "outFields": "ENTITYNAME,PREMISEADDRESS,BUSINESSACTIVITY,LICENSESTATUS",
              "f": "json"}
    st, d = _json_get(base + "/query?" + urllib.parse.urlencode(params), timeout=60, proxy=proxy)
    if not d:
        return []
    feats = d.get("features", [])
    if archive and feats:
        archive_raw("DCBusinessLicense", street, unit, json.dumps(d)[:200000], ext="json")
    
    target = _target(street, city, state, zip_code, unit)
    people = []
    seen = set()
    for f in feats:
        a = f.get("attributes", {})
        addr_str = (a.get("PREMISEADDRESS") or "").strip()
        prem_addr = normalize_address(addr_str)
        
        if target.street_key == prem_addr.street_key:
            if unit and prem_addr.has_unit() and prem_addr.unit_key != target.unit_key:
                continue
            
            name = (a.get("ENTITYNAME") or "BUSINESS").strip()
            activity = (a.get("BUSINESSACTIVITY") or "").strip()
            status = (a.get("LICENSESTATUS") or "").strip()
            note = f"Business License: {activity}" + (f" ({status})" if status else "")
            
            key = (name.upper(), note)
            if key not in seen:
                seen.add(key)
                people.append(Person(name=name, source="DCBusinessLicense",
                                     current_address=prem_addr,
                                     note=note[:200]))
    return people


def fetch_dc_311(street, city, state, zip_code, unit="", proxy="", archive=True) -> list[Person]:
    """DC 311 Service Requests. Context for building condition."""
    base = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/ServiceRequests/MapServer/8"
    street_up = re.sub(r"\s+", " ", street.strip().upper()).replace("'", "''")
    params = {"where": f"STREETADDRESS LIKE '{street_up}%'",
              "outFields": "STREETADDRESS,SERVICETYPEDESCRIPTION,SERVICEORDERDATE,SERVICECALLCOUNT",
              "f": "json"}
    st, d = _json_get(base + "/query?" + urllib.parse.urlencode(params), timeout=60, proxy=proxy)
    if not d:
        return []
    feats = d.get("features", [])
    if archive and feats:
        archive_raw("DC311", street, unit, json.dumps(d)[:200000], ext="json")
    
    target = _target(street, city, state, zip_code, unit)
    people = []
    seen = set()
    for f in feats:
        a = f.get("attributes", {})
        addr_str = (a.get("STREETADDRESS") or "").strip()
        prem_addr = normalize_address(addr_str)
        
        if target.street_key == prem_addr.street_key:
            if unit and prem_addr.has_unit() and prem_addr.unit_key != target.unit_key:
                continue
            
            desc = a.get("SERVICETYPEDESCRIPTION") or ""
            issue_ts = a.get("SERVICEORDERDATE")
            date_str = ""
            if issue_ts:
                from datetime import datetime
                try:
                    date_str = datetime.fromtimestamp(issue_ts / 1000.0).strftime("%Y-%m-%d")
                except Exception:
                    pass
            
            note = f"{desc} ({date_str})"
            name = "311 REQUEST"
            key = (name, note)
            if key not in seen:
                seen.add(key)
                people.append(Person(name=name, source="DC311",
                                     current_address=prem_addr,
                                     note=note[:200]))
    return people


def fetch_dc_crime(street, city, state, zip_code, unit="", proxy="", archive=True) -> list[Person]:
    """DC Crime Incidents (Last 30 Days). Context."""
    base = "https://maps2.dcgis.dc.gov/dcgis/rest/services/FEEDS/MPD/MapServer/8"
    
    street_clean = re.sub(r"\s+", " ", street.strip().upper()).replace("'", "''")
    num_match = re.search(r"^\s*(\d+)\s+(.*)", street_clean)
    if num_match:
        num = int(num_match.group(1))
        hundreds = (num // 100) * 100
        street_name = num_match.group(2).strip()
        block_query = f"{hundreds} BLOCK %{street_name}%"
    else:
        block_query = f"%{street_clean}%"

    params = {"where": f"BLOCK LIKE '{block_query}'",
              "outFields": "OFFENSE,REPORT_DAT,BLOCK",
              "f": "json"}
    st, d = _json_get(base + "/query?" + urllib.parse.urlencode(params), timeout=60, proxy=proxy)
    if not d:
        return []
    feats = d.get("features", [])
    if archive and feats:
        archive_raw("DCCrime", street, unit, json.dumps(d)[:200000], ext="json")
    
    target = _target(street, city, state, zip_code, unit)
    people = []
    seen = set()
    for f in feats:
        a = f.get("attributes", {})
        block_str = (a.get("BLOCK") or "").strip()
        
        offense = a.get("OFFENSE") or "Unknown Offense"
        issue_ts = a.get("REPORT_DAT")
        date_str = ""
        if issue_ts:
            from datetime import datetime
            try:
                date_str = datetime.fromtimestamp(issue_ts / 1000.0).strftime("%Y-%m-%d")
            except Exception:
                pass
        
        note = f"{offense} ({date_str}) at {block_str}"
        name = "CRIME INCIDENT"
        key = (name, note)
        if key not in seen:
            seen.add(key)
            people.append(Person(name=name, source="DCCrime",
                                 current_address=target,
                                 note=note[:200]))
    return people


# Registry — each fn takes (street, city, state, zip_code, unit=..., proxy=..., **opts).
def fetch_osm_context(street, city, state, zip_code, unit="", proxy="", archive=True) -> list[Person]:
    """
    Nationwide Building Intel via OpenStreetMap (Nominatim).
    Geocodes the address and returns the building type (e.g. house, apartments, office),
    neighborhood, and lat/lon. Works globally.
    """
    import urllib.parse
    target = _target(street, city, state, zip_code, unit)
    
    # Nominatim requires a descriptive User-Agent
    headers = {"User-Agent": "AddressResearchOSINT/1.0 (research project)"}
    q = f"{street}, {city}, {state} {zip_code}"
    params = {"q": q, "format": "json", "addressdetails": "1", "limit": "1"}
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    
    try:
        import urllib.request
        import json
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"    !! OSM Nominatim error: {e}")
        return []
        
    if not data:
        return []
        
    r = data[0]
    osm_type = r.get("type", "building").replace("_", " ").title()
    lat, lon = r.get("lat"), r.get("lon")
    addr = r.get("address", {})
    neighborhood = addr.get("neighbourhood") or addr.get("suburb") or addr.get("city_district") or ""
    
    note_parts = []
    if neighborhood:
        note_parts.append(f"Neighborhood: {neighborhood}")
    if lat and lon:
        note_parts.append(f"Coordinates: {lat[:8]}, {lon[:9]}")
    note_parts.append(f"OSM ID: {r.get('osm_type')}/{r.get('osm_id')}")
    
    note = f"Nationwide Building Type: {osm_type} | " + " | ".join(note_parts)
    
    # We return this as a context record
    return [Person(name=f"OSM GEODATA: {osm_type.upper()}", source="OSMContext", 
                   current_address=target, note=note)]

API_SOURCES = {
    "OpenFEC": fetch_fec,
    "DCProperty": fetch_dc_property,
    "DCVacant": fetch_dc_vacant,
    "DCPermit": fetch_dc_permits,
    "DCBusinessLicense": fetch_dc_bbl,
    "DC311": fetch_dc_311,
    "DCCrime": fetch_dc_crime,
    "OSMContext": fetch_osm_context,
    "SECEdgar": fetch_sec_edgar
}
