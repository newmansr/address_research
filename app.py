import streamlit as st
import os
import sys
from lookup import gather_people, gather_api_sources, score_candidates, owner_context
from resident_core import Address, normalize_address

st.set_page_config(page_title="Address Research OSINT", page_icon="🔍", layout="wide")

st.title("🔍 Address Research & OSINT Dashboard")

st.sidebar.header("Search Parameters")
street = st.sidebar.text_input("Street Address", "800 New Jersey Ave SE")
city = st.sidebar.text_input("City", "Washington")
state = st.sidebar.text_input("State", "DC")
zip_code = st.sidebar.text_input("Zip Code", "20003")
units_str = st.sidebar.text_input("Units (comma separated)", "")

st.sidebar.header("Modules")
do_osint = st.sidebar.checkbox("OSINT (Deep Web & Biographies)", value=True)
do_enrich = st.sidebar.checkbox("Enrich (FEC, Corp, Docs)", value=True)

if st.sidebar.button("Run OSINT Scan"):
    with st.spinner("Initializing scan..."):
        units = [u.strip().upper() for u in units_str.split(",")] if units_str else []
        street_up = " ".join(street.split()).upper()
        
        st.info("Scraping Browser Sources (TruePeopleSearch, USPhoneBook)...")
        people = gather_people(street_up, city.upper(), state.upper(), zip_code, units, True, ["TruePeopleSearch", "USPhoneBook"], proxies="", delay=2.0, session_dir="")
        
        st.info("Fetching Open Data APIs (Property, OSM, FEC)...")
        people += gather_api_sources(street_up, city.upper(), state.upper(), zip_code, ["OSMContext", "DCProperty"], proxies="", fec_opts={})
        
        st.success(f"Collected {len(people)} raw records.")
        
        target = normalize_address(f"{street_up}, {city.upper()}, {state.upper()} {zip_code}")
        current, possible, former, b_only = score_candidates(people, target)
        
        st.subheader("Current Residents")
        for c in current:
            st.write(f"**{c.name}** (Age: {c.age})")
            
        st.subheader("Building Context")
        for line in owner_context(people, target, do_unmask=do_enrich):
            st.write(line)

