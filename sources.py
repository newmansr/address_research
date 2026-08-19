"""
sources.py - unified fetch layer with per-source strategy + raw archival.

Access map (tested live):
  - ThatsThem            : NO browser (requests + JSON-LD). WHOLE building in one call.
  - USPhoneBook          : Cloudflare -> CDP browser. "People Living at" / "Lives at" text.
  - CyberBackgroundChecks: Cloudflare -> CDP browser. "Lives at / Used to live" + phones.
  - TruePeopleSearch     : Turnstile -> CDP browser; summary page + per-person detail crawl.
  - FastPeopleSearch     : Cloudflare -> CDP browser; summary page + per-person detail crawl.

Strategy: prefer browser-free sources; use ONE consolidated CDP-mode browser (SeleniumBase UC)
for the Cloudflare/Turnstile sources. Every fetch is archived to ./raw/ (capped) so parsers can
be re-tuned offline without re-scraping.

NOTE: these people-search sites are FCRA-restricted and their ToS generally prohibit scraping.
Intended for personal research only - NOT for tenant/employment/credit screening.
"""

from __future__ import annotations

import os
import time
import re
import random
from datetime import datetime

import requests

# Polite pacing between browser requests (seconds): base + up to this much jitter. Reduces the
# "too many requests" throttling that heavy back-to-back scraping triggers. 0 base disables it.
_PACE_JITTER = 2.0


def _pace(base: float) -> None:
    if base and base > 0:
        time.sleep(base + random.uniform(0, _PACE_JITTER))

RAW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# ── Raw archival ─────────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


RAW_KEEP = 400  # cap on archived captures; oldest are pruned so raw/ can't grow unbounded


def _prune_raw(keep: int = RAW_KEEP) -> None:
    try:
        files = [os.path.join(RAW_DIR, f) for f in os.listdir(RAW_DIR)]
        files = [f for f in files if os.path.isfile(f)]
        if len(files) > keep:
            files.sort(key=os.path.getmtime)
            for f in files[:len(files) - keep]:
                try:
                    os.remove(f)
                except OSError:
                    pass
    except OSError:
        pass


def archive_raw(source: str, street: str, unit: str, content: str, ext: str = "txt") -> str:
    """Persist a raw capture so it can be re-parsed later without re-fetching (oldest pruned)."""
    os.makedirs(RAW_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{_slug(street)}_{_slug(unit) or 'ALL'}__{source}__{stamp}.{ext}"
    path = os.path.join(RAW_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    _prune_raw()
    return path


_ALL_UNIT = "ALL"

# API sources (apis.py) archive raw JSON for audit, but they are queried LIVE, not replayed through
# the text parsers - so the --from-cache path must skip their captures (they have no text parser).
_API_REPLAY_SKIP = {"OpenFEC", "DCProperty", "DCPropertyOwner", "SECEdgar"}


def _parse_capture_name(stem: str, target_slug: str):
    """Split an archived filename stem into (unit, source_label, stamp), or None if it's not a
    capture for `target_slug`. Filename shape: '{street}_{unit|ALL}__{source}__{stamp}'."""
    parts = stem.split("__")
    if len(parts) < 3:
        return None
    street_unit, source, stamp = parts[0], parts[1], parts[2]
    if not street_unit.startswith(target_slug + "_"):
        return None
    return street_unit[len(target_slug) + 1:], source, stamp


def load_cached_captures(street: str, units) -> list[dict]:
    """Replay source: return archived raw/ captures for `street` (and `units`), newest run only.

    Lets the parsers/scoring be re-run with ZERO network - the same pages that were scraped feed
    straight back in. De-dupes to the most recent capture per (unit, source) so a stale capture
    can't out-vote a fresh one; for detail-crawl sources (many person-pages per run) it keeps every
    file from the most recent day. Building-level (ALL) captures are always included.

    Returns [{"name": source_label, "unit": unit_or_empty, "text": ...}] - the same shape
    `fetch_browser_sources` returns, so the caller parses it identically.
    """
    target_slug = _slug(street)
    want = set(units or [])
    try:
        files = [f for f in os.listdir(RAW_DIR) if os.path.isfile(os.path.join(RAW_DIR, f))]
    except OSError:
        return []

    groups: dict[tuple, list] = {}   # (unit, label) -> [(stamp, filename), ...]
    for f in files:
        stem = os.path.splitext(f)[0]
        parsed = _parse_capture_name(stem, target_slug)
        if not parsed:
            continue
        unit, label, stamp = parsed
        if label in _API_REPLAY_SKIP:      # API JSON is queried live, not replayed as text
            continue
        if want and unit != _ALL_UNIT and unit not in want:
            continue
        groups.setdefault((unit, label), []).append((stamp, f))

    captures: list[dict] = []
    for (unit, label), items in groups.items():
        items.sort()                                  # ascending by stamp
        if label.endswith("Detail"):                  # keep the whole newest run (per-person pages)
            newest_day = items[-1][0][:8]
            chosen = [(st, fn) for st, fn in items if st[:8] == newest_day]
        else:                                         # summary/single-record source: newest only
            chosen = [items[-1]]
        for stamp, fname in chosen:
            try:
                with open(os.path.join(RAW_DIR, fname), encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            captures.append({"name": label, "unit": "" if unit == _ALL_UNIT else unit,
                             "text": text, "stamp": stamp})
    return captures


# ── ThatsThem (browser-free, whole-building) ─────────────────────────────────

def thatsthem_building_url(street: str, city: str, state: str, zip_code: str = "") -> str:
    parts = [street, city, state]
    if zip_code:
        parts.append(zip_code)
    slug = "-".join("-".join(p.split()) for p in parts)
    return f"https://thatsthem.com/address/{slug}"


# NOTE: ThatsThem has no working unit-level URL - an "…-Apt-637-…" path still returns the whole
# building's residents (each tagged with their own unit via JSON-LD, which we filter downstream).


_TT_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


def _tt_get(url: str, proxy: str = ""):
    """GET ThatsThem with a real Chrome TLS fingerprint (curl_cffi) if available, else requests.
    curl_cffi defeats Cloudflare's bot-FINGERPRINT check; it can't defeat an IP-level block.
    A `proxy` ('host:port' or 'user:pass@host:port') routes through a different IP, which does."""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        from curl_cffi import requests as creq
        return creq.get(url, impersonate="chrome", timeout=30, proxies=proxies)
    except ImportError:
        sess = requests.Session()
        sess.headers.update(_TT_HEADERS)
        return sess.get(url, timeout=30, proxies=proxies)


def fetch_thatsthem(street: str, city: str, state: str, zip_code: str = "",
                    archive: bool = True, session=None, retries: int = 2, proxy: str = "") -> str:
    """Fetch the building-level ThatsThem page (covers every unit) browser-free. Returns HTML.

    A Cloudflare "too many requests" page is an IP-level block that does NOT clear by retrying
    (or waiting days) - so we fail FAST on it and let the browser fallback handle ThatsThem
    (a real browser passes the challenge that HTTP clients can't). Only transient errors retry.
    """
    url = thatsthem_building_url(street, city, state, zip_code)
    html = ""
    for attempt in range(retries):
        try:
            resp = _tt_get(url, proxy=proxy)
            body = resp.text
            if resp.status_code == 200 and "application/ld+json" in body:
                html = body
                break
            low = body.lower()
            if any(s in low for s in ("too many", "just a moment", "attention required",
                                      "access denied", "cf-chl", "/cdn-cgi/")):
                print(f"    ThatsThem: Cloudflare IP block ({len(body)} chars) - won't clear by "
                      f"retrying; using the browser instead.")
                break  # fail fast - retrying an IP block is pointless
            print(f"    ThatsThem attempt {attempt+1}: HTTP {resp.status_code}, len={len(body)} - retrying")
        except Exception as e:
            print(f"    ThatsThem attempt {attempt+1}: {type(e).__name__} - retrying")
        time.sleep(2 * (attempt + 1))

    if not html:
        print("    !! ThatsThem browser-free path blocked (IP flagged by Cloudflare). To restore it,"
              " change your IP (reboot router / phone hotspot / VPN). --browser clears it meanwhile.")
    if archive and html:
        archive_raw("ThatsThem", street, "", html, ext="html")
    return html


# ── Browser sources (Cloudflare) - consolidated on SeleniumBase UC ───────────

NO_RESULTS_PHRASES = [
    "we could not find any results", "no results found", "please review your search",
    "no records found", "loading search results", "404 - page not found",
]

# Cloudflare / bot-wall interstitials - page hasn't cleared yet.
BLOCK_PHRASES = [
    "performing security verification", "just a moment", "verify you are human",
    "checking your browser", "needs to review the security", "enable javascript and cookies",
    "ray id:",
]
MIN_RESULT_CHARS = 1200  # real result pages are large; smaller == shell/challenge

# Embedded JS interstitials that are LARGE (so size won't flag them) but aren't results.
# TPS renders a Cloudflare Turnstile widget then auto-submits a form to redirect to results;
# until that completes we're still on the loading/challenge page.
INTERSTITIAL_PHRASES = [
    "automatic submission failed", "loading content, please wait",
    "turnstile.render", "submitformcaptcha", "captchaform",
]


def _looks_blocked(text: str) -> bool:
    t = text.lower()
    return len(text) < MIN_RESULT_CHARS or any(p in t for p in BLOCK_PHRASES)


def _is_interstitial(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in INTERSTITIAL_PHRASES)


def _needs_clearing(text: str) -> bool:
    """A page we must not accept yet: a hard block, or a JS challenge/loading interstitial."""
    return _looks_blocked(text) or _is_interstitial(text)


def usphonebook_url(street: str, city: str, state: str, zip_code: str = "", unit: str = "") -> str:
    s = street.lower().replace(" ", "-")
    if unit:
        s += f"-unit-{unit.lower()}"
    return (f"https://www.usphonebook.com/address/{s}"
            f"_{city.lower().replace(' ', '-')}-{state.lower()}")


def truepeoplesearch_url(street: str, city: str, state: str, zip_code: str = "", unit: str = "") -> str:
    import urllib.parse
    street_q = street + (f" Apt {unit}" if unit else "")
    csz = f"{city}, {state}" + (f" {zip_code}" if zip_code else "")
    return ("https://www.truepeoplesearch.com/resultaddress?"
            + urllib.parse.urlencode({"streetaddress": street_q, "citystatezip": csz}))


def cyberbackgroundchecks_url(street: str, city: str, state: str, zip_code: str = "", unit: str = "") -> str:
    s = street.lower().replace(" ", "-")
    if unit:
        s += f"-apt-{unit.lower()}"
    return (f"https://www.cyberbackgroundchecks.com/address/{s}"
            f"/{city.lower().replace(' ', '-')}/{state.lower()}")


def _fps_style_url(host: str, street: str, city: str, state: str, unit: str = "") -> str:
    """Shared /address/{street}[-unit-N]_{city}-{state} slug used by the FPS site family."""
    s = street.lower().replace(" ", "-")
    if unit:
        s += f"-unit-{unit.lower()}"
    return f"https://www.{host}/address/{s}_{city.lower().replace(' ', '-')}-{state.lower()}"


def fastpeoplesearch_url(street, city, state, zip_code="", unit=""):
    return _fps_style_url("fastpeoplesearch.com", street, city, state, unit)


def searchpeoplefree_url(street, city, state, zip_code="", unit=""):
    return _fps_style_url("searchpeoplefree.com", street, city, state, unit)


def fastbackgroundcheck_url(street, city, state, zip_code="", unit=""):
    return _fps_style_url("fastbackgroundcheck.com", street, city, state, unit)


def nuwber_url(street, city, state, zip_code="", unit=""):
    s = street.replace(" ", "-")
    if unit:
        s += f"-Unit-{unit}"
    return f"https://nuwber.com/address/{s}-{city.replace(' ', '-')}-{state}"


# Browser source specs: (source name, url builder, result-wait CSS).
BROWSER_SOURCES = {
    "USPhoneBook": (usphonebook_url, "body"),
    "TruePeopleSearch": (truepeoplesearch_url, ".card-summary"),
    "CyberBackgroundChecks": (cyberbackgroundchecks_url, "body"),
    "FastPeopleSearch": (fastpeoplesearch_url, "body"),
    "SearchPeopleFree": (searchpeoplefree_url, "body"),
    "FastBackgroundCheck": (fastbackgroundcheck_url, "body"),
    "Nuwber": (nuwber_url, "body"),
}


def _detect_text(sb) -> str:
    """Fast body text for challenge detection (CDP - newlines not preserved)."""
    try:
        return sb.cdp.get_text("body") or ""
    except Exception:
        try:
            return sb.get_text("body") or ""
        except Exception:
            return ""


def _clean_text(sb) -> str:
    """Newline-preserving body text for PARSING (WebDriver .text), CDP as fallback.

    The parsers are line-based; WebDriver's `.text` separates block elements with
    newlines, whereas CDP's get_text concatenates with spaces (which broke parsing).
    """
    try:
        t = sb.get_text("body")
        if t and len(t) > 200:
            return t
    except Exception:
        pass
    return _detect_text(sb)


def _page_source(sb) -> str:
    """Full page HTML - needed for sources parsed from markup (ThatsThem JSON-LD)."""
    for getter in (lambda: sb.cdp.get_page_source(), lambda: sb.get_page_source()):
        try:
            h = getter()
            if h:
                return h
        except Exception:
            continue
    return ""


def _display_scaling_percent():
    """Best-effort Windows display-scaling detection (100 = no scaling; None if unknown).
    Reads physical vs logical width - no DPI-awareness change, so it won't affect the browser."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        hdc = ctypes.windll.user32.GetDC(0)
        logical = ctypes.windll.gdi32.GetDeviceCaps(hdc, 8)     # HORZRES (scaled)
        physical = ctypes.windll.gdi32.GetDeviceCaps(hdc, 118)  # DESKTOPHORZRES (real)
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return round(physical / logical * 100) if logical else None
    except Exception:
        return None


def _warn_if_scaled() -> None:
    """Non-100% scaling offsets the PyAutoGUI Turnstile click, so challenges won't clear."""
    pct = _display_scaling_percent()
    if pct and pct > 105:
        print(f"  !! Display scaling looks like ~{pct}% (not 100%). The Turnstile checkbox click "
              f"will likely MISS - set Windows Display scaling to 100% for reliable clearing "
              f"(Settings > System > Display > Scale). The keyboard fallback still tries.")


def _focus_browser(sb) -> None:
    """Bring the Chrome window to the foreground - PyAutoGUI clicks land on the focused
    window, so a Turnstile click misses if the terminal/another app is on top."""
    for call in (lambda: sb.cdp.bring_active_window_to_front(),
                 lambda: sb.bring_to_front(),
                 lambda: sb.cdp.maximize()):
        try:
            call()
            return
        except Exception:
            continue


def _clear_challenge(sb, max_tries: int = 8) -> None:
    """Persistently clear a Cloudflare Turnstile challenge / JS interstitial on the CDP page.

    CDP mode passes most "managed" challenges with no click; an INTERACTIVE challenge needs a
    real click on the checkbox. The click is done with PyAutoGUI (physical mouse), so it depends
    on the Chrome window being focused + 100% display scaling. To survive a silently-missed
    mouse click we ALTERNATE the mouse method with the keyboard method (Tab+Space) across
    attempts, and re-focus the window before each try.
    """
    sb.sleep(3)
    methods = ("uc_gui_click_captcha", "uc_gui_handle_captcha")
    for attempt in range(max_tries):
        if not _needs_clearing(_detect_text(sb)):
            return
        print(f"      challenge/interstitial present - clearing "
              f"(attempt {attempt + 1}/{max_tries})...")
        _focus_browser(sb)
        # Alternate mouse-click / keyboard each attempt (mouse can miss silently); fall back to
        # the other method immediately if the chosen one raises.
        order = methods if attempt % 2 == 0 else methods[::-1]
        for m in order:
            try:
                getattr(sb, m)()
                break
            except Exception as e:
                print(f"        {m} failed: {type(e).__name__}")
        # Give the challenge time to settle AND (for TPS) the form-submit redirect to land.
        sb.sleep(5 + attempt)


MAX_DETAILS = 8  # cap detail-page fetches per unit (bounds time + challenges)

# Sources whose results page is a summary (city-level): crawl per-person detail pages for the
# full unit-level address. (source) -> (detail-link regex, base url, archive label).
DETAIL_CRAWL = {
    "TruePeopleSearch": (r"/find/person/[A-Za-z0-9]+",
                         "https://www.truepeoplesearch.com", "TruePeopleSearchDetail"),
    "FastPeopleSearch": (r"/[a-z0-9][a-z0-9-]*_id_[A-Za-z0-9]+",
                         "https://www.fastpeoplesearch.com", "FastPeopleSearchDetail"),
    # FPS sister sites - same `/{slug}_id_{id}` detail links (verify link format from a capture).
    "SearchPeopleFree": (r"/[a-z0-9][a-z0-9-]*_id_[A-Za-z0-9]+",
                         "https://www.searchpeoplefree.com", "SearchPeopleFreeDetail"),
    "FastBackgroundCheck": (r"/[a-z0-9][a-z0-9-]*_id_[A-Za-z0-9]+",
                            "https://www.fastbackgroundcheck.com", "FastBackgroundCheckDetail"),
}


# Words that can precede a name in the summary text but aren't part of it.
_NAME_PREFIX_STOP = {
    "view", "free", "details", "detail", "past", "previous", "current", "address", "addresses",
    "relatives", "relative", "associates", "aka", "akas", "phone", "phones", "number", "numbers",
    "email", "emails", "background", "report", "records", "record", "search", "sponsored",
    "possible", "neighbors", "social", "media", "lives", "lived", "property", "in", "for",
    "also", "known", "as", "age",
}


def _summary_resident_keys(summary_text: str) -> set:
    """(first, last) name keys for people the summary lists with an age (the residents)."""
    flat = re.sub(r"\s+", " ", summary_text)
    keys = set()
    for m in re.finditer(r"([A-Z][A-Za-z.'\-]+(?: [A-Z][A-Za-z.'\-]+){1,3})\s+Age\s+\d+", flat):
        toks = [t for t in m.group(1).split() if t.lower() not in _NAME_PREFIX_STOP]
        if len(toks) >= 2:
            keys.add((toks[0].lower(), toks[-1].lower()))
    return keys


def _crawl_detail_pages(sb, spec: dict, archive: bool, summary_text: str = "",
                        delay: float = 0.0) -> list[str]:
    """From a cleared summary page, crawl each person's detail page for the FULL
    (unit-level) current address that the summary omits.

    Speed: when the detail URL encodes the name (FPS `/{first}-{last}_id_…`), only crawl links
    whose slug matches a RESIDENT from the summary - skipping relatives'/associates' pages.
    (TPS uses opaque `/find/person/<id>` links, which its result list already limits to residents.)

    The first challenge solve usually sets a session cookie, so detail pages typically load
    without another challenge (but we clear just in case). Returns the detail-page texts.
    """
    link_re, base_url, label = DETAIL_CRAWL[spec["name"]]
    try:
        html = sb.cdp.get_page_source()
    except Exception:
        try:
            html = sb.get_page_source()
        except Exception:
            html = ""

    links: list[str] = []
    for m in re.finditer(link_re, html):
        if m.group(0) not in links:
            links.append(m.group(0))

    # Filter name-slug links (FPS) down to the summary's residents.
    resident_keys = _summary_resident_keys(summary_text)
    if resident_keys:
        filtered = []
        for link in links:
            sm = re.search(r"/([a-z][a-z0-9]*(?:-[a-z0-9]+)+)_id_", link)
            if not sm:
                filtered.append(link)          # no name in URL (TPS) -> keep
                continue
            slug = sm.group(1).split("-")
            if (slug[0], slug[-1]) in resident_keys:
                filtered.append(link)
        if filtered:
            links = filtered

    out: list[str] = []
    for link in links[:MAX_DETAILS]:
        try:
            _pace(delay)   # polite gap between detail-page loads
            sb.cdp.open(base_url + link)
            _clear_challenge(sb)
            dtext = _clean_text(sb)
            if archive:
                archive_raw(label, spec.get("street", ""), spec.get("unit", ""), dtext)
            if not _needs_clearing(dtext):
                out.append(dtext)
        except Exception as e:
            print(f"      {label} error: {type(e).__name__}")
    print(f"    {spec['name']}: crawled {len(out)}/{len(links[:MAX_DETAILS])} detail page(s)")
    return out


def fetch_browser_sources(specs: list[dict], archive: bool = True,
                          proxies: str = "", delay: float = 0.0, session_dir: str = "") -> list[dict]:
    """Fetch Cloudflare-protected sources with a single SeleniumBase CDP-mode browser.

    `specs` = [{"name", "url", "street", "unit", "wait_css"(optional)}].
    Returns a LIST of {"name", "unit", "text"} (one per spec) - never keyed by name,
    so multiple units of the same source don't overwrite each other.

    `proxies` is a comma-separated list; on a block we back off, rotate to the next proxy and
    restart the browser. `session_dir`, if given, is a PERSISTENT Chrome profile (user_data_dir) so
    a solved Cloudflare/Turnstile clearance carries over to later runs instead of re-challenging
    every time (the single biggest anti-blocking win); it disables incognito.
    """
    from seleniumbase import SB  # imported lazily so requests-only runs need no browser

    _warn_if_scaled()  # heads-up before we rely on PyAutoGUI clicks for Turnstile
    results: list[dict] = []

    proxy_list = [p.strip() for p in proxies.split(",")] if proxies else [""]
    proxy_idx = 0
    spec_idx = 0
    block_hits = 0

    while spec_idx < len(specs):
        current_proxy = proxy_list[proxy_idx] or None
        cdp_active = False
        try:
            with SB(uc=True, locale="en", incognito=(not session_dir),
                    user_data_dir=(session_dir or None), proxy=current_proxy,
                    chromium_arg="--no-sandbox,--disable-dev-shm-usage") as sb:
                while spec_idx < len(specs):
                    spec = specs[spec_idx]
                    name, url = spec["name"], spec["url"]
                    print(f"\n  [{name}] {url}")
                    if spec_idx > 0:
                        _pace(delay)
                    try:
                        if not cdp_active:
                            sb.activate_cdp_mode(url)
                            cdp_active = True
                        else:
                            sb.cdp.open(url)

                        _clear_challenge(sb)
                        want_html = spec.get("html", False)
                        text = _page_source(sb) if want_html else _clean_text(sb)

                        if archive:
                            archive_raw(name, spec.get("street", ""), spec.get("unit", ""), text,
                                        ext="html" if want_html else "txt")

                        if _needs_clearing(text):
                            block_hits += 1
                            print(f"    !! {name}: blocked / challenge not cleared ({len(text)} chars).")
                            if proxy_idx < len(proxy_list) - 1:
                                wait = min(3 + 2 * block_hits, 20)   # back off before rotating
                                print(f"    -> Backing off {wait}s, rotating to next proxy, retrying...")
                                time.sleep(wait)
                                proxy_idx += 1
                                break  # Break inner loop to restart SB with new proxy
                            else:
                                print(f"    -> No more proxies. Skipping.")
                                spec_idx += 1
                                continue
                                
                        if want_html and "application/ld+json" not in text:
                            print(f"    !! {name}: no data block (still rate-limited?) - skipping.")
                            spec_idx += 1
                            continue
                            
                        if not want_html and any(p in text.lower() for p in NO_RESULTS_PHRASES):
                            print(f"    !! {name}: no-results page, skipping.")
                            spec_idx += 1
                            continue
                            
                        print(f"    got {len(text)} chars")

                        if name in DETAIL_CRAWL:
                            if not _summary_resident_keys(text):
                                print(f"    !! {name}: summary lists 0 residents (throttled stub?) - skipping detail crawl.")
                                spec_idx += 1
                                continue
                            for dtext in _crawl_detail_pages(sb, spec, archive, summary_text=text, delay=delay):
                                results.append({"name": name, "unit": spec.get("unit", ""), "text": dtext})
                            spec_idx += 1
                            continue

                        results.append({"name": name, "unit": spec.get("unit", ""), "text": text})
                        spec_idx += 1
                        
                    except Exception as e:
                        print(f"    !! {name} error: {e}")
                        spec_idx += 1
        except Exception as outer_e:
             print(f"  !! SB init error: {outer_e}")
             if proxy_idx < len(proxy_list) - 1:
                 proxy_idx += 1
             else:
                 spec_idx += 1
                 
    return results


# ── Reverse Phone & Forward Name Search URL builders ─────────────────────────

def _phone_digits(phone: str) -> str:
    """Strip a phone string to its 10-digit core."""
    import re
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits


def tps_phone_url(phone: str) -> str:
    d = _phone_digits(phone)
    return f"https://www.truepeoplesearch.com/resultphone?phoneno={d}"


def fps_phone_url(phone: str) -> str:
    d = _phone_digits(phone)
    return f"https://www.fastpeoplesearch.com/{d}"


def usphonebook_phone_url(phone: str) -> str:
    d = _phone_digits(phone)
    return f"https://www.usphonebook.com/{d}"


def tps_name_url(first: str, last: str, city: str = "", state: str = "") -> str:
    import urllib.parse
    csz = f"{city}, {state}" if city else state
    return ("https://www.truepeoplesearch.com/resultname?"
            + urllib.parse.urlencode({"searchedName": f"{first} {last}", "citystatezip": csz}))


def fps_name_url(first: str, last: str, city: str = "", state: str = "") -> str:
    slug = f"{first.lower()}-{last.lower()}"
    loc = f"_{city.lower().replace(' ', '-')}-{state.lower()}" if city else ""
    return f"https://www.fastpeoplesearch.com/name/{slug}{loc}"


def usphonebook_name_url(first: str, last: str, city: str = "", state: str = "") -> str:
    slug = f"{first.lower()}-{last.lower()}"
    loc = f"_{city.lower().replace(' ', '-')}-{state.lower()}" if city else ""
    return f"https://www.usphonebook.com/{slug}{loc}"


PHONE_SOURCES = {
    "TruePeopleSearch": tps_phone_url,
    "FastPeopleSearch": fps_phone_url,
    "USPhoneBook": usphonebook_phone_url,
}

NAME_SOURCES = {
    "TruePeopleSearch": tps_name_url,
    "FastPeopleSearch": fps_name_url,
    "USPhoneBook": usphonebook_name_url,
}
