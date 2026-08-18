"""
parsers.py — per-source parsers that turn raw scraped text into Person records.

Each parser is intentionally narrow and defensive: people-search pages are noisy, so we anchor
on stable landmarks and bail gracefully (return []) when structure is missing.

Implemented: USPhoneBook, CyberBackgroundChecks, ThatsThem (JSON-LD), and the sister-site detail
parsers TruePeopleSearch / FastPeopleSearch (shared `_parse_ps_detail`). Dispatch via `PARSERS`.
"""

from __future__ import annotations

import json
import re

from resident_core import Person, normalize_address, since_to_year

_NAME_TOKEN = re.compile(r"[A-Z][A-Za-z.'\-]*$")


def _collapse(text: str) -> str:
    """Whitespace-agnostic: works on both clean (newline) and CDP (single-line) text."""
    return re.sub(r"\s+", " ", text).strip()


_AKA_STOP = (r"Current Address|Previous Address|Past Address|Phone Number|Phones\s*\||Lived here|"
             r"Related to|Possible Relatives|Possible Associates|Associated with|Email|"
             r"Used to live|Education|Background|VIEW DETAILS|View Report|$")


def _extract_aka(text: str) -> list[str]:
    """Alternate names from an 'Also Seen As' / 'Other observed names' / 'AKA' block.

    Names may be comma- or pipe-separated; keeps plausible 2-4 token personal names and drops
    boilerplate/counters. These reconcile maiden/alias variants of the SAME person downstream.
    """
    m = re.search(r"(?:Also Seen As|Also Known As|Also known as|Other observed names|"
                  r"Other Observed Names|AKAs?)\b(.*?)(?:" + _AKA_STOP + r")", text)
    if not m:
        return []
    chunk = re.sub(r"^\s*Includes all names[^.]*\.\s*", "", m.group(1))  # drop TPS boilerplate
    out: list[str] = []
    for part in re.split(r"[,|]", chunk):
        toks = part.split()
        cap = [t for t in toks if re.match(r"^[A-Z][A-Za-z.'\-]*$", t)]
        if 2 <= len(cap) <= 4 and len(cap) == len(toks):
            name = " ".join(cap)
            if name not in out:
                out.append(name)
    return out


def _clean_name(prefix: str) -> str:
    """Extract a person's name from the messy text immediately before their age.

    Handles the CDP single-line format where a short "First Last" heading precedes the
    full "First [Middle] Last" (e.g. '...DC Robert Kinsler Robert Albrecht Kinsler').
    """
    toks = prefix.split()
    name: list[str] = []
    for t in reversed(toks):           # take the trailing run of capitalized name tokens
        if _NAME_TOKEN.match(t):
            name.insert(0, t)
        else:
            break
    if len(name) >= 2 and len(name[0]) == 2 and name[0].isupper():
        name = name[1:]                # drop a stray trailing state code (e.g. "DC")
    for i in range(2, len(name)):      # drop duplicated "First Last" heading
        if name[i] == name[0]:
            name = name[i:]
            break
    return " ".join(name)


def parse_usphonebook(text: str) -> list[Person]:
    """Parse the 'People Living at <address>' block from a USPhoneBook page.

    Region sits between 'People Living at' and the 'Public Records' paid-results
    duplicates. Each person record ends with 'View Report'.
    """
    text = _collapse(text)
    start = text.find("People Living at")
    if start == -1:
        return []
    region = text[start:]
    for marker in ("Public Records", "Paid Results", "Neighbors"):
        idx = region.find(marker)
        if idx != -1:
            region = region[:idx]

    people: list[Person] = []
    for rec in region.split("View Report"):
        m = re.search(r",\s*Age\s*(\d+)", rec)
        if not m:
            continue
        name = _clean_name(rec[:m.start()])
        if not name:
            continue
        person = Person(name=name, age=int(m.group(1)), source="USPhoneBook")
        rest = rec[m.end():]

        cm = re.search(r"Lives at\s+(.*?)(?:Prior addresses:|Relatives:|$)", rest)
        if cm and cm.group(1).strip():
            person.current_address = normalize_address(cm.group(1))

        pm = re.search(r"Prior addresses:\s*(.*?)(?:Relatives:|$)", rest)
        if pm:
            for chunk in re.split(r"(?<=\d{4})\s+(?=\d+\s)", pm.group(1)):
                chunk = chunk.strip()
                if chunk:
                    person.prior_addresses.append(normalize_address(chunk))

        rm = re.search(r"Relatives:\s*(.*)$", rest)
        if rm:
            person.relatives = [r.strip() for r in rm.group(1).split(",") if r.strip()]

        people.append(person)
    return people


_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")


def _norm_phone(p: str) -> str:
    try:
        import phonenumbers
        from phonenumbers import carrier
        parsed = phonenumbers.parse(p, "US")
        if not phonenumbers.is_valid_number(parsed):
            return ""
        formatted = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
        c_name = carrier.name_for_number(parsed, "en")
        return f"{formatted} ({c_name})" if c_name else formatted
    except Exception:
        # Fallback if phonenumbers isn't installed or fails
        digits = re.sub(r"\D", "", p)
        if len(digits) >= 10:
            return f"+1-{digits[-10:-7]}-{digits[-7:-4]}-{digits[-4:]}"
        return ""


def _ranked_phones(block: str) -> list[str]:
    """Phones from a TPS/FPS detail block, best first: primary, then most-recent 'Last reported',
    with Inactive numbers dropped. Each number's metadata is read only up to the NEXT number, so a
    later 'Inactive'/date can't bleed onto the wrong phone."""
    matches = list(_PHONE_RE.finditer(block))
    entries = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
        tail = block[m.end():end]
        if re.search(r"\bInactive\b", tail):
            continue
        lr = re.search(r"Last reported\s+([A-Za-z]{3,9}\.?\s+\d{4})", tail)
        yr = since_to_year(lr.group(1)) if lr else None
        primary = 1 if re.search(r"Primary Phone", tail) else 0
        entries.append((primary, yr if yr is not None else -1.0, _norm_phone(m.group())))
    entries.sort(key=lambda e: (e[0], e[1]), reverse=True)
    out: list[str] = []
    for _, _, ph in entries:
        if ph and ph not in out:
            out.append(ph)
    return out


def parse_cyberbackgroundchecks(text: str) -> list[Person]:
    """Parse CyberBackgroundChecks address results.

    Each person record ends with 'VIEW DETAILS'. Layout:
      <Name> Age: NN Lives at <current> Used to live <priors...>
      Phones | (xxx) xxx-xxxx | ...   Related to | ... |
    """
    text = _collapse(text)
    start = text.find("results for")
    region = text[start:] if start != -1 else text

    people: list[Person] = []
    for rec in region.split("VIEW DETAILS"):
        m = re.search(r"Age:\s*(\d+)", rec)
        if not m:
            continue
        name = _clean_name(rec[:m.start()])
        if not name:
            continue
        person = Person(name=name, age=int(m.group(1)), source="CyberBackgroundChecks")
        rest = rec[m.end():]

        cm = re.search(
            r"Lives at\s+(.*?)(?:Used to live|Phones\s*\||Other observed|Related to|This is me|Remove My Record|\[\d+\] more|$)",
            rest)
        if cm and cm.group(1).strip():
            person.current_address = normalize_address(cm.group(1))

        um = re.search(r"Used to live\s+(.*?)(?:Phones\s*\||Other observed|Related to|\[\d+\] more|$)",
                       rest)
        if um:
            for chunk in re.findall(r".*?\[[^\]]*?(?:County|Parish|Borough)\]", um.group(1)):
                chunk = re.sub(r"\s*\[[^\]]*\]\s*$", "", chunk).strip()
                if chunk:
                    person.prior_addresses.append(normalize_address(chunk))

        pm = re.search(r"Phones\s*\|(.*?)(?:Related to|Associated with|Other observed|$)", rest)
        if pm:
            seen = []
            for ph in _PHONE_RE.findall(pm.group(1)):
                n = _norm_phone(ph)
                if n and n not in seen:
                    seen.append(n)
            person.phones = seen

        rm = re.search(r"Related to\s*\|(.*?)(?:Associated with|This is me|Other observed|"
                       r"VIEW DETAILS|$)", rest)
        if rm:
            person.relatives = [n.strip() for n in rm.group(1).split("|")
                                if 2 <= len(n.split()) <= 4
                                and re.match(r"^[A-Z][A-Za-z.'\- ]+$", n.strip())]

        person.aka = _extract_aka(rest)
        people.append(person)
    return people


def _iter_jsonld_persons(html: str):
    """Yield Person JSON-LD objects from a ThatsThem page."""
    for m in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL
    ):
        try:
            data = json.loads(m.group(1))
        except (ValueError, json.JSONDecodeError):
            continue
        graph = data.get("@graph", []) if isinstance(data, dict) else (
            data if isinstance(data, list) else [])
        for node in graph:
            if isinstance(node, dict) and node.get("@type") == "Person":
                yield node


def parse_thatsthem(html: str) -> list[Person]:
    """Parse ThatsThem's JSON-LD into Person records.

    ThatsThem lists everyone *associated* with the building, with each person's
    unit baked into the most specific `homeLocation`. It does NOT distinguish
    current vs former, so these are treated as unit-level associations and
    cross-corroborated/ranked in Phase 2 (lower weight than an explicit
    "Lives at" from USPhoneBook).
    """
    people: list[Person] = []
    for node in _iter_jsonld_persons(html):
        name = (node.get("name") or "").strip()
        if not name:
            continue

        # homeLocation may be a dict or list; pick the most specific (has a unit).
        homes = node.get("homeLocation")
        if isinstance(homes, dict):
            homes = [homes]
        homes = homes or []

        best_addr = None
        for place in homes:
            sa = (place.get("address") or {}).get("streetAddress", "")
            pc = (place.get("address") or {}).get("postalCode", "")
            loc = (place.get("address") or {}).get("addressLocality", "")
            reg = (place.get("address") or {}).get("addressRegion", "")
            if not sa:
                continue
            full = ", ".join(b for b in [sa, loc, reg, pc] if b)
            addr = normalize_address(full)
            # Prefer the entry that carries a unit.
            if best_addr is None or (addr.has_unit() and not best_addr.has_unit()):
                best_addr = addr

        phones = node.get("telephone") or []
        if isinstance(phones, str):
            phones = [phones]

        knows = node.get("knows") or []
        relatives = [k.get("name", "").strip()
                     for k in knows if isinstance(k, dict) and k.get("name")]

        people.append(Person(
            name=name,
            current_address=best_addr,
            relatives=relatives,
            phones=[p for p in phones if p],
            source="ThatsThem",
        ))
    return people


_TPS_STOP = {"Apply", "View", "Details", "Records", "Record", "Filter", "All",
             "Ages", "Found", "Search", "Sponsored", "Links", "Neighbors"}


_FPS_NAME_STOP = {"Phone", "Numbers", "Number", "for", "Email", "Emails", "Address", "Addresses",
                  "AKA", "AKAs", "Background", "View", "FREE", "Free", "Marital", "Status",
                  "Associates", "Relatives", "Age", "Lives", "Lived", "Report", "Records",
                  "Social", "Media", "History", "Profile", "Full"}


_TPS_SECTIONS = (r"Lived here|Previous Address(?:es)?|Past Address(?:es)?|Phone Numbers?|"
                 r"Email Address(?:es)?|AKA|Also Known As|Possible Relatives|Possible Associates|"
                 r"Possible Owners|Neighbors|Sponsored|View All|$")


def parse_truepeoplesearch(text: str) -> list[Person]:
    """Parse a TruePeopleSearch detail page — AUTHORITATIVE (see `_parse_ps_detail`)."""
    return _parse_ps_detail(text, "TruePeopleSearch")


def parse_fastpeoplesearch(text: str) -> list[Person]:
    """Parse a FastPeopleSearch detail page — AUTHORITATIVE. FPS is TPS's sister site, so
    the detail-page layout is (near-)identical and shares `_parse_ps_detail`."""
    return _parse_ps_detail(text, "FastPeopleSearch")


def parse_searchpeoplefree(text: str) -> list[Person]:
    """SearchPeopleFree — FPS-family sister site; shares `_parse_ps_detail` (verify vs a capture)."""
    return _parse_ps_detail(text, "SearchPeopleFree")


def parse_fastbackgroundcheck(text: str) -> list[Person]:
    """FastBackgroundCheck — FPS-family sister site; shares `_parse_ps_detail` (verify vs a capture)."""
    return _parse_ps_detail(text, "FastBackgroundCheck")


def _parse_ps_detail(text: str, source: str) -> list[Person]:
    """Parse a TruePeopleSearch/FastPeopleSearch *detail* page — AUTHORITATIVE.

    The summary results page omits the unit-level address, so the orchestrator crawls each
    person's detail page (which has the full 'Current Address') and feeds those here.

    Returns [] for a non-detail page, so a stray summary page never produces mis-weighted records.
    """
    text = _collapse(text)

    _CO = r"[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)* (?:County|Parish|Borough)"

    # TPS anchor: "...most recently reported address for <Name>. <addr> <County>".
    cm = re.search(
        r"most recently reported address for\s+(?P<name>[A-Z][^.]+?)\.\s*(?P<addr>.+?)\s+"
        r"(?:" + _CO + r"|\(|Previous Address|Phone Number)", text)
    name = cm.group("name").strip() if cm else None
    addr = cm.group("addr") if cm else None
    since = until = ""
    if cm:
        # TPS prints the tenure as a date range right after the county: "(Jul 2018 - Jun 2026)".
        # Range START = move-in; range END = last-reported (a stale end => may have moved out).
        # (A phone like "(434) 906-..." can't match — the token must be a Month+year or bare year.)
        _D = r"[A-Za-z]{3,9}\.?\s+\d{4}|\d{4}"
        sm = re.search(r"\(\s*(" + _D + r")\s*(?:-\s*(" + _D + r")\s*)?\)",
                       text[cm.end():cm.end() + 80])
        if sm:
            since = sm.group(1)
            until = sm.group(2) or ""

    # FPS anchor: "Current Address (Since <date>) <addr> <County> ... Full Name: <Name>".
    if not cm:
        am = re.search(r"Current Address\s*(?:\(\s*Since\s+(?P<since>[^)]*?)\s*\)\s*)?"
                       r"(?P<addr>\d+\s+.+?)\s+"
                       r"(?:" + _CO + r"|Full Name|Phone Number|Lived here|Previous Address)", text)
        if not am:
            return []
        addr = am.group("addr")
        since = (am.group("since") or "").strip()
        # Allow single-letter middle initials ('Amy K Williams') — the tokens after the first
        # may be one char, else the Full Name truncates to the first name and the record is lost.
        nm = re.search(r"Full Name:\s*([A-Z][A-Za-z.'\-]*(?: [A-Z][A-Za-z.'\-]*){0,4})", text)
        if nm:
            toks = nm.group(1).split()
            while toks and toks[-1] in _FPS_NAME_STOP:   # strip trailing "Phone Numbers", etc.
                toks.pop()
            name = " ".join(toks)

    if name is None:
        nm = re.search(r"\bfor\s+([A-Z][A-Za-z.'\-]+(?: [A-Z][A-Za-z.'\-]+){1,3})\b", text)
        name = nm.group(1) if nm else ""
    if not name or len(name.split()) < 2:
        return []

    person = Person(name=name, source=source, current_since=since, current_until=until,
                    current_address=normalize_address(addr))
    person.aka = _extract_aka(text)

    am = re.search(r"\bAge\s+(\d+)\b", text)
    if am:
        person.age = int(am.group(1))
    else:
        by = re.search(r"born in\s+\w+\s+(\d{4})", text)
        if by:
            from datetime import date
            person.age = date.today().year - int(by.group(1))

    # Previous Addresses: the data block (the one that actually contains street numbers).
    for pm in re.finditer(r"Previous Address(?:es)?\s+(.{0,1200}?)"
                          r"(?:Phone Numbers?|Email Address|Possible|Background|$)", text):
        block = pm.group(1)
        if not re.search(r"\d+\s+[A-Z]", block):
            continue
        for chunk in re.findall(r"\d+\s+.+?(?:[A-Z][A-Za-z]+ (?:County|Parish|Borough)|\(|$)", block):
            chunk = re.sub(r"\s*(?:[A-Z][A-Za-z]+ (?:County|Parish|Borough)|\().*$", "", chunk).strip()
            if re.search(r"\d", chunk):
                person.prior_addresses.append(normalize_address(chunk))
        break

    # Phone numbers: rank by "Last reported" recency (most recent first), primary first, and DROP
    # numbers flagged Inactive. Skip the table-of-contents "Phone Numbers" heading (no digits).
    person.phones = []
    for pm in re.finditer(r"Phone Numbers?\b(.*?)(?:Email Address|Possible Relatives|"
                          r"Possible Associates|Education|Background Report|View |$)", text):
        ranked = _ranked_phones(pm.group(1))
        if ranked:
            person.phones = ranked
            break

    return [person]


# Anchors that appear ONLY on genuinely populated result/detail pages - NOT in page shells,
# boilerplate JSON-LD, or bot-check stubs. (Deliberately excludes 'application/ld+json' and
# 'Current Address', which appear on every page including throttled stubs -> false drift alarms.)
_RESULT_MARKERS = ("People Living at", "results for", "most recently reported address for",
                   "Full Name:", "VIEW DETAILS", "View Report")

# Bot-check / challenge / rate-limit stubs: a 0-parse here is a BLOCK, not parser drift.
_CHALLENGE_PHRASES = ("are you human", "re-captcha", "recaptcha", "strange activity",
                      "just a moment", "verify you are human", "checking your browser",
                      "access denied", "too many requests")


def has_result_markers(text: str) -> bool:
    """True if a page looks like a populated results/detail page (so a 0-parse means the parser
    drifted from the layout). A bot-check/challenge stub returns False - that's a block, not drift."""
    low = text.lower()
    if any(p in low for p in _CHALLENGE_PHRASES):
        return False
    return any(m in text for m in _RESULT_MARKERS)


# Registry so callers can dispatch by source name.
PARSERS = {
    "USPhoneBook": parse_usphonebook,
    "ThatsThem": parse_thatsthem,
    "CyberBackgroundChecks": parse_cyberbackgroundchecks,
    "TruePeopleSearch": parse_truepeoplesearch,
    "FastPeopleSearch": parse_fastpeoplesearch,
    "SearchPeopleFree": parse_searchpeoplefree,
    "FastBackgroundCheck": parse_fastbackgroundcheck,
}


def parse_source(source_name: str, text: str) -> list[Person]:
    fn = PARSERS.get(source_name)
    if not fn:
        return []
    res = fn(text)
    if not res and has_result_markers(text):
        print(f"    !! {source_name}: PARSER DRIFT detected, falling back to LLM...")
        try:
            from llm_parser import fallback_parse_llm
            res = fallback_parse_llm(source_name, text)
        except ImportError:
            print("    !! llm_parser not found, cannot fallback")
    return res
