import streamlit as st
import os
from lookup import gather_people, gather_api_sources, score_candidates, owner_context
from resident_core import Address, normalize_address
from enrich import enrich_person, format_enrichment

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

# Default sources matching the CLI
BROWSER_SOURCES = ["FastPeopleSearch", "TruePeopleSearch", "USPhoneBook"]
API_SOURCES = ["DCProperty", "OSMContext", "DCVacant", "DCPermit", "DCBusinessLicense", "DC311", "DCCrime", "OpenFEC"]

if st.sidebar.button("Launch OSINT Scan"):
    with st.spinner("Executing multi-source scrape..."):
        units = [u.strip().upper() for u in units_str.split(",")] if units_str else []
        street_up = " ".join(street.split()).upper()
        target = normalize_address(f"{street_up}, {city.upper()}, {state.upper()} {zip_code}")
        
        status_text = st.empty()
        
        status_text.info(f"Gathering Data Brokers: {', '.join(BROWSER_SOURCES)}...")
        people = gather_people(street_up, city.upper(), state.upper(), zip_code, units, True, BROWSER_SOURCES, proxies="", delay=2.0, session_dir="")
        
        status_text.info("Querying Open Data & Context APIs...")
        people += gather_api_sources(street_up, city.upper(), state.upper(), zip_code, API_SOURCES, proxies="", fec_opts={})
        
        status_text.success("✅ Collection complete. Correlating records...")
        
        current, possible, former, b_only = score_candidates(people, target)
        
        tab_roster, tab_building = st.tabs(["👥 Resident Roster", "🏢 Building Context"])
        
        with tab_roster:
            col1, col2, col3 = st.columns(3)
            
            def render_person(p):
                age_str = f" (Age {p.age})" if p.age else ""
                with st.expander(f"{p.name}{age_str}  [Score: {p.score}]"):
                    st.caption(f"Sources: {', '.join(p.source_list)}")
                    if p.phones:
                        st.markdown("**Phones:** " + ", ".join(p.phones))
                    if p.prior_addresses:
                        st.markdown("**Prior:**")
                        for a in p.prior_addresses[:3]:
                            st.markdown(f"- {a.display()}")
                    
                    if do_enrich:
                        with st.spinner("Running deep-web enrichment..."):
                            edata = enrich_person(p.name, city, state)
                            elines = format_enrichment(edata)
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
