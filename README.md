# Address Research & OSINT Platform

A highly advanced, multi-source OSINT (Open Source Intelligence) platform designed to aggregate, deduplicate, and enrich property and resident data. It bypasses anti-bot protections to scrape data brokers, queries open data APIs, and uses local LLMs and deep-web Dorking to generate comprehensive intelligence dossiers.

## 🏗 System Architecture

The codebase is strictly modularized to separate data extraction, parsing, correlation, and presentation. 

### Core Pipeline
1. **Input:** The target address is passed via CLI (lookup.py) or the Web GUI (app.py).
2. **Extraction (sources.py & apis.py):** The engine fires off concurrent requests to JSON APIs and spins up a headless SeleniumBase Chrome browser to bypass Cloudflare and scrape HTML data brokers.
3. **Parsing (parsers.py):** Raw HTML/JSON is parsed into unified Person data objects.
4. **Correlation (scoring.py):** A weighted scoring algorithm evaluates all Person records against the target address, resolving aliases and deduplicating records to classify them as Current, Possible, or Former residents.
5. **Enrichment (enrich.py):** High-confidence residents are routed through deep-web DuckDuckGo searches to find social media profiles, public PDFs, and court records.
6. **Presentation:** Data is output to the Terminal, an HTML dossier (dossier.py), or the interactive Streamlit Web UI (app.py).

---

## 📁 File Structure & Developer Guide

For future AI agents or developers, here is the exact mapping of where logic lives:

### 1. Data Models (resident_core.py)
- Defines the Person and Address dataclasses.
- **Rule for AI:** Do not change the Person schema without updating parsers.py, scoring.py, and app.py.

### 2. Browser Scraping Engine (sources.py)
- Uses SeleniumBase (CDP mode) to bypass PerimeterX and Cloudflare Turnstile.
- **Rule for AI:** If a new data broker requires JS rendering or anti-bot bypass, add the URL builder here and append it to BROWSER_SOURCES. 

### 3. API Fetching (apis.py)
- Uses urllib or curl_cffi to fetch data from Open Data endpoints (DC Property, Nominatim, FEC).
- **Rule for AI:** To add a new public API, write a fetch_* function here returning a list[Person] and register it in the API_SOURCES dictionary at the bottom of the file.

### 4. Parsers (parsers.py)
- Contains Regex and BeautifulSoup logic to turn raw HTML/JSON into Person objects.
- **Rule for AI:** Every source added to sources.py MUST have a matching parse_* block in the parse_source factory function.

### 5. Scoring & Deduplication (scoring.py)
- The brain of the tool. Deduplicates people based on exact name string matching.
- Scores records based on source reliability.
- **Rule for AI:** If you add a source that provides Context (e.g., Crime, Permits, Building Type) rather than Resident data, you MUST add the source name to the CONTEXT_SOURCES set in this file so it isn't treated as a human resident.

### 6. Deep Web Enrichment (enrich.py)
- Uses duckduckgo-search to perform Google Dorking (ext:pdf, site:linkedin.com).
- **Rule for AI:** When adding new enrichment modules, ensure they fail gracefully (return empty lists) rather than crashing the pipeline.

### 7. User Interfaces (lookup.py & app.py)
- lookup.py: The robust command-line interface.
- app.py: The Streamlit Web UI. 

---

## 🚀 Deployment (Docker & Tailscale)

This project is configured to be deployed on a headless Linux server (e.g., mini-pc) running securely on a Tailscale Tailnet.

**Network Architecture:**
- **Inbound:** Streamlit binds exclusively to the Tailscale IP (TAILSCALE_IP in .env). The GUI is invisible to the public internet.
- **Outbound:** The Docker container routes scraping traffic through the host machine. A Cellular connection (via mobile hotspot or tethered exit node) is highly recommended for scraping, as Cellular CGNAT IPs are rarely blocked by Cloudflare compared to commercial VPNs.

### Deployment Commands

Create a .env file containing your Tailscale IP:
echo "TAILSCALE_IP=100.x.y.z" > .env

Deploy using the provided script:
./deploy.sh

*(The Dockerfile utilizes Xvfb to create a virtual display server. This is mandatory, as SeleniumBase requires a display to physically move the virtual mouse and click Cloudflare Turnstile checkboxes).*

---

## 🤖 Note for Future AI Agents

1. **Do not break the CLI:** When updating app.py, ensure the underlying functions in lookup.py still work for terminal users.
2. **Prioritize Free Data:** The core philosophy of this tool is to use OSINT and web scraping to bypass paid API gateways. Always attempt to use DuckDuckGo search parsing or SeleniumBase scraping before suggesting a paid API key.
3. **Respect Rate Limits:** If adding a new open data API (like OpenStreetMap), ensure you implement caching or respect standard 1-request-per-second rate limits to avoid getting the user's IP banned.
