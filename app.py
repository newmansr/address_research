import streamlit as st
import os
from lookup import gather_people, gather_api_sources, score_candidates, owner_context
from resident_core import Address, normalize_address
from enrich import enrich_person, format_enrichment
from map_viz import generate_migration_map
from streamlit_folium import st_folium

st.set_page_config(page_title="Address Research OSINT", page_icon="🔍", layout="wide")
st.title("🔍 Advanced Address Research Dashboard")

st.sidebar.header("Target Location")
street = st.sidebar.text_input("Street Address", "800 New Jersey Ave SE")
city = st.sidebar.text_input("City", "Washington")
state = st.sidebar.text_input("State", "DC")
zip_code = st.sidebar.text_input("Zip Code", "20003")
units_str = st.sidebar.text_input("Units (comma separated)", "")

st.sidebar.header("OSINT Modules")
do_enrich = st.sidebar.checkbox("Deep Web Enrichment (Court, Socials, LLCs)", value=True)
use_browser = st.sidebar.checkbox(
    "Browser scrapers (SeleniumBase — slow, may hang in Docker)", value=False,
    help="FastPeopleSearch / TruePeopleSearch / USPhoneBook via headless Chrome. Off by default: "
         "the open-data APIs always run regardless. Enable only once the SeleniumBase-in-Docker "
         "path is confirmed working, or the scan can stall on a Chrome launch deadlock.")

# Default sources matching the CLI
BROWSER_SOURCES = ["FastPeopleSearch", "TruePeopleSearch", "USPhoneBook"]
API_SOURCES = ["DCProperty", "OSMContext", "DCVacant", "DCPermit", "DCBusinessLicense", "DC311", "DCCrime", "OpenFEC"]


def run_scan():
    """Run the full pipeline ONCE and return a results dict to stash in session_state.

    Everything network-bound (collection, scoring, owner unmasking, enrichment) happens here,
    behind the scan spinner. Rendering then reads only from the returned dict, so Streamlit reruns
    (notably the one st_folium fires to sync its state) re-render instantly and never re-hit the
    network or wipe the results.
    """
    units = [u.strip().upper() for u in units_str.split(",")] if units_str else []
    street_up = " ".join(street.split()).upper()
    target_str = f"{street_up}, {city.upper()}, {state.upper()} {zip_code}"
    target = normalize_address(target_str)

    status_text = st.empty()

    # Open-data APIs first: independent, browser-free, and can't be blocked by Cloudflare, so they
    # always return something even if every people-search broker is blocked/deadlocked.
    status_text.info("Querying open-data & context APIs (DC property, permits, crime, FEC)...")
    people = gather_api_sources(street_up, city.upper(), state.upper(), zip_code, API_SOURCES,
                                proxies="", fec_opts={})

    # People-search brokers. ThatsThem is browser-free; the rest need SeleniumBase. The browser path
    # can deadlock at Chrome launch inside a headless container, so when it's enabled we run it under
    # a timeout and fall back to the open-data results instead of hanging the whole scan.
    brokers = ["ThatsThem"] + (BROWSER_SOURCES if use_browser else [])
    status_text.info(f"Gathering people-search brokers: {', '.join(brokers)}...")

    def _gather_brokers():
        return gather_people(street_up, city.upper(), state.upper(), zip_code, units,
                             use_browser, BROWSER_SOURCES, proxies="", delay=2.0, session_dir="")

    timed_out = False
    if use_browser:
        import concurrent.futures
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_gather_brokers)
        try:
            # Generous ceiling: a real browser scrape of 4 sources + FPS/TPS detail crawls is many
            # slow, challenge-clearing page loads. This only bites on a genuine hang, not a slow run.
            people += fut.result(timeout=900)
        except concurrent.futures.TimeoutError:
            timed_out = True
        finally:
            ex.shutdown(wait=False)
    else:
        people += _gather_brokers()

    status_text.info(f"Collection complete ({len(people)} raw records). Correlating...")
    current, possible, former, b_only = score_candidates(people, target)
    owner_lines = owner_context(people, target, do_unmask=do_enrich)

    # Enrichment runs ONCE here (not per-rerun): each person's deep-web intel is computed under the
    # scan spinner and stored, so the roster renders instantly afterwards. Guarded so a slow/blocked
    # source can't crash the scan.
    enrich_map = {}
    if do_enrich:
        candidates = current + possible + former
        for i, p in enumerate(candidates):
            if p.name in enrich_map:
                continue
            status_text.info(f"Deep-web enrichment {i + 1}/{len(candidates)}: {p.name}...")
            try:
                enrich_map[p.name] = format_enrichment(p.name, enrich_person(p.name, city, state))
            except Exception as e:
                enrich_map[p.name] = [f"(enrichment error: {type(e).__name__})"]

    status_text.empty()
    return {
        "current": current, "possible": possible, "former": former, "b_only": b_only,
        "owner_lines": owner_lines, "enrich": enrich_map,
        "raw": [(p.source, p.name) for p in people],
        "target_str": target_str,
        "timed_out": timed_out,
    }


if st.sidebar.button("Launch OSINT Scan"):
    with st.spinner("Executing multi-source scrape... (this can take a minute)"):
        st.session_state.scan = run_scan()


# ── Render from session_state so st_folium's rerun can't blank the page ───────────────────────────
scan = st.session_state.get("scan")

if not scan:
    st.info("Enter a target address in the sidebar and click **Launch OSINT Scan**.")
    st.stop()

if scan.get("timed_out"):
    st.warning("Browser scrapers timed out (SeleniumBase likely deadlocked in Docker) — showing "
               "open-data results only.")

current, possible, former = scan["current"], scan["possible"], scan["former"]
enrich_lines = scan["enrich"]

with st.expander("🛠️ Debug: Raw Scraped Records (Before Correlation)"):
    if not scan["raw"]:
        st.error("ZERO records were returned by any source (open-data APIs and brokers all empty).")
    else:
        for src, name in scan["raw"]:
            st.text(f"Source: {src} | Name: {name}")

tab_roster, tab_building, tab_map = st.tabs(["👥 Resident Roster", "🏢 Building Context", "🗺️ Migration Map"])

with tab_roster:
    col1, col2, col3 = st.columns(3)

    def render_person(p):
        age_str = f" (Age {p.age})" if p.age else ""
        conf = f" · {p.confidence}" if getattr(p, "confidence", "") else ""
        with st.expander(f"{p.name}{age_str}  [Score: {p.score}{conf}]"):
            st.caption(f"Sources: {', '.join(p.sources)}")
            if p.phones:
                st.markdown("**Phones:** " + ", ".join(p.phones))
            if getattr(p, "since", ""):
                st.markdown(f"**Resident since:** {p.since}")
            if getattr(p, "moved_to", ""):
                st.markdown(f"**Now listed at:** {p.moved_to}")
            if p.evidence:
                st.markdown("**Evidence:**")
                for ev in p.evidence:
                    st.markdown(f"- {ev}")
            if p.prior_addresses:
                st.markdown("**Prior addresses:**")
                for a in p.prior_addresses[:3]:
                    st.markdown(f"- {a.display()}")

            elines = enrich_lines.get(p.name)
            if elines:
                st.markdown("---")
                st.markdown("**Deep Web Intel:**")
                for line in elines:
                    st.markdown(f"- {line}")

    with col1:
        st.subheader("🟢 Current")
        if not current: st.write("None found.")
        for c in current: render_person(c)

    with col2:
        st.subheader("🟡 Possible / Unconfirmed")
        if not possible: st.write("None found.")
        for p in possible: render_person(p)

    with col3:
        st.subheader("🔴 Former")
        if not former: st.write("None found.")
        for f in former: render_person(f)

with tab_building:
    st.subheader("Building Ownership & Alerts")
    if scan["owner_lines"]:
        for line in scan["owner_lines"]:
            st.markdown(f"- {line}")
    else:
        st.write("No owner-of-record / context records found for this address.")
    if scan["b_only"]:
        st.caption(f"(+{scan['b_only']} people associated with the building, unit unknown)")

with tab_map:
    st.subheader("Geospatial Migration Patterns")
    st.caption("Blue pins represent prior addresses of current residents. Green pins represent new "
               "addresses of former residents. Solid lines show migration vectors.")

    with st.spinner("Geocoding addresses via OpenStreetMap (Respecting 1-req/sec limit)..."):
        m = generate_migration_map(scan["target_str"], current, former)
        st_folium(m, width=1200, height=600)
