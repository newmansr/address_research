"""
enrich.py — person enrichment (Feature 4).

Given an IDENTIFIED resident's name, pull their footprint from the independent open-data APIs that
are queryable BY NAME (the complement to `--osint`'s web/LLM search). All free/open:

  - FEC by name          : campaign contributions -> occupation, employer, recent activity, amounts.
  - DC property by name  : DC parcels OWNED by this person (do they own this unit, or others?).
  - OpenCorporates       : companies they're an officer of (best-effort; free tier is limited/gated).

Reuses `apis.py`'s HTTP + endpoint helpers. Name matching is first+last via `name_key` so we don't
mix up different people who merely share a surname.
"""

from __future__ import annotations

import os
import urllib.parse
from duckduckgo_search import DDGS

from resident_core import name_key
from apis import (_json_get, FEC_BASE, _resolve_itspe_url, _flip_lastfirst, _dc_owner_display,
                  _is_entity)


def enrich_fec(name, api_key="", max_pages=2, proxy="", state="") -> list[dict]:
    """This person's FEC individual contributions: occupation/employer/date/amount (most recent).
    `state` (the resident's state) narrows same-named donors - name-only search is inherently fuzzy."""
    key = api_key or os.environ.get("FEC_API_KEY") or "DEMO_KEY"
    want = name_key(name)
    # Query with first+last only: FEC stores 'LAST, FIRST MIDDLE' and matches a middle initial poorly.
    toks = name.split()
    query_name = f"{toks[0]} {toks[-1]}" if len(toks) >= 2 else name
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        params = {"api_key": key, "contributor_name": query_name, "is_individual": "true",
                  "per_page": "50", "page": str(page), "sort": "-contribution_receipt_date"}
        if state:
            params["contributor_state"] = state
        _, d = _json_get(FEC_BASE + "?" + urllib.parse.urlencode(params), timeout=60, proxy=proxy)
        if not d or not d.get("results"):
            break
        for c in d["results"]:
            if name_key(_flip_lastfirst(c.get("contributor_name") or "")) != want:
                continue    # keep only same first+last, not everyone who shares a surname
            occ = (c.get("contributor_occupation") or "").strip().title()
            emp = (c.get("contributor_employer") or "").strip().title()
            k = (occ, emp)
            if k in seen:
                continue
            seen.add(k)
            out.append({"occupation": occ, "employer": emp,
                        "date": (c.get("contribution_receipt_date") or "")[:10],
                        "amount": c.get("contribution_receipt_amount"),
                        "city": c.get("contributor_city"), "state": c.get("contributor_state")})
            if len(out) >= 5:
                return out
        if page >= (d.get("pagination", {}).get("pages") or 1):
            break
    return out


def enrich_dc_property(name, proxy="") -> list[dict]:
    """DC parcels OWNED by this person (they may own the unit, or other DC property)."""
    toks = [t for t in name.upper().replace(".", " ").split() if len(t) > 1]
    if len(toks) < 2:
        return []
    first, last = toks[0], toks[-1]
    base = _resolve_itspe_url(proxy)
    where = f"OWNERNAME LIKE '%{last}%{first}%' OR OWNERNAME LIKE '%{last} {first}%'"
    params = {"where": where, "outFields": "OWNERNAME,PREMISEADD,UNITNUMBER",
              "f": "json", "resultRecordCount": "50"}
    _, d = _json_get(base + "/query?" + urllib.parse.urlencode(params), timeout=60, proxy=proxy)
    if not d:
        return []
    want = name_key(name)
    out, seen = [], set()
    for f in d.get("features", []):
        a = f.get("attributes", {})
        owner = a.get("OWNERNAME") or ""
        if _is_entity(owner) or name_key(_dc_owner_display(owner, False)) != want:
            continue
        u = (a.get("UNITNUMBER") or "").strip()
        disp = (a.get("PREMISEADD") or "").strip() + (f" #{u}" if u else "")
        if disp and disp not in seen:
            seen.add(disp)
            out.append({"address": disp})
    return out


def enrich_opencorporates(name, proxy="") -> list[dict]:
    """Companies this person is a registered officer of (best-effort; free tier limited/token-gated)."""
    url = "https://api.opencorporates.com/v0.4/officers/search?" + urllib.parse.urlencode({"q": name})
    _, d = _json_get(url, timeout=30, proxy=proxy)
    if not d:
        return []
    want = name_key(name)
    out, seen = [], set()
    for o in (d.get("results", {}) or {}).get("officers", [])[:25]:
        off = o.get("officer", {})
        if name_key(off.get("name") or "") != want:
            continue
        comp = (off.get("company") or {}).get("name")
        if comp and comp not in seen:
            seen.add(comp)
            out.append({"company": comp, "position": off.get("position")})
    return out[:5]


def enrich_court(name, proxy="") -> list[dict]:
    """CourtListener free RECAP dockets via DDGS."""
    out = []
    try:
        results = DDGS(proxies=proxy if proxy else None).text(f'site:courtlistener.com "{name}"', max_results=10)
        want = name_key(name)
        for r in results:
            text = (r.get("title", "") + " " + r.get("body", "")).upper()
            if name.upper() not in text and want not in text:
                continue
            out.append({
                "case_name": r.get("title"),
                "court": "CourtListener",
                "date_filed": "",
                "docket_url": r.get("href")
            })
            if len(out) >= 5:
                break
    except Exception:
        pass
    return out


def enrich_dc_court(name, proxy="") -> list[dict]:
    """DC Superior Court case search via DDGS."""
    out = []
    try:
        results = DDGS(proxies=proxy if proxy else None).text(f'site:dccourts.gov "{name}"', max_results=10)
        want = name_key(name)
        for r in results:
            text = (r.get("title", "") + " " + r.get("body", "")).upper()
            if name.upper() not in text and want not in text:
                continue
            out.append({
                "case_info": r.get("title"),
                "url": r.get("href")
            })
            if len(out) >= 3:
                break
    except Exception:
        pass
    return out


def enrich_socials(name, city="", state="", proxy="") -> list[dict]:
    """
    Feature 2: Social Media & Digital Footprinting.
    Search for LinkedIn, Twitter, and GitHub profiles via DuckDuckGo.
    """
    out = []
    try:
        from duckduckgo_search import DDGS
        loc = f'"{city}" "{state}"' if city and state else ""
        
        # Search LinkedIn
        li_res = DDGS(proxies=proxy if proxy else None).text(f'site:linkedin.com/in/ "{name}" {loc}', max_results=2)
        for r in li_res:
            out.append({"platform": "LinkedIn", "url": r.get("href"), "snippet": r.get("body", "")})
            
        # Search Twitter
        tw_res = DDGS(proxies=proxy if proxy else None).text(f'site:twitter.com "{name}" {loc}', max_results=2)
        for r in tw_res:
            if "/status/" not in r.get("href", ""):  # Avoid individual tweets, aim for profiles
                out.append({"platform": "Twitter", "url": r.get("href"), "snippet": r.get("body", "")})
                
        # Search GitHub
        gh_res = DDGS(proxies=proxy if proxy else None).text(f'site:github.com "{name}" -site:github.com/issues', max_results=2)
        for r in gh_res:
             if len(r.get("href", "").split("/")) == 4: # e.g. https://github.com/username
                 out.append({"platform": "GitHub", "url": r.get("href"), "snippet": r.get("body", "")})
    except Exception:
        pass
    return out


def enrich_documents(name, city="", state="", proxy="") -> list[dict]:
    """
    Feature 6: Nationwide Document OSINT.
    Searches for public PDFs (Resumes, Board Minutes, Legal filings) mentioning the target.
    """
    out = []
    try:
        from duckduckgo_search import DDGS
        loc = f'"{city}" "{state}"' if city and state else ""
        
        # Search for PDFs
        pdf_res = DDGS(proxies=proxy if proxy else None).text(f'"{name}" {loc} ext:pdf', max_results=3)
        for r in pdf_res:
            out.append({"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body", "")})
    except Exception:
        pass
    return out


def enrich_person(name, city="", state="", api_key="", proxy="") -> dict:
    """All name-based enrichment for one person. Each sub-source degrades to [] on failure."""
    return {
        "fec": enrich_fec(name, api_key=api_key, proxy=proxy, state=state),
        "dc_property": enrich_dc_property(name, proxy=proxy),
        "opencorporates": enrich_opencorporates(name, proxy=proxy),
        "court": enrich_court(name, proxy=proxy),
        "dc_court": enrich_dc_court(name, proxy=proxy),
        "socials": enrich_socials(name, city, state, proxy=proxy),
        "documents": enrich_documents(name, city, state, proxy=proxy)
    }


def format_enrichment(name: str, data: dict) -> list[str]:
    """Format the enrichment results into a list of display lines."""
    lines = []
    for e in data.get("fec", []):
        role = ", ".join(x for x in [e.get("occupation"), e.get("employer")] if x)
        when = f" (as of {e['date']})" if e.get("date") else ""
        if role:
            lines.append(f"FEC: {role}{when}")
    props = data.get("dc_property", [])
    if props:
        lines.append("Owns DC property: " + "; ".join(p["address"] for p in props[:3]))
    for c in data.get("opencorporates", []):
        pos = f" ({c['position']})" if c.get("position") else ""
        lines.append(f"Officer of {c['company']}{pos}")
    for c in data.get("court", []):
        lines.append(f"Court: {c.get('case_name')} ({c.get('docket_url', '')})")
    for c in data.get("dc_court", []):
        lines.append(f"DC Court: {c.get('case_info')} ({c.get('url', '')})")
    for s in data.get("socials", []):
        lines.append(f"Social [{s['platform']}]: {s['url']} - {s['snippet'][:80]}...")
    for d in data.get("documents", []):
        lines.append(f"Public PDF: {d['title']} ({d['url']})")
    return lines


def unmask_dc_llc(llc_name: str, proxy="") -> list[dict]:
    """
    Feature 1: Corporate Unmasking.
    Given an LLC or entity name (often the owner of a DC property), query the DC Corporate
    Registration API to find the Registered Agent and address.
    """
    # Clean up common LLC suffixes for better LIKE matching
    import re
    clean_name = re.sub(r'\b(LLC|L\.L\.C\.|INC|INCORPORATED|CORP|CORPORATION)\b', '', llc_name, flags=re.IGNORECASE).strip()
    clean_name = re.sub(r'[.,]', '', clean_name).strip()
    
    if len(clean_name) < 4:
        return []
        
    url = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Business_Licensing_and_Grants_WebMercator/MapServer/0/query"
    where = f"BUSINESS_NAME LIKE '%{clean_name.upper()}%'"
    params = {
        "where": where,
        "outFields": "BUSINESS_NAME,ENTITY_STATUS,RA_NAME,RA_ADDRESS1",
        "f": "json",
        "resultRecordCount": "5"
    }
    
    out = []
    try:
        from apis import _json_get
        import urllib.parse
        full_url = url + "?" + urllib.parse.urlencode(params)
        _, d = _json_get(full_url, timeout=30, proxy=proxy)
        for f in (d or {}).get("features", []):
            attr = f.get("attributes", {})
            bname = attr.get("BUSINESS_NAME")
            ra_name = attr.get("RA_NAME")
            if bname and ra_name:
                out.append({
                    "business_name": bname,
                    "status": attr.get("ENTITY_STATUS"),
                    "registered_agent": ra_name,
                    "agent_address": attr.get("RA_ADDRESS1")
                })
    except Exception:
        pass
    return out
