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

if st.sidebar.button("Launch OSINT Scan"):
    with st.spinner("Executing multi-source scrape..."):
        units = [u.strip().upper() for u in units_str.split(",")] if units_str else []
        street_up = " ".join(street.split()).upper()
        target_str = f"{street_up}, {city.upper()}, {state.upper()} {zip_code}"
        target = normalize_address(target_str)
        
        status_text = st.empty()
        
        # Open-data APIs first: independent, browser-free, and can't be blocked by Cloudflare, so
        # they always return something even if every people-search broker is blocked/deadlocked.
        status_text.info("Querying open-data & context APIs (DC property, permits, crime, FEC)...")
        people = gather_api_sources(street_up, city.upper(), state.upper(), zip_code, API_SOURCES, proxies="", fec_opts={})

        # People-search brokers. ThatsThem is browser-free; the rest need SeleniumBase. The browser
        # path can deadlock at Chrome launch inside a headless container, so when it's enabled we run
        # it under a timeout and fall back to the open-data results instead of hanging the whole scan.
        brokers = ["ThatsThem"] + (BROWSER_SOURCES if use_browser else [])
        status_text.info(f"Gathering people-search brokers: {', '.join(brokers)}...")

        def _gather_brokers():
            return gather_people(street_up, city.upper(), state.upper(), zip_code, units,
                                 use_browser, BROWSER_SOURCES, proxies="", delay=2.0, session_dir="")

        if use_browser:
            import concurrent.futures
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            fut = ex.submit(_gather_brokers)
            try:
                people += fut.result(timeout=240)
            except concurrent.futures.TimeoutError:
                status_text.warning("Browser scrapers timed out (SeleniumBase likely deadlocked in "
                                    "Docker) — showing open-data results only.")
            finally:
                ex.shutdown(wait=False)
        else:
            people += _gather_brokers()
        
        status_text.success(f"✅ Collection complete. Found {len(people)} raw records. Correlating...")
        
        with st.expander("🛠️ Debug: Raw Scraped Records (Before Correlation)"):
            if not people:
                st.error("ZERO records were returned by the scrapers. SeleniumBase likely crashed.")
            else:
                for p in people:
                    st.text(f"Source: {p.source} | Name: {p.name}")

        current, possible, former, b_only = score_candidates(people, target)
        
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

                    if do_enrich:
                        with st.spinner("Running deep-web enrichment..."):
                            edata = enrich_person(p.name, city, state)
                            elines = format_enrichment(p.name, edata)
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
            for line in owner_context(people, target, do_unmask=do_enrich):
                st.markdown(f"- {line}")
                
        with tab_map:
            st.subheader("Geospatial Migration Patterns")
            st.caption("Blue pins represent prior addresses of current residents. Green pins represent new addresses of former residents. Solid lines show migration vectors.")
            
            with st.spinner("Geocoding addresses via OpenStreetMap (Respecting 1-req/sec limit)..."):
                m = generate_migration_map(target_str, current, former)
                st_folium(m, width=1200, height=600)

