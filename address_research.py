"""
Address Research Tool — Auto Cloudflare bypass + Ollama
---------------------------------------------------------
Uses nodriver for Cloudflare sites (FPS, TPS) — the official successor
to undetected-chromedriver with better bypass rates.
Other sites auto-scrape via SeleniumBase.

Setup:
    pip install nodriver seleniumbase pyautogui
    ollama pull llama3

Usage:
    C:/Users/newma/miniconda3/python.exe address_research.py
"""

import asyncio
import json
import re
import urllib.request
import os
import time
import nodriver as uc
from seleniumbase import SB

# ── Configuration ────────────────────────────────────────────────
OLLAMA_MODEL  = "llama3"
OLLAMA_URL    = "http://localhost:11434/api/generate"

SOURCES = [
    {
        "name": "FastPeopleSearch",
        "url": lambda s, c, st, z="": (
            f"https://www.fastpeoplesearch.com/address/"
            f"{s.lower().replace(' ', '-')}_{c.lower().replace(' ', '-')}-{st.lower()}"
        ),
        "manual": True,
        "result_css": "div.card",
    },
    {
        "name": "TruePeopleSearch",
        "url": lambda s, c, st, z="": (
            f"https://www.truepeoplesearch.com/resultaddress?streetaddress="
            + re.sub(r'\b(unit)\b', 'Apt', s, flags=re.IGNORECASE).replace(' ', '%20')
            + f"&citystatezip={c.replace(' ', '%20')}, {st}"
            + (f"%20{z}" if z else "")
        ),
        "has_cloudflare": True,
        "result_selector": ".card-body",
    },
    {
        "name": "USPhoneBook",
        "url": lambda s, c, st, z="": (
            f"https://www.usphonebook.com/address/"
            + s.lower().replace(' ', '-')
            + "_"
            + c.lower().replace(' ', '-')
            + "-"
            + st.lower()
        ),
        "auto_scrape": True,
    },
    {
        "name": "ThatsThem",
        "url": lambda s, c, st, z="": (
            f"https://thatsthem.com/address/"
            + re.sub(r'\b(unit|apt)\b\s*\S+', '', s, flags=re.IGNORECASE).strip().replace(' ', '-')
            + f"/{c.title().replace(' ', '-')}/{st.upper()}"
            + (f"/{z}" if z else "")
        ),
        "auto_scrape": True,
        "wait_for_url_change": "/searching",
    },
]


# ── Ollama Analysis ───────────────────────────────────────────────
def analyze_with_ollama(address, combined_text):
    prompt = f"""You are analyzing raw scraped text from public records websites for: {address}

KEY RULE: Only list someone as a CURRENT resident if their current/most recent address in the data matches
or closely matches "{address}". If their listed current address is somewhere else (different city, different
street), they are a FORMER resident - list them separately.

Raw scraped text:
---
{combined_text[:12000]}
---

Return:
## Current Residents
(only people whose current address matches the target address)

## Former Residents
(people whose prior addresses include the target but who now live elsewhere)

## Confidence
High / Medium / Low - and why

## Notes
Any conflicts between sources, data quality issues, or caveats."""

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    print("\nRunning local analysis with Ollama (this may take 20-60 seconds)...")
    with urllib.request.urlopen(req, timeout=600) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        return result.get("response", "No response returned.")


# ── Cloudflare scraper (nodriver) ─────────────────────────────────
NO_RESULTS_PHRASES = [
    "we could not find any results",
    "no results found",
    "please review your search",
    "no records found",
    "loading search results",
]



# ── Manual scraper (undetected_chromedriver) ──────────────────────
def scrape_manual_sites(sources_manual, street, city, state, zip_code):
    """Open in browser, user solves Cloudflare manually, script scrapes on Enter."""
    import undetected_chromedriver as uc_driver
    from selenium.webdriver.common.by import By as SeBy

    results = {}
    options = uc_driver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--incognito")
    driver = uc_driver.Chrome(options=options, use_subprocess=True, version_main=145)
    driver.set_page_load_timeout(300)
    driver.command_executor._client_config.timeout = 600

    try:
        for source in sources_manual:
            name       = source["name"]
            url        = source["url"](street, city, state, zip_code)
            result_css = source.get("result_css", "body")
            print(f"\n  [{name}] Opening browser...")
            print(f"  URL: {url}")
            driver.get(url)

            while True:
                input(f"\n  -> Solve Cloudflare in the browser, wait for results,\n"
                      f"     then press Enter to scrape {name}...")
                page_text = driver.find_element(SeBy.TAG_NAME, "body").text
                if any(p in page_text.lower() for p in NO_RESULTS_PHRASES):
                    print(f"  !! No results yet, try again.")
                    continue
                try:
                    driver.find_element(SeBy.CSS_SELECTOR, result_css)
                    print(f"  Results confirmed. Scraping...")
                    break
                except Exception:
                    print(f"  '{result_css}' not found yet, try again.")

            page_text = driver.find_element(SeBy.TAG_NAME, "body").text
            print(f"  Got {len(page_text)} characters from {name}.")
            results[name] = f"=== {name} ===\n{page_text}\n"

    finally:
        try:
            driver.service.stop()
        except Exception:
            pass
        try:
            driver.quit()
        except Exception:
            pass
        try:
            type(driver).__del__ = lambda self: None
        except Exception:
            pass

    return results



    """Scrape non-Cloudflare sites using SeleniumBase."""
    results = {}

    with SB(uc=True, incognito=True, headed=True) as sb:
        sb.driver.set_page_load_timeout(300)
        for source in sources_auto:
            name = source["name"]
            url  = source["url"](street, city, state, zip_code)
            print(f"\n  [{name}] Auto-scraping...")
            print(f"  URL: {url}")

            try:
                sb.driver.get(url)

                wait_fragment = source.get("wait_for_url_change")
                if wait_fragment:
                    print(f"  Waiting for redirect...")
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if wait_fragment not in sb.driver.current_url:
                            break
                        time.sleep(0.5)
                    time.sleep(2)
                else:
                    time.sleep(4)

                page_text = sb.get_text("body")
                if any(p in page_text.lower() for p in NO_RESULTS_PHRASES):
                    print(f"  !! '{name}' returned a no-results page. Skipping.")
                else:
                    print(f"  Got {len(page_text)} characters from {name}.")
                    results[name] = f"=== {name} ===\n{page_text}\n"

            except Exception as e:
                print(f"  !! Error scraping {name}: {e}")

    return results


# ── Main ──────────────────────────────────────────────────────────
async def scrape_with_nodriver(sources_cf, street, city, state, zip_code):
    """Scrape Cloudflare-protected sites using nodriver."""
    results = {}
    browser = await uc.start(headless=False)

    try:
        for source in sources_cf:
            name     = source["name"]
            url      = source["url"](street, city, state, zip_code)
            selector = source.get("result_selector", "body")
            print(f"\n  [{name}] Navigating...")
            print(f"  URL: {url}")

            try:
                tab = await browser.get(url)

                print(f"  Waiting for results (handles Cloudflare automatically)...")
                deadline = time.time() + 60
                found = False
                while time.time() < deadline:
                    try:
                        el = await tab.find(selector, timeout=2)
                        if el:
                            found = True
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(1)

                if not found:
                    print(f"  Warning: '{selector}' never appeared - scraping anyway.")

                content = await tab.get_content()
                text = re.sub(r'<[^>]+>', ' ', content)
                text = re.sub(r'\s+', ' ', text).strip()

                if any(p in text.lower() for p in NO_RESULTS_PHRASES):
                    print(f"  !! '{name}' did not return usable results. Skipping.")
                else:
                    print(f"  Got {len(text)} characters from {name}.")
                    results[name] = f"=== {name} ===\n{text}\n"

            except Exception as e:
                print(f"  !! Error scraping {name}: {e}")

    finally:
        try:
            browser.stop()
        except Exception:
            pass

    return results


def scrape_auto_sites(sources_auto, street, city, state, zip_code):
    """Scrape non-Cloudflare sites using SeleniumBase."""
    results = {}

    with SB(uc=True, incognito=True, headed=True) as sb:
        sb.driver.set_page_load_timeout(300)
        for source in sources_auto:
            name = source["name"]
            url  = source["url"](street, city, state, zip_code)
            print(f"\n  [{name}] Auto-scraping...")
            print(f"  URL: {url}")

            try:
                sb.driver.get(url)

                wait_fragment = source.get("wait_for_url_change")
                if wait_fragment:
                    print(f"  Waiting for redirect...")
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if wait_fragment not in sb.driver.current_url:
                            break
                        time.sleep(0.5)
                    time.sleep(2)
                else:
                    time.sleep(4)

                page_text = sb.get_text("body")
                if any(p in page_text.lower() for p in NO_RESULTS_PHRASES):
                    print(f"  !! '{name}' returned a no-results page. Skipping.")
                else:
                    print(f"  Got {len(page_text)} characters from {name}.")
                    results[name] = f"=== {name} ===\n{page_text}\n"

            except Exception as e:
                print(f"  !! Error scraping {name}: {e}")

    return results


def research_address(street, city, state, zip_code=""):
    street  = re.sub(r'\b(se|sw|ne|nw)\b', lambda m: m.group().upper(), street.title(), flags=re.IGNORECASE)
    city    = city.title()
    state   = state.upper()
    address = f"{street}, {city}, {state} {zip_code}".strip()
    print(f"\nResearching: {address}")
    print("=" * 60)

    script_dir = os.path.dirname(os.path.abspath(__file__))

    sources_manual = [s for s in SOURCES if s.get("manual")]
    sources_cf     = [s for s in SOURCES if s.get("has_cloudflare")]
    sources_auto   = [s for s in SOURCES if s.get("auto_scrape")]

    all_results = {}

    # Manual sites (FPS — you solve Cloudflare, script scrapes on Enter)
    if sources_manual:
        print("\n── Manual sites ──")
        manual_results = scrape_manual_sites(sources_manual, street, city, state, zip_code)
        all_results.update(manual_results)

    # Cloudflare auto sites (TPS — nodriver handles it)
    if sources_cf:
        print("\n── Cloudflare sites (nodriver auto-bypass) ──")
        cf_results = asyncio.run(scrape_with_nodriver(sources_cf, street, city, state, zip_code))
        all_results.update(cf_results)

    # Auto-scrape sites
    if sources_auto:
        print("\n── Auto-scrape sites ──")
        auto_results = scrape_auto_sites(sources_auto, street, city, state, zip_code)
        all_results.update(auto_results)

    if not all_results:
        print("\nNo results collected.")
        return

    # Preserve SOURCES order
    all_text = [all_results[s["name"]] for s in SOURCES if s["name"] in all_results]
    combined = "\n\n".join(all_text)
    print(f"\nTotal text collected: {len(combined)} characters")

    analysis = analyze_with_ollama(address, combined)

    print("\n" + "=" * 60)
    print("ANALYSIS RESULT")
    print("=" * 60)
    print(analysis)
    print("=" * 60)

    output_file = os.path.join(script_dir, f"results_{street.replace(' ', '_')}_{city}.txt")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"Address: {address}\n\n")
        f.write("=== RAW SCRAPED DATA ===\n\n")
        f.write(combined)
        f.write("\n\n=== ANALYSIS ===\n\n")
        f.write(analysis)

    print(f"\nFull results saved to: {output_file}")


# ── Entry point ───────────────────────────────────────────────────
if __name__ == "__main__":
    print("Address Research Tool")
    print("---------------------")
    street   = input("Street address (e.g. 800 New Jersey Ave SE Unit 837): ").strip()
    city     = input("City [Washington]: ").strip() or "Washington"
    state    = input("State [DC]: ").strip() or "DC"
    zip_code = input("Zip code [20003]: ").strip() or "20003"

    research_address(street, city, state, zip_code)
