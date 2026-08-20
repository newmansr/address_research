# Next Steps & Operational Notes

Status as of 2026-08-20. This captures where the project stands after the deployment/debug
sprint, the open problems, and the prioritized backlog. Companion to `README.md` (architecture)
and `PROJECT_PLAN.md` (design history).

---

## Current status — what works

The full pipeline runs end-to-end on the mini-PC (zeus) Docker deployment:

- **GUI** (`app.py`) loads over Tailscale Serve at `https://osint.tail9128d0.ts.net` and results
  persist across reruns (no more blank screen).
- **Open-data APIs** (`apis.py`) always run and return: DC property owner-of-record, permits,
  crime, vacancy, business licenses, 311, FEC, OSM building context.
- **People-search brokers** (SeleniumBase real browser) work when routed through a clean cellular
  IP: FastPeopleSearch / TruePeopleSearch / USPhoneBook clear Cloudflare, crawl detail pages, and
  parse residents. Scoring classifies them per-unit into Current / Possible / Former with a
  move-chain (where former tenants went).
- Validated against `800 New Jersey Ave SE #837`: correctly identified the unit as an **LLC-owned
  rental** (NEW JERSEY AT H LLC) with **no current occupant in the databases** and 4 former tenants
  with their new addresses. That is a correct, complete result — not an empty one.

---

## Priority 1 (URGENT) — Fix the egress architecture

**Problem.** To dodge Cloudflare's IP block, we routed zeus through a phone Tailscale **exit node**.
`tailscale set --exit-node=<phone>` sends **100% of zeus's outbound internet traffic** through the
phone's cellular link. That degrades/breaks every other homelab service on zeus (Immich, Beszel,
the NBA app, CC Offers, UBM), burns mobile data, and fully depends on the phone staying awake on
cellular.

**Immediate mitigation (do now):**
```bash
tailscale set --exit-node=
```
Restores normal home egress for the whole box. The scraper is blocked again (home IP is
Cloudflare-flagged for the people-search sites), but the homelab is healthy.

**Proper fix — route ONLY the scraper through cellular, not all of zeus.** The people-search sites
are the only thing that needs the clean IP; the DC open-data APIs work fine from the home IP. So:

1. Leave zeus on its **normal home connection** (no exit node).
2. Run a **SOCKS5 proxy on the phone** (on cellular), reachable over Tailscale
   (`socks5://<phone-tailscale-ip>:<port>`). Android options: "Every Proxy", Termux + a SOCKS
   server. Keep it on the tailnet.
3. Wire the app to send **only** the people-search traffic through that proxy. The code already
   supports it end-to-end — `fetch_thatsthem(..., proxy=)`, `fetch_browser_sources(..., proxies=)`
   (→ `SB(proxy=)`), and `apis._http_get(..., proxy=)` — but `app.py` currently hardcodes
   `proxies=""`. Add a `SCRAPE_PROXY` env var (read from `.env`, consistent with the homelab
   pattern) and/or a sidebar field, and pass it to `gather_people` (brokers + ThatsThem) while
   leaving `gather_api_sources` **direct** (gov APIs don't need it).

   - curl_cffi: use `socks5h://host:port` (remote DNS) in the proxies dict.
   - SeleniumBase/Chrome: `--proxy-server=socks5://host:port` via the existing `proxy=` arg.

**Alternative to the phone SOCKS proxy:** a paid **residential/mobile proxy service**
(host:port + user:pass) is set-and-forget, needs no phone, and drops straight into the same
`SCRAPE_PROXY` plumbing. Trade money for reliability.

**Net effect either way:** zeus's homelab traffic stays on the home connection; only the scraper
uses the cellular/residential exit. No more collateral damage.

---

## Priority 2 — ThatsThem parser yields 0 from a full page

The browser retry of ThatsThem returns **~471,000 chars** (the whole-building page) but
`parse_thatsthem` extracts **0 residents**. ThatsThem is the building-wide source (every unit's
associated people), so this is the biggest coverage lever left.

- Likely cause: the JSON-LD in the **browser-rendered DOM** (`sb.cdp.get_page_source()`) differs
  from the raw-HTTP JSON-LD the parser (`_iter_jsonld_persons`) was tuned on — different `@graph`
  shape, escaping, or the Person nodes not where the regex/`json.loads` expects.
- Next action: pull a fresh capture from the container
  (`/app/raw/*ThatsThem*.html`) and inspect the `application/ld+json` block structure
  (`grep -c 'application/ld' <file>`, `grep -o '@type":"[A-Za-z]*"' <file> | sort | uniq -c`), then
  adjust `parse_thatsthem` to match. May reveal residents the unit-specific searches miss.

---

## Priority 3 — Reliability & data-quality polish

- **FastPeopleSearch flakiness:** yielded 2 residents one run, 0 the next ("0 residents parsed —
  blocked/stub"). The challenge/detail-crawl isn't deterministic. Investigate whether it's a
  not-cleared challenge (add a retry/settle) or a parser drift on the browser text.
- **TPS address field carries Zillow-style junk:** e.g.
  `55 M St NE #1000 Washington, DC 20002 $658,000 | 1,264 Sq Ft | Built 2018`. The detail-page
  parser is grabbing property metadata into the address. `normalize_address` still extracts the
  core so matching works, but it pollutes display and map geocoding — trim to the address.
- **`duckduckgo_search` is deprecated → `ddgs`.** Enrichment still runs (RuntimeWarnings only), but
  migrate `enrich.py` to `pip install ddgs` / `from ddgs import DDGS` and update the `proxies=`
  kwarg to the new API. Update `requirements.txt`.

---

## Priority 4 — GUI/UX

- **Multi-unit scoring:** the GUI scores against the **first** entered unit only. For a real
  multi-unit sweep it should score/report per-unit like the CLI's roster.
- **Lazy enrichment:** enrichment is now off-by-default and bounded to the top 6 leads, but the
  ideal is to render the roster instantly and enrich a person **on demand** (per-person button,
  cached) so deep-web lookups never delay results.
- Consider a **from-cache** toggle (the `raw/` captures + `gather_from_cache` already exist) so
  re-scoring doesn't require a fresh multi-minute scrape.

---

## Priority 5 — Broader code audit (not yet reviewed)

The debug sprint covered `app.py`, `scoring.py`, `apis.py`, `sources.py`, `resident_core.py`, and
the Docker setup. Still unreviewed for bugs:

- `parsers.py` (beyond `parse_thatsthem`) — detail-page and summary parsers.
- `history.py` — the SQLite delta/history tracking used by the CLI.
- `graph.py`, `dossier.py`, `llm_parser.py`, `enrich.py` (full pass), `export.py`.

---

## Operational runbook

- **Deploy:** on zeus, `cd ~/address_research && ./deploy.sh` (git pull + `docker-compose up -d
  --build`). Reach the GUI by its Tailscale Serve name, not an IP.
- **Egress:** keep zeus on its normal connection; once Priority 1 lands, only the scraper uses the
  proxy. Do NOT leave a whole-host exit node on — it breaks the other homelab services.
- **Scanning:** open-data APIs run always; tick **Browser scrapers** only when the scraper proxy is
  live; leave **Deep Web Enrichment** off unless you specifically want it (it's slow).
- **Docker base image:** `python:3.11-slim` tracks Debian bookworm — do not reintroduce
  `libgconf-2-4` or `apt-key` (both removed). Chrome installs from its official `.deb`; Xvfb needs
  `xauth`; SeleniumBase/PyAutoGUI needs `libtk8.6 libtcl8.6 scrot`.

---

## Changelog — this session's fixes

- Docker build fixed for Debian bookworm (dropped `libgconf-2-4`, Chrome via official `.deb`,
  added `xauth`, `libtk8.6`/`libtcl8.6`/`scrot`).
- GUI exposed via Tailscale Serve on loopback bind (per homelab standard); CORS/XSRF disabled for
  the proxy.
- Fixed `format_enrichment` call, `ScoredCandidate` field mismatch (roster/map crash), and the
  4 dead open-data APIs (`street_key`/`unit_key` → `match_address`).
- Results persisted in `st.session_state` (fixed the `st_folium` blank-screen).
- Scan made resilient: APIs first, browser sources off-by-default under a 900s timeout.
- Scoring target now includes the entered unit; debug expander shows parsed addresses.
- Enrichment off-by-default and bounded so it can't block the roster.
