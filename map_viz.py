import folium
import urllib.parse
import urllib.request
import json
import time
import streamlit as st
from resident_core import Person, Address

@st.cache_data(show_spinner=False)
def geocode_address(address_str: str):
    """Hits Nominatim to get lat/lon. Cached to respect the 1 req/sec limit."""
    time.sleep(1.1)  # Strictly enforce Nominatim rate limits
    headers = {"User-Agent": "AddressResearchOSINT/1.0 (research project)"}
    params = {"q": address_str, "format": "json", "limit": "1"}
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print(f"Geocoding error for {address_str}: {e}")
    return None, None

def generate_migration_map(target_address_str: str, current: list[Person], former: list[Person]):
    """Generates an interactive Folium map showing migration patterns."""
    target_lat, target_lon = geocode_address(target_address_str)
    
    # Default to center of US if target geocoding fails
    start_lat = target_lat if target_lat else 39.8283
    start_lon = target_lon if target_lon else -98.5795
    start_zoom = 12 if target_lat else 4
    
    m = folium.Map(location=[start_lat, start_lon], zoom_start=start_zoom, tiles="CartoDB positron")
    
    if target_lat and target_lon:
        folium.Marker(
            [target_lat, target_lon],
            popup=f"<b>TARGET:</b><br>{target_address_str}",
            tooltip="Target Property",
            icon=folium.Icon(color="red", icon="home")
        ).add_to(m)

    # Plot prior addresses (Blue)
    for p in current:
        if not p.prior_addresses: continue
        for prior in p.prior_addresses[:2]:  # Map max 2 priors to avoid clutter
            prior_str = prior.display()
            lat, lon = geocode_address(prior_str)
            if lat and lon:
                folium.Marker(
                    [lat, lon],
                    popup=f"<b>Prior Address of {p.name}</b><br>{prior_str}",
                    tooltip=f"{p.name} (Prior)",
                    icon=folium.Icon(color="blue", icon="info-sign")
                ).add_to(m)
                
                if target_lat and target_lon:
                    # Draw a line from Prior to Target
                    folium.PolyLine(
                        locations=[(lat, lon), (target_lat, target_lon)],
                        color="blue",
                        weight=2,
                        opacity=0.6,
                        dash_array="5, 10"
                    ).add_to(m)

    # Plot moved-to addresses (Green)
    for p in former:
        # We assume p.current_address is where they moved TO (since they are a former resident of the target)
        if not p.current_address: continue
        fwd_str = p.current_address.display()
        if fwd_str.upper() == target_address_str.upper(): continue # Ignore if same
        
        lat, lon = geocode_address(fwd_str)
        if lat and lon:
            folium.Marker(
                [lat, lon],
                popup=f"<b>Forward Address of {p.name}</b><br>{fwd_str}",
                tooltip=f"{p.name} (Moved To)",
                icon=folium.Icon(color="green", icon="share")
            ).add_to(m)
            
            if target_lat and target_lon:
                # Draw a line from Target to Forward
                folium.PolyLine(
                    locations=[(target_lat, target_lon), (lat, lon)],
                    color="green",
                    weight=2,
                    opacity=0.6,
                    dash_array="5, 10"
                ).add_to(m)

    return m
