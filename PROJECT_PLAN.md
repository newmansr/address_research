# Address Research — Project Plan & Progress

**Goal:** Input an address (single unit or a range of units in a building) → output the
**most likely current resident(s)**, with a confidence level and supporting evidence.

**Constraints (decided 2026-06-28):**
- Target = **current resident** (not legal owner).
- **Free scraping only** — no paid data APIs.
- **Single lookups**, with the option to input a **range/list of unit numbers** in one building.

---

## Architecture direction

The highest-value source (USPhoneBook) returns **already-structured** blocks
(`Name, Age` / `Lives at ...` / `Prior addresses:` / `Relatives:`). The original script
fed raw page text to an 8B local LLM (`llama3`) to decide who is current — which produced
errors (e.g. listing a Unit 938 resident as living in Unit 637).

**New approach:** parse the structured data deterministically, classify current vs former by
**unit-exact address matching**, score/rank candidates with corroboration + recency signals,
and demote the LLM to an *optional* plain-English summarizer over the structured records.

---

## Phase 5 — "Possible / unconfirmed" leads & the data-staleness limit  ✅ DONE (2026-06-28)
Real-world miss surfaced: unit **837** is occupied by **Samantha Karlin**, but the tool reported
"no current resident." Investigation: Samantha IS in USPhoneBook + CBC, but they list her current
address as her **previous** home (`800 4th St SW Apt N414`) — the free databases lag on recent
move-ins. The unit search returns 4 people (Samantha, Edmond Leber, Brent Maheux, Ryan Mckeown=former)
**all** showing addresses elsewhere, so there's **no structural signal** that uniquely identifies her.
This is a genuine free-data freshness limitation, not a parser bug.
- [x] Reverted a noisy in-progress `IMPLICIT_UNIT` experiment (it resurfaced Amy/938 as a 637
      candidate — a regression).
- [x] Added a `Person.searched_unit` tag (which unit's source search returned this person).
- [x] **"Possible (unconfirmed)" tier** (`score_candidates` now returns `current, possible, former,
      building_only`): people a unit-specific search returned but whose listed current address is
      elsewhere. Shown **only when there's no confirmed current resident**, clearly labeled as leads
      (recent move-in the DB hasn't updated, or a former). Surfaces Samantha (w/ phones) for 837;
      stays hidden for 637 (Tenkhoff confirmed) so no noise. Added a "Possible (unconfirmed)" export
      column. No regression on saved files.
- Honest takeaway: free sources can miss very recent move-ins; paid/real-time data (out of scope)
      or the surfaced leads are the only recourse.

## Phase 1 — Deterministic extraction & matching  ✅ DONE (2026-06-28)
Biggest accuracy gain; no scraping changes required.
- [x] `resident_core.py`: `Person` model, address normalizer, unit-exact matcher, classifier.
- [x] Per-source parser for **USPhoneBook** (highest yield) → structured `Person` records (`parsers.py`).
- [x] Classifier rule: current-address matches target **unit** → current; target only in
      prior list → former; otherwise → noise/relative.
- [x] `validate_saved.py`: re-parse the 8 existing `results_*.txt` files as a regression test
      set and print structured current/former per unit.

**Validation results (8 saved files):**
| Unit | Engine verdict (current) | Notes |
|---|---|---|
| 637 | Kathryn P Tenkhoff | **Fixed** old LLM bug (it wrongly added Amy Williams of Unit 938). |
| 638 | Sydney Young | + Arielle Miller correctly flagged former. |
| 737 | Russell Hartley, Jessica Pudlo | two current candidates. |
| 837 | (none) | Correct — USPhoneBook returned only unrelated people. |
| 839 | (none) | Jason Seyler correctly former. |
| 937 | (none) | Correct — no unit resident in source data. |
| 1104 | Carney Clegg | + Jason Dijak former. |
| 1105 | Jessica Anderson, Samuel Newman | + Kimberly Dixson former (move-chain confirmed). |

Takeaways: where the source actually contains a unit resident we now extract it unit-exact and
never invent one. The remaining "(none)" cases are **single-source data gaps** → addressed by
Phase 3 (more sources) + Phase 2 (scoring/recency).

## Phase 2 — Scoring, corroboration & recency ranking  ✅ DONE (2026-06-28)
- [x] **Cross-source merge** (`scoring.py`): combines the same person across sources by
      first+last name; unions phones, ages, sources. Aggregates **each source's strongest signal
      once**, so duplicate captures can't inflate a score.
- [x] **Confidence score** per candidate from weighted signals:
      USPhoneBook current `+100` / former `-70`; ThatsThem current `+45`; TPS/CBC current `+55`;
      `+30` corroboration when ≥2 sources agree. Confidence = High (≥100) / Medium (≥40) / Low.
- [x] **Move-chain / recency**: a USPhoneBook *former* verdict pushes a candidate down to the
      "former (moved out)" list; a confirmed current resident **supersedes** ThatsThem-only names
      for the same unit (`-50`, flagged "likely prior tenant").
- [x] **Rank → most likely current resident** + alternates + confidence + evidence lines.
      Building-only associations (unit unknown) are excluded from candidates and shown as a count.
- [x] **Optional LLM summarizer** (`summarize.py`, `--llm`): narrates the **structured, already
      scored** result via local Ollama. The LLM no longer *decides* anything (that was the original
      bug); it only writes prose, and returns None gracefully if Ollama is off.

**Verified (live + synthetic):** 637 → Tenkhoff *High* (USPhoneBook); 1104 → the Bernards *Medium*
(ThatsThem, with phones); a person confirmed by both sources → *High* (score 175, corroboration);
a ThatsThem-only name competing with a confirmed current → correctly superseded/dropped; a
USPhoneBook prior-address person → "former". Duplicate captures de-dup to a single signal.

## Phase 3 — Fix & consolidate scrapers (reliability)  ✅ DONE (2026-06-28)
**Four working sources w/ corroboration:** ThatsThem (requests), USPhoneBook (CDP),
CyberBackgroundChecks (CDP), TruePeopleSearch (CDP, Turnstile-solved, corroboration-level).
Live+offline verified: 637 → Tenkhoff *High* (UPB+CBC+TPS, 3-source corroboration); 1104 →
Rahsaan G Bernard *High* (ThatsThem+UPB+CBC, 4 phones). Parsers are whitespace-agnostic
(`_collapse`) — handle CDP single-line AND old clean text (no regression).

- [x] **Fix ThatsThem** — big win. Correct URL is one dash-joined segment
      `/address/{Street}-{City}-{ST}-{ZIP}`. **No browser / no Cloudflare** — plain `requests`.
      Returns the WHOLE building's residents as **JSON-LD `Person` records** (name, unit,
      phone numbers, relatives) in a single call. Verified live (70 people @ 800 NJ Ave SE).
- [x] **Consolidate browser stacks → 1** (SeleniumBase UC, the engine that actually worked
      for USPhoneBook). `nodriver` + `undetected_chromedriver` paths retired in the new pipeline.
- [x] **Raw archival** — every fetch saved to `./raw/` (`archive_raw()`), so parsers can be
      re-tuned offline without re-scraping.
- [x] **Classifier precision fix** — added a `BUILDING` (unit-unconfirmed) bucket so a unit-less
      building association no longer floods every unit's "current" list. (Found via ThatsThem,
      benefits all sources.) No regression on the 8 saved USPhoneBook files.
- [x] **USPhoneBook browser fetch — VERIFIED LIVE on user's desktop (2026-06-28).** SeleniumBase
      UC bypassed Cloudflare and scraped the structured "People Living at" block; parser correctly
      extracted Kathryn Tenkhoff as current resident of Unit 637. (chromedriver auto-updates to the
      installed Chrome — the old hardcoded `version_main=145` problem is gone.)
- [x] **Fixed multi-unit overwrite bug** — `fetch_browser_sources` returned a dict keyed by source
      name, so a second unit's page overwrote the first. Now returns a list of
      `{name, unit, text}`. Verified: 637 → Tenkhoff + 1104 → Bernards merge correctly.
- [x] **URL builders + fetch/archive wired for all 3 browser sources** (`BROWSER_SOURCES` in
      `sources.py`): USPhoneBook, TruePeopleSearch, CyberBackgroundChecks. URL formats confirmed
      via search (CBC: `/address/{street}-apt-{unit}/{city}/{state}`; TPS: `resultaddress` endpoint).
      `lookup.py --sources` selects which to run; all three fetch + archive to `raw/` on a
      `--browser` run.
- [x] **Robust block detection** — browser fetch detects Cloudflare interstitials / withheld-
      results shells (`_looks_blocked`: <1200 chars or known challenge phrases) and **skips them**
      instead of scraping a shell.
- [x] **Switched browser fetch to SeleniumBase CDP Mode** (`_clear_cloudflare`). The previous
      `uc_open_with_reconnect` + single click only clicked once and never re-clicked when Cloudflare
      cycled (user-observed). CDP mode keeps the page human (often passes with no click) and the new
      loop **re-clicks every iteration** (mouse via `uc_gui_click_captcha`, keyboard fallback
      `uc_gui_handle_captcha`), up to 6×, with per-attempt logging.
- [x] **CDP-mode bypass — CLEARED Cloudflare.** Fixed two follow-on bugs it exposed: (1) navigate
      later URLs with `sb.cdp.open()` (re-calling `activate_cdp_mode` didn't navigate); (2) parse
      from newline-preserving `sb.get_text()` / whitespace-agnostic parsers (CDP text is single-line).
- [x] **CyberBackgroundChecks parser — DONE & verified.** `parse_cyberbackgroundchecks`: records end
      `VIEW DETAILS`; extracts name, age, `Lives at` current address, `Used to live` priors, and
      **phone numbers**. Registered + scoring weight active. 637/1104 corroborate Tenkhoff/Bernard.
- [x] **USPhoneBook parser — made whitespace-agnostic** (`_collapse` + split on `View Report` +
      `_clean_name` to strip the duplicated "First Last" heading). Works on CDP single-line text and
      old saved files alike.
- [x] **TruePeopleSearch — WORKING (2026-06-28).** Earlier "hCaptcha" read was wrong: the page JS is
      `turnstile.render({sitekey:'0x4AAAAAAAmywfqBst8n7ro5', action:'900', callback→submitForm})` —
      **Cloudflare Turnstile** (`#h-captcha` id is a red herring), exactly what `uc_gui_click_captcha()`
      handles. It slipped through because the loading page is 8342 chars, so `_looks_blocked` (<1200)
      treated it as valid. **Fix:** `_is_interstitial`/`_needs_clearing` detect the Turnstile/loading
      interstitial (`turnstile.render`, "loading content", "automatic submission failed"); the clear
      loop solves it (cleared in 2 tries live) and waits for the form-submit redirect to results.
      `parse_truepeoplesearch` written + verified (637 list incl. Tenkhoff).
      Initially treated as association-only (summary page lacks unit address).
- [x] **TruePeopleSearch — PROMOTED TO AUTHORITATIVE (2026-06-28).** Orchestrator now crawls each
      person's `/find/person/...` detail page (links scraped from the cleared results page; session
      cookie from the first Turnstile solve usually lets detail pages load without re-challenge).
      `_crawl_tps_details` fetches up to `TPS_MAX_DETAILS=8` detail pages/unit and archives them as
      `TruePeopleSearchDetail`. `parse_truepeoplesearch` rewritten to parse the detail page
      (`Current Address` / `Previous Addresses` / phones) and returns [] for a non-detail page.
      Scoring: TPS → `CONFIRMING_SOURCES`, weight **90**, FORMER −50. Also added street-prefix
      tolerance in `match_address` (`_street_matches`) for sources that omit the street/city comma.
      **Decisive win:** a person who appears in the unit search but whose *detail* current address is
      a different unit (e.g. Amy Williams → 938) is now correctly NOT current at the searched unit.
- [x] **VERIFIED against real detail captures (2026-06-28).** Live crawl fetched 4/4 detail pages.
      Real data section is anchored by "…most recently reported address for <Name>. <full addr>
      <County> (dates)" (the first `Current Address` is just a TOC link). Parser tuned to that anchor
      (multi-word county stop, e.g. "District Of Columbia County"). Result: 637 → Tenkhoff *High*,
      corroborated by **USPhoneBook + CBC + TPS** (3 sources); Amy Williams (detail #938) correctly
      excluded from 637. TPS is now genuinely authoritative. (TPS phone extraction still empty — minor,
      other sources supply phones.) Detail crawl adds up to 8 page loads/unit (slower on ranges).

**Reliability map (tested live 2026-06-28):**
| Source | Needs browser? | Status |
|---|---|---|
| **ThatsThem** | ❌ No (requests) | ✅ Working — whole-building, phones+relatives |
| **USPhoneBook** | ✅ Cloudflare | ✅ **Verified live** — SeleniumBase UC + parser |
| **TruePeopleSearch** | ✅ 403 bot-block | 🟡 Fetch+archive wired; parser pending a capture |
| **CyberBackgroundChecks** | ✅ Cloudflare | 🟡 Fetch+archive wired; parser pending a capture |
| FastPeopleSearch | ✅ manual | ⬜ Was failing; low priority |

**Note:** Unit 1105 at **800** NJ Ave SE correctly returns no data — unit 1105 belongs to the
**880** building (different street number). Validates that matching is genuinely number-specific.

## Phase 4 — Unit-range batch mode  ✅ DONE (2026-06-28)
- [x] `--units` accepts a single unit, comma list (`637,737,837`), or range (`1101-1110`);
      `parse_units()` expands ranges. ThatsThem is fetched once per building and reused for every
      unit (ranges are essentially free on the browser-free source).
- [x] **`--out FILE.csv|.xlsx`** writes one row per unit (`export.py`): Unit, Most Likely Resident,
      Confidence, Age, Phones, Corroborating Sources, Score, Other Candidates, Former Residents,
      Building-only count. CSV uses stdlib + UTF-8 BOM (clean Excel open); XLSX uses openpyxl with a
      bold + frozen header row and set column widths. Format chosen by file extension.
- [x] Verified live: `--units 1037,1038,1104,1105 --out units.csv/.xlsx` → correct per-unit rows.
- [n/a] Randomized delays: ThatsThem is one fetch/building; browser sources already pace via the
      CDP challenge-clearing waits. Can add jitter later if a source rate-limits.

---

## Known issues found in current outputs (2026-06-28 review)
- Only **USPhoneBook** returns usable data; TPS/FPS/ThatsThem all failed in the saved runs.
- LLM mis-classified a Unit 938 resident as a current resident of Unit 637 (unit ignored).
- Three different browser-automation libraries in use — fragile.

## Files
- `resident_core.py` — address normalization, unit-exact matching, Person model, classifier
  (CURRENT / BUILDING / FORMER / OTHER).
- `parsers.py` — per-source → `Person` records (USPhoneBook text + ThatsThem JSON-LD).
- `sources.py` — fetch layer: browser-free ThatsThem (requests) + consolidated SeleniumBase UC
  for Cloudflare sources; raw archival to `./raw/`.
- `export.py` — Phase 4 CSV/XLSX writer; one row per unit (`build_row`, `write_results`).
- `scoring.py` — Phase 2 cross-source merge, weighted scoring, corroboration, supersession,
  ranking → `ScoredCandidate`s (most likely current resident + former list).
- `summarize.py` — optional Ollama summary over structured results (`--llm`); degrades to None.
- `lookup.py` — orchestrator (CLI). Browser-free by default; `--browser` adds Cloudflare sources;
  `--units` accepts single / list / range; `--llm` adds a plain-English summary.
- `validate_saved.py` — regression-tests the engine against saved `results_*.txt`.
- `raw/` — archived raw captures (created on first run).
- `address_research.py` — ORIGINAL script (3 browser stacks + LLM). Superseded by the modular
  pipeline above; kept for reference / its manual FastPeopleSearch flow. To be retired.

## Changelog
- **2026-06-28** — Reviewed existing script + 8 saved outputs. Locked scope (resident / free /
  single+range). Created this plan.
- **2026-06-28** — Phase 1 complete: built deterministic extraction/matching engine + USPhoneBook
  parser + regression validator. Confirmed the Unit-637 misclassification is fixed and no false
  current-residents are produced across the 8 saved runs.
- **2026-06-28** — Phase 3 (mostly): fixed ThatsThem (browser-free, whole-building JSON-LD with
  phones/relatives — verified live); consolidated browser stacks to SeleniumBase UC; added raw
  archival; added `lookup.py` orchestrator with unit list/range; fixed classifier precision
  (BUILDING bucket). Remaining: live-test browser sources on a desktop + write CyberBackgroundChecks
  parser from a real capture.
- **2026-06-28** — Phase 3 browser path **verified live** on the user's desktop: SeleniumBase UC
  bypassed Cloudflare and scraped USPhoneBook (637 → Tenkhoff). Fixed a multi-unit overwrite bug in
  `fetch_browser_sources` (now returns a list). USPhoneBook source is now ✅ done. Remaining Phase 3:
  TruePeopleSearch live test + CyberBackgroundChecks parser.
- **2026-06-28** — Phase 2 complete: `scoring.py` (cross-source merge, weighted confidence,
  corroboration, move-chain/supersession, ranking) + `summarize.py` (optional Ollama narration over
  structured results). Verified live and with synthetic cases. The LLM is now narration-only — the
  original "LLM decides who's current" bug class is fully removed.
- **2026-06-28** — Phase 3 wiring: added TruePeopleSearch + CyberBackgroundChecks URL builders
  (formats confirmed via search) and wired all 3 browser sources into `lookup.py` (`--sources`).
  Their parsers are blocked on one `--browser` capture (Cloudflare blocks all browser-free probes).
- **2026-06-28** — First multi-source live run: **corroboration confirmed working** — Unit 1104 →
  Rahsaan G Bernard *High* confidence (ThatsThem + USPhoneBook agree, +phones). CBC hit the
  Cloudflare interstitial, TPS returned only chrome. Added block detection so shells are skipped.
- **2026-06-28** — User reported the Cloudflare "verify you are human" box gets clicked once but
  never re-clicked when it cycles. **Rewrote the browser fetch on SeleniumBase CDP Mode**: re-clicks
  each loop (mouse + keyboard fallback), 6 attempts, logged.
- **2026-06-28** — CDP rewrite **cleared the challenge** (CBC/TPS returned 50k+ chars) but exposed
  two bugs: (1) `activate_cdp_mode(url)` only navigates once, so CBC/TPS stayed on the USPhoneBook
  page; (2) `sb.cdp.get_text()` strips newlines, breaking the line-based USPhoneBook parser. Fixed:
  navigate later URLs with `sb.cdp.open()`; made parsers whitespace-agnostic.
- **2026-06-28** — **Phase 3 COMPLETE.** From real CDP captures: rewrote `parse_usphonebook`
  (split on `View Report`, `_clean_name` strips duplicated heading) and wrote
  `parse_cyberbackgroundchecks` (split on `VIEW DETAILS`; name/age/current/priors/**phones**).
  Verified offline against archived captures: 637 → Tenkhoff *High* (UPB+CBC); 1104 → Rahsaan G
  Bernard *High* (ThatsThem+UPB+CBC, 4 phones). No regression on saved files. TPS dropped (hCaptcha).
  Three corroborating free sources now live.
- **2026-06-28** — **Phase 4 COMPLETE.** Added `export.py` + `--out FILE.csv|.xlsx` for one-row-per-
  unit batch output; `--units` ranges (`1101-1110`) expand and reuse the single ThatsThem fetch.
  Verified live. **All four phases done.**
- **2026-06-28** — **TruePeopleSearch revived as a 4th source.** Discovered its wall is Cloudflare
  Turnstile (not hCaptcha); fixed interstitial detection so the clear loop solves it (cleared live
  in 2 tries). Wrote `parse_truepeoplesearch`. TPS results page is summary-only (no unit address),
  so it's an association/corroboration source (weight 40, supersedable). 637 → Tenkhoff now
  corroborated by 3 sources. On by default (+~30s/unit).
- **2026-06-28** — **TPS made AUTHORITATIVE.** Added `_crawl_tps_details`: scrape `/find/person/`
  links from the results page, fetch up to 8 detail pages/unit, parse `Current Address` for the real
  unit-level address. TPS → confirming source (weight 90). Added `_street_matches` prefix tolerance
  so a glued street/city ("…SE Washington") still matches.
- **2026-06-28** — **TPS authoritative VERIFIED on real detail captures.** Live crawl got 4/4 detail
  pages; tuned the parser to the real "most recently reported address for <Name>. <addr>" anchor.
  637 → Tenkhoff *High*, corroborated by UPB+CBC+TPS; Amy Williams (detail #938) correctly excluded
  from 637. No regression on saved files. **All 4 sources now authoritative-or-corroborating.**
- **2026-06-28** — **Phase 5: data-staleness handling.** Diagnosed the 837/Samantha Karlin miss as
  free-database lag (her current address still shows her old home; the unit search returns 4
  address-elsewhere people with no way to rank her above them). Reverted the noisy `IMPLICIT_UNIT`
  experiment (regressed 637). Added `searched_unit` tagging + a "Possible (unconfirmed)" lead tier
  shown only when no confirmed current exists — surfaces Samantha (w/ phones) for 837, stays clean
  for 637. New export column. No regression.
- **2026-06-28** — **ThatsThem rate-limit handling.** Heavy dev testing tripped ThatsThem's
  Cloudflare "too many requests" wall (HTTP 200 + 80KB block page that silently parsed to 0).
  `fetch_thatsthem` now uses JSON-LD presence as the success signal, detects the rate-limit/
  challenge, retries with backoff, and prints a clear "unavailable (rate-limited); other sources
  still ran" message instead of a silent 0. Recovers on its own after a cooldown.
- **2026-06-28** — **837 deep-dive conclusion.** Of the 3 leads: Brent Maheux has the building
  (no unit) in his priors (former building resident); Edmond Leber has NO connection to the
  building (search-engine noise); Samantha Karlin (the real resident) has ZERO footprint at
  800 NJ Ave SE in any free source. So the free data cannot rank her #1 — she's only surfaced
  because the search backend returned her. Confirmed ceiling of free data for recent move-ins.

## Phase 6 — Add FastPeopleSearch (user reports high accuracy)  ✅ DONE (2026-06-29)
**FPS is live as the 5th source (4th authoritative).** Live-verified: 637 → Tenkhoff *High*,
corroborated by **4 sources** (UPB+CBC+TPS+FPS) with phones. FPS detail page format differs from
TPS — `Current Address (Since <date>) <addr> <County> Full Name: <Name>` — parser tuned to it
(strip trailing "Phone Numbers" from the Full Name). Phone extraction fixed for FPS **and** TPS
(skip the table-of-contents "Phone Number" that has no digits). Crawled relatives' detail pages
correctly classify as "other". No regression on saved files.

FPS is the sister site of TruePeopleSearch (shared data backend); user finds it very accurate.
It was in the original script but failed (old manual Cloudflare flow → ERR_CONNECTION_CLOSED).
- [x] `fastpeoplesearch_url()` + wired into `BROWSER_SOURCES` (CDP browser path, like CBC/TPS).
      URL: `/address/{street}[-unit-{unit}]_{city}-{state}`. Requests → 403 (needs browser).
- [x] **Capture confirmed:** FPS summary page is city-level only (like TPS) — the full unit address
      is on each person's `..._id_...` detail page. Unit URL works (header "Unit 637"; FAQ "associate
      4 people with …Unit 637").
- [x] **Detail-crawl generalized** (`DETAIL_CRAWL` + `_crawl_detail_pages`): now drives both TPS
      (`/find/person/…`) and FPS (`/…_id_…`). FPS fetch crawls each detail page, emits those.
- [x] **`parse_fastpeoplesearch`** shares `_parse_ps_detail` with TPS (sister sites) + a fallback
      "Current Address" anchor. Registered. FPS summary → [] (safe). Synthetic detail verified.
- [x] Scoring: FPS → `CONFIRMING_SOURCES`, weight **95** (highest authoritative — user rates it most
      accurate), FORMER −55. Added to default `--sources` (first). TPS refactor: no regression.
- [~] **Pending live verification:** (a) the FPS detail-link regex finds links, (b) FPS detail pages
      use the shared anchor. Capture: `python lookup.py "800 New Jersey Ave SE" Washington DC 20003
      --units 637 --browser --sources FastPeopleSearch`. If crawl count is 0 or 637 shows no FPS,
      tune from archived `raw/*FastPeopleSearchDetail*`.
- **2026-06-29** — **FastPeopleSearch added & VERIFIED (Phase 6 done).** 5th source, weight 95
  (highest authoritative, user rates it most accurate). Generalized the detail-crawler to drive both
  TPS and FPS. FPS detail anchor: "Current Address (Since <date>) <addr> <County> Full Name: <Name>".
  Fixed name capture (strip trailing "Phone Numbers") and phone extraction (skip the TOC "Phone
  Number"; now works for FPS+TPS). 637 → Tenkhoff High, 4-source corroboration, with phones. No
  regression.
- **2026-06-29** — **ThatsThem browser fallback.** When the browser-free `requests` fetch is
  rate-limited and `--browser` is on, `gather_people` adds a ThatsThem spec (`html: True`) to the
  browser batch; `fetch_browser_sources` returns page SOURCE (HTML, for JSON-LD) via `_page_source`,
  requires the `application/ld+json` block, and skips cleanly if still blocked. Real-browser
  fingerprint + CDP often clears what `requests` can't (same IP caveat noted). Verified offline.
- **2026-06-29** — **Three refinements.** (1) ThatsThem browser fallback now uses the UNIT-specific
  page (`thatsthem_unit_url`, `/address/{street}-Apt-{unit}-{city}-{st}-{zip}`) per unit instead of
  the whole building. (2) Speed: `_crawl_detail_pages` filters FPS detail links by name-slug to only
  the summary's residents (skips relatives' pages — 8→~4 crawls/unit); TPS uses opaque ids and
  already lists only residents. (3) FPS weight bumped to 110 (above USPhoneBook's 100) so it wins
  outright when sources disagree. Verified offline; no regression.
- **2026-07-01** — **ThatsThem unit-URL: reverted.** Live test showed ThatsThem ignores the `Apt` in
  the URL — a `…-Apt-637-…` request returns the whole building (56 residents, various units). Confirmed
  its dataset has NO unit-637 resident (has 401/402/429/642/734/10xx/11xx…), so ThatsThem correctly
  contributes nothing to 637 — not a bug. Browser fallback reverted to a single building fetch (one
  fetch covers a whole range; residents filtered by their JSON-LD unit). Removed `thatsthem_unit_url`.

## Phase 7 — Audit fixes + more sources  🟡 (2026-07-01)
**Audit fixes (all applied, verified, no regression):**
- [x] Age-aware merge (`merge_people`) — two different people with the same name are kept apart when
      ages conflict (>1 yr), so one person's unit match can't absorb another's addresses/phones.
- [x] CBC weight 55→85 (authoritative band) + confidence recalibrated (High ≥85) so any single
      authoritative "Lives at" confirmation reads as High, not Medium.
- [x] `_street_matches` tightened — a prefix-match only counts if the extra tokens look like a
      glued-on city, NOT a directional/suffix ('MAIN ST' no longer matches 'MAIN ST N').
- [x] `normalize_address` strips a trailing state code that leaked into the street (no-comma sources).
- [x] CDP activation flag (survives a failed first spec) + `raw/` retention cap (`_prune_raw`, 400).
- [x] Docstring cleanup + FCRA/ToS note in `sources.py` header.

**New sources:**
- [x] **SearchPeopleFree + FastBackgroundCheck** — FPS-family sisters. Wired: FPS-style URL, detail
      crawl (`_id_` links), reuse `_parse_ps_detail`, weight 88, confirming. **Pending one capture**
      to confirm their detail-link format + page anchor (same-backend data → more corroboration).
- [~] **Nuwber** — independent. URL builder + `BROWSER_SOURCES` (browser). Parser NOT registered
      (fetches+archives only) — pending a capture to write it.
- [ ] **Radaris** — independent but form-based (all direct-URL guesses 404). Needs form navigation
      (type address → submit) — a different fetch path; deferred.
- [ ] **Rehold** — independent, browser-free, BUT base URL treats the address as single-family and
      the multi-unit URL is unresolved (now rate-limiting probes). Needs URL discovery; deferred.

Capture cmd (writes raw/ for the new browser sources):
`python lookup.py "800 New Jersey Ave SE" Washington DC 20003 --units 637 --browser --sources FastPeopleSearch,SearchPeopleFree,FastBackgroundCheck,Nuwber`
- **2026-07-02** — **Turnstile click robustness + crawled-relative bug.** Cursor wasn't landing on
  the Turnstile checkbox (PyAutoGUI needs window focus + 100% display scaling). `_clear_challenge`
  now focuses the window and ALTERNATES mouse-click with the keyboard method (Tab+Space, no cursor
  needed) across attempts — live run cleared FPS on attempt 2 (the keyboard pass). Also fixed:
  detail-crawl sources (FPS/TPS/sisters) were tagging crawled RELATIVES with `searched_unit`, so a
  relative (Amy Rebecca Williams, 62) floated into the "possible" leads. Now only summary sources
  (USPhoneBook/CBC/Nuwber) get the association tag; 837/Samantha still surfaces, relatives don't.
  Note: FPS is currently serving compact summary + stub detail pages (IP throttling from heavy
  testing) — needs a cooldown; the full source set is more robust than FPS-only.
- **2026-07-02** — **Diagnosed ThatsThem as an IP-level Cloudflare block** (confirmed: even
  curl_cffi with a real Chrome fingerprint gets "too many" on this IP — so it's not a TLS
  fingerprint nor a time-based limit; it won't self-reset). `fetch_thatsthem` now uses curl_cffi
  when available, FAILS FAST on the block (0.7s vs ~18s), and prints accurate guidance (change IP
  or use --browser, which clears it on the same IP). TPS `__cf_chl_rt_tk` stuck = interactive
  Turnstile not clearing because the mouse click misses (display scaling ≠ 100%); keyboard-alternate
  helps but 100% scaling is the real fix.
- **2026-07-02** — **Display-scaling preflight.** `_warn_if_scaled()` (called at browser-fetch start)
  detects Windows scaling via physical-vs-logical width and warns if >105%. Confirmed the user's
  machine is at 125% — the direct cause of the missed Turnstile clicks. Set to 100% for reliable
  mouse-click clearing (keyboard fallback still tries regardless).

## Phase 8 — Accuracy (recency), robustness (tests/replay), reliability  ✅ DONE (2026-07-03)
Five improvements, implemented + verified together. Highlights: recency is now a real ranking
signal (previously the "since" date was scraped and discarded); a proper assertion test suite
locks in every prior fix (and caught a new latent parser bug on the way); a `--from-cache` replay
lets the whole pipeline run offline against archived captures.

- [x] **Recency "since date" signal.** `Person.current_since` + `since_to_year()` (parses
      'Jul 2018' / '2015' / '07/2018' / '2018-07'). Parsers now capture it: TPS from the post-county
      date RANGE ('… County (Jul 2018 - Jun 2026)' → range start = move-in), FPS from
      '(Since <date>)'. Scoring uses it three ways: (1) **moved-out detection** — a source dating a
      NEWER current address elsewhere than the unit tenure demotes the person to *former*
      (`RECENCY_SUPERSEDED_PENALTY`); (2) **tie-break** — equal-score current candidates rank by
      most-recent move-in (live win: 637 Tenkhoff *Nov 2023* now outranks Wayne Hwilliams *Dec 2020*,
      both score 305); (3) **display** — "since <date>" on the console line, an evidence bullet, and a
      new **"Resident Since"** export column.
- [x] **Recency-aware confidence recalibration** (supersedes Phase 7's flat "High ≥85"). High now
      requires **≥2 corroborating sources OR one confirming source with a recent move-in date**
      (`RECENT_WITHIN_YEARS=6`). A lone, undated/old authoritative source → new **"Medium-High"**
      tier (real but uncorroborated — don't overstate). 637 with 3–4 live sources stays *High*; the
      old single-source saved captures now read *Medium-High* (intended).
- [x] **Assertion test suite** (`test_pipeline.py`, no pytest needed — `python test_pipeline.py`,
      exits non-zero on failure). 19 groups / 68 assertions over normalize/match/classify,
      since parsing, all five parsers (compact fixtures matched to the REAL page anchors), age-aware
      merge, and the full `score_candidates` ranking (corroboration, supersession, recency conflict,
      recency tie-break, possible/former tiers, confidence tiers). Includes the Amy-Williams-#938
      regression. `validate_saved.py` now also runs the scoring pass (was classify-only).
- [x] **Latent bug found by the suite + fixed:** the FPS "Full Name" regex required every token to
      be 2+ chars, so a middle initial ('Amy **K** Williams') truncated the name to "Amy" and the
      record was dropped. Now allows single-letter initials.
- [x] **`--from-cache` replay** (`load_cached_captures` in `sources.py`, `gather_from_cache` in
      `lookup.py`). Reconstructs `people` from archived `raw/` captures — NO network — de-duped to the
      newest run per (unit, source) so a stale capture can't out-vote a fresh one (detail-crawl runs
      keep the whole newest day). Mirrors live `searched_unit` tagging, so scoring is identical to a
      live run. Lets parsers/scoring be iterated without re-hitting (and getting blocked by) the
      sites; doubles as a real-data regression check. Verified: 637 → Tenkhoff *High* + since date;
      837 → Karlin lead intact.
- [x] **Reliability.** (1) **`--proxy host:port`** threaded into curl_cffi (ThatsThem) AND
      SeleniumBase — a different IP is the real fix for the IP-level block. (2) **`--delay S`**
      polite base+jitter gap between page/detail loads (`_pace`) to avoid tripping rate limits
      (opt-in; 0 = off, preserves current speed). (3) **Per-source parse-yield reporting** (live +
      cache) — flags a source that cleared but parsed 0 residents (a throttled stub), which the old
      "crawled N/N" message hid. (4) **Early stub-skip** — a detail-crawl summary that clears but
      lists 0 residents (`_summary_resident_keys` empty) skips the detail crawl instead of wasting
      page loads/challenges on a stub.
- **2026-07-03** — **Phase 8 shipped.** All five verified: `test_pipeline.py` 68/68 green;
      `validate_saved.py` scoring pass shows no classify regression on the 8 saved files;
      `--from-cache` reproduces 637 → Tenkhoff (High, since Nov 2023, phones, recency tie-break over
      Wayne) and 837 → Karlin lead. Data-staleness ceiling unchanged (free DBs still lag brand-new
      move-ins) — these changes sharpen accuracy *within* the ceiling and are honest *about* it.

## Phase 9 — Second audit: identity, independence, discarded signals, robustness  ✅ DONE (2026-07-06)
A critical re-audit found the real accuracy leaks weren't on the happy path — they were in identity
matching, in a corroboration model that counted resold data as independent, and in signals scraped
then thrown away. Implemented in five parts, each with tests + a `--from-cache` verification.

**Part 1 — matching & identity core:**
- [x] **Locality guard** (`match_address`): a same-numbered street in another city/state no longer
      matches. ZIP (and state) must agree when both sides carry them; missing = wildcard, so a terse
      source never loses a match. Was silently false-matching prior addresses across states.
- [x] **Suffix-aware `name_key`**: strips Jr/Sr/II-V so 'Robert Kinsler Jr' and 'Robert A Kinsler'
      both key ROBERT KINSLER (previously 'Jr' became the surname → the person split into two).
- [x] **AKA reconciliation** (`merge_people`): folds together maiden/alias variants (e.g. Amy
      Williams / Amy Davidson) using parsed 'Also Seen As' / 'Other observed names', but ONLY when
      the two share a first name AND ages don't conflict — so a spouse listed in someone's observed
      names isn't absorbed. Verified on real data: Kirk is NOT merged into Kathryn.

**Part 2 — confidence calibration (the biggest overstatement fix):**
- [x] **Evidence families** (`SOURCE_FAMILY`): TruePeopleSearch/FastPeopleSearch/SearchPeopleFree/
      FastBackgroundCheck share ONE backend, so their agreement is one piece of evidence, not four.
      Corroboration bonus, the "≥2 → High" rule, AND the score now count distinct FAMILIES (score
      takes the strongest weight per family, so N sisters can't out-rank two independent sources).
      637's genuine 3-family corroboration (CBC + TPS-FPS + USPhoneBook) is unchanged → still High.
- [x] **Undated current-address conflict**: when one source says the unit is current but another
      lists the person's current address elsewhere (no dates to arbitrate), confidence is lowered
      (`CONFLICT_PENALTY`) and flagged, instead of silently ignoring the disagreeing source.

**Part 3 — signals that were scraped and discarded:**
- [x] **Move-out / staleness from the date-range END** (`Person.current_until`): TPS prints a range
      `(Jul 2018 - Jun 2026)`; we kept only the start. Now capture the end; if the unit's last-reported
      date is well in the past (`STALE_AFTER_YEARS`), flag "may have moved since".
- [x] **Phone recency & status** (`_ranked_phones`): rank TPS/FPS phones by "Last reported" date
      (most recent first, primary first) and DROP numbers flagged Inactive.

**Part 4 — robustness:**
- [x] **stdout UTF-8 safeguard** (`_utf8_console`, all entry points): a non-cp1252 char in a print no
      longer crashes on a legacy Windows console (proven `UnicodeEncodeError`). Also converted printed
      em-dashes to ASCII in the hot paths.
- [x] **`requirements.txt`** pinned to the tested versions (curl_cffi / seleniumbase are the fragile
      ones).
- [x] **Loud parser-drift warning** (`has_result_markers`): a source whose page has result markers
      but parses to 0 residents now prints "PARSER DRIFT?" (layout changed) vs. a quiet stub.

**Part 5 — eval harness & accuracy features:**
- [x] **`eval_accuracy.py` + `eval_ground_truth.json`**: measures precision against known units by
      replaying the cache — turns weight/threshold tuning into measurement. Seeded with 637 (current
      Tenkhoff) + 837 (possible Karlin) → **2/2**. Extend as more units are verified.
- [x] **Household grouping** (`report_unit`): a co-listed resident who is a relative of the top pick
      or shares a phone is shown as "Same household (likely co-residents)" instead of a competing
      answer. Live: Kirk M Tenkhoff now groups under Kathryn; Wayne stays a separate candidate.
      Added CBC 'Related to' parsing to feed it.
- [x] **Capture-age awareness**: `--from-cache` prints the newest capture date/age and warns if the
      cache is >90 days old.
- **2026-07-06** — **Phase 9 shipped.** `test_pipeline.py` **91/91**; `eval_accuracy.py` **2/2**;
      `validate_saved.py` no regression (all 8 saved units correct); `--from-cache` 637 → Tenkhoff
      *High* (3 families, household-grouped Kirk) and 837 → Karlin lead. The evidence-family fix is the
      headline: confidence is no longer inflated by resold data. Ceiling still the free-data lag.

## Phase 10 — Independent open-data sources (break the resold-data echo)  ✅ DONE (2026-07-06)
The Phase 9 audit showed the six aggregator sites are largely ONE data lineage, so more of them
can't add real corroboration. This phase adds genuinely INDEPENDENT free open-data APIs — a new
`apis.py` module (JSON, no browser, no Cloudflare), each its own evidence family.

**Built (verified live against the real APIs):**
- [x] **OpenFEC** (`fetch_fec`) — FEC individual contributions: name + self-reported address + DATE +
      occupation/employer. Queries by contributor ZIP (server can't filter by street), pages
      most-recent-first, matches street+unit client-side. DEMO_KEY works; `--fec-key`/`FEC_API_KEY`
      for real use. **Honest limit:** a dense ZIP (e.g. 20003 = 1.4M records) needs the building's
      9-digit ZIP (`--fec-zip9`) or it won't reach the building — confirmed live (0 matches for this
      building without zip9). Shines in less dense areas / with zip9. Weight 60, confirming, dated.
- [x] **DCProperty** (`fetch_dc_property`) — DC Integrated Tax System (opendata.dc.gov) owner of
      record. FeatureServer URL resolved at runtime via ArcGIS Hub (its layer name is date-versioned).
      **Owner-occupant** (a person mailing to the unit) → real residency signal, weight 85, family
      DCProperty. **Entity/absentee owner** → the unit is a RENTAL; surfaced as an "Owner of record"
      CONTEXT line (source `DCPropertyOwner`, never ranked as a resident). Empirically confirmed
      **800 New Jersey Ave SE is a single-LLC rental** ("NEW JERSEY AT H LLC") — so property records
      here name the landlord, not residents, and the tool says exactly that. For owner-occupied condo
      buildings the same code yields per-unit owner-occupants.
- [x] **SECEdgar** (`fetch_sec_edgar`) — EDGAR full-text search → filer names at an address. Wired and
      working, but **very low yield for residential** (0 hits for this address, as expected) — a
      tie-breaker only, opt-in via `--api-sources`. Weight 35.

**Wiring:**
- [x] `Person.note` (occupation/employer, owner-of-record, filing) surfaced in the evidence lines.
- [x] Evidence families + weights + `CONFIRMING_SOURCES` updated; `CONTEXT_SOURCES={"DCPropertyOwner"}`
      filtered out of resident scoring and shown by `report_unit` as an "Owner of record" line.
      DC 'LAST FIRST' owner names reordered to 'First Last' so they merge with the aggregators.
- [x] CLI: `--apis`, `--api-sources` (default `OpenFEC,DCProperty`), `--fec-key`, `--fec-zip9`,
      `--fec-pages`; `gather_api_sources` (one building-level query per source, per-unit classified
      downstream); "Owner of Record" export column; raw JSON archived to `raw/`.
- [x] Tests: 5 new groups (fixture/monkeypatched, no network) → **107/107**. Live E2E: 637 shows the
      LLC owner-of-record context while Tenkhoff stays *High*; eval still 2/2; no regression.

**Deliberately NOT built (reported, with rationale — not shipping broken/ill-advised stubs):**
- **Voter registration** — the best current-resident signal, but DC restricts access to permitted
      uses; won't build a circumvention.
- **OpenCorporates / business filings** — free tier is token-gated and NAME-based (officer/company
      search), not reverse-address; doesn't fit "address → resident".
- **Professional license boards / court dockets** — no reverse-address API; fragmented per-board/
      per-court name-search portals. A future per-jurisdiction adapter, not a general source.
- **Zillow/Redfin/LinkedIn/Nextdoor/search-dorking** — ToS-hostile and/or low unit-level precision;
      the useful "last-sold recency" is partly covered by DCProperty already.

Takeaway: of the brainstormed open sources, exactly three are genuinely reverse-address-capable via
free open APIs, and all three are built. They add real independent corroboration and, crucially, the
owner-occupied-vs-rental signal — while staying honest that no open source names a brand-new private
renter (the standing free-data ceiling).
- **2026-07-08** — **Phase 10 follow-up fixes** (surfaced by a unit-941 run): (1) API JSON archives
      (`ALL__OpenFEC__`/`ALL__DCProperty__`) were being swept into the `--from-cache` TEXT-replay path
      and flagged "stale/stub" — added `_API_REPLAY_SKIP` so `load_cached_captures` ignores them (they
      are queried live, not replayed). (2) A bot-check/captcha stub was mis-flagged "PARSER DRIFT" —
      tightened `_RESULT_MARKERS` (dropped `application/ld+json` and `Current Address`, which appear on
      every page/shell) and added a challenge-phrase guard so a block reads as "stub", not drift; also
      stopped counting a detail-crawl SUMMARY (parses 0 by design) toward drift. 111/111 tests. The
      941 result itself was correct all along: prior resident Timothy Kutta moved out, no confirmed
      replacement in free data (staleness ceiling), LLC-owned rental.

## Phase 11 — Reconciliation of an out-of-band 2026-08-10 session  ✅ DONE (2026-08-18)
On returning to the project, `scoring.py`/`parsers.py` (and others) contained changes NOT made in
the documented sessions above — an out-of-band dev session dated **2026-08-10** had extended the
tool. It left the tree at 109/111 tests (a regression). Audited, reconciled, re-greened before
resuming feature work.

**What the 08-10 session added (now inventoried):**
- **`history.py` (new)** — SQLite delta tracking (`record_run` / `get_delta`) tagging residents
  `[NEW]`/`[CONFIRMED]`/`[REMOVED]` vs the previous run for a street/zip/unit. Wired into
  `report_unit` (on by default). *This is a first cut of the monitoring feature.*
- **`llm_parser.py` (new)** — Ollama auto-heal parser (`fallback_parse_llm`, invoked by
  `parse_source` when a populated page parses to 0), plus `osint_evaluate` (duckduckgo-search +
  Ollama for recent web/move signals). Wired via `--osint`. *First cut of web enrichment.*
- **`parsers.py`** — `_norm_phone` upgraded to the **`phonenumbers`** library (validate + NATIONAL
  format + carrier); `parse_source` gained the LLM drift-fallback.
- **`scoring.py`** — `merge_people` rewritten to **fuzzy first-name matching** (`difflib` ratio
  > 0.85 + a `NICKNAMES` map, exact last name); a **"Network Elevation"** block promotes a
  `possible` lead into `current` if it matches a confirmed resident's relative.
- **`lookup.py`/`sources.py`/`apis.py`** — new flags `--proxies` (rotation), `--osint`, `--forward`
  (multi-hop, currently a STUB that logs but never calls `fetch_forward_search`); new `DCVacant`
  API source in the `--api-sources` default.

**Reconciliation done:**
- [x] Diagnosed the 109/111 regression: the failing phone tests were STALE, not a bug — they used a
      NANP-invalid fake number (`202-111-2222`) that `phonenumbers` correctly rejects, and asserted
      the old `+1-...` format. Kept the phonenumbers upgrade (legit improvement); rewrote the test
      with valid numbers + digit-based (format-agnostic) assertions. **Back to 111/111.**
- [x] Declared the new deps: `phonenumbers` in requirements.txt; noted duckduckgo-search + Ollama as
      optional (llm_parser only).

**Flagged risks (for a decision before hardening):**
- ⚠️ **Network Elevation can assert an elsewhere-resident as current.** Probe: a relative listed
      living in Reston, VA was promoted to *current@637 [Medium-High]* just for being a confirmed
      resident's relative — re-introducing the exact "lives elsewhere but shown as current" error the
      project was built to avoid, with score/confidence inconsistent (score ~10, confidence
      Medium-High). Recommend gating it (keep as a labeled lead, not "current") or requiring a
      building-level address link.
- ⚠️ **Fuzzy merge edge:** `Jon Smith` ~ `Joan Smith` → merge (ratio 0.857); narrow, guarded by the
      age check. The nickname handling (Bob↔Robert) is a genuine win.
- ⚠️ **`--forward` is a stub** — advertises multi-hop forward search but does nothing yet.

**Revised feature roadmap** (the 08-10 work already partially covers 1/2/4/5, so COMPLETE & HARDEN
rather than rebuild): finish Feature 1 as the building roster/graph + a real move-chain (completing
`--forward`); extend `history.py` into full snapshot diffing for Feature 2; complete proxy rotation
(`--proxies`) + session reuse for Feature 5; extend `osint_evaluate` into structured enrichment for
Feature 4. All on top of the reconciled, green baseline.

## Phase 12 — Feature 1: building graph, roster & move-chains  ✅ DONE (2026-08-18)
Complete-and-harden on top of the reconciled baseline. Mines data we already fetch (one ThatsThem
building call returns the whole building) into three OSINT views. New module **`graph.py`** +
`--roster`; the fake `--forward` stub replaced with a real forward trace.

- [x] **Elevation gated** (`scoring.py`): the 08-10 "Network Elevation" no longer promotes a
      confirmed resident's relative into `current` when that relative's own records place them
      elsewhere (it asserted a Reston relative as current@637). Now it ANNOTATES them as a "stronger
      lead (not confirmed current)" and keeps them in `possible`. Regression test added.
- [x] **`graph.build_roster`** + `--roster`: discovers every unit present in the data and prints the
      likely resident of EACH, not one at a time. Live (cache): **47 units** mapped for 800 NJ Ave SE
      (637→Tenkhoff High, 938→Amy Williams High, 1104→Bernard High, 837/941→leads/none). Status
      renamed `current`/`lead`/`none` (was the overstated "confirmed").
- [x] **`graph.household_clusters`**: union-find over residents linked by a shared phone or a
      relative/AKA tie. Live: **11 households** surfaced (Tenkhoff spouses, the Bernard family, Kutta
      Jr/Sr, …) — co-residents grouped without being merged into one person.
- [x] **`graph.move_chains`** + `moved_to` on `ScoredCandidate`: reconstructs where former residents
      went (unit in their PRIOR addresses, current address elsewhere). Live: **18 move-chains** — and
      this directly answered the earlier unit-941 mystery: *Timothy John Kutta Jr: 941 → now at
      1877 Gina Dr, Tallahassee, FL*. Shown per-unit on the `Former` line and under `--forward` for
      leads (837's Karlin → her old 800 4th St SW address, confirming the staleness story).
- [x] **`Address.display()`** — strips `[County]` tags / collapses whitespace so move-destination
      addresses read cleanly across the varied source formats.
- [x] Tests: `graph` roster/household/move-chain + display cleanup + the elevation-gate regression →
      **119/119**.

Note: `build_roster` calls `score_candidates` per discovered unit (fuzzy `merge_people` is O(n²));
fine for a ~70-person building, worth a single-merge optimization if buildings get much larger.

## Phase 13 — Feature 2: history / change monitoring  ✅ DONE (2026-08-18)
Complete-and-harden of the 08-10 `history.py`. Attacks the freshness ceiling over time: record each
run, diff against the last, and view the change log on demand.

- [x] **Fixed a silently-broken feature:** the 08-10 integration called `target.zip_code`, but
      `Address` has `.zip` — so `record_run` threw `AttributeError` (swallowed) and **history had
      never actually recorded anything** (no run ever showed a delta tag). Fixed to `target.zip` and
      added `_hist_street()` so the street key is consistent between the per-unit path (Address) and
      the roster/`--history` path (raw string). Delta tags now really fire (run 2 → `[CONFIRMED]`).
- [x] **`--history` dashboard (read-only, no scrape):** with `--units`, a resident TIMELINE per unit
      (timestamped, newest-first); without, the BUILDING change log (`history.building_changes` -
      NEW/REMOVED current residents between each unit's two latest runs). New `history` functions:
      `last_run`, `get_timeline`, `building_changes`.
- [x] **Roster records + tags** (`report_roster`): a `--roster` run now records every unit and shows
      its `[NEW]`/`[CONFIRMED]`/`[REMOVED]` tag, so building history accrues from roster sweeps too.
- [x] **Display regression fixed:** the 08-10 `report_unit` printed EVERY current candidate as
      "MOST LIKELY" and then AGAIN under household/other (double-listing). Restored to a single
      most-likely + household + other-candidates, keeping the delta tags and the `--osint` hook.
- [x] Test (`test_history_monitoring`, isolated temp DB): NEW/REMOVED deltas, timeline order, and the
      building change log. **124/124.**

Usage: scrape periodically (each run records automatically), then
`python lookup.py "800 New Jersey Ave SE" Washington DC 20003 --history` for the building change log,
or add `--units 637` for that unit's resident timeline.

## Phase 14 — Feature 5: beat the blocking  ✅ DONE (2026-08-18)
The 08-10 session had already built solid PROXY ROTATION into `fetch_browser_sources` (comma-list
`--proxies`, rotate + restart the browser on a block) and wired it end-to-end. Completed the rest:

- [x] **Session/cookie reuse (`--keep-session`)** - the biggest missing win. `fetch_browser_sources`
      now takes `session_dir`; when set it uses a PERSISTENT Chrome profile (`user_data_dir`,
      incognito off = `./.chrome_profile`) so a solved Cloudflare/Turnstile clearance CARRIES OVER to
      later runs instead of re-challenging every time. (Was `incognito=True` = fresh session always.)
- [x] **Backoff on block** before rotating proxies (`min(3 + 2*block_hits, 20)` s), so a rotated
      proxy / rate-limited IP isn't hammered instantly.
- [x] **Removed the dead `fetch_forward_search` stub** (Feature 1 replaced `--forward` with a real
      data-derived forward trace, so nothing referenced it).
- [~] **Browser-free-first: deliberately NOT added.** Assessed and skipped - the browser sources are
      Cloudflare-INTERACTIVE (Turnstile), so a curl_cffi pre-probe can't clear them and would just
      waste a request each. ThatsThem (the one genuinely browser-free source) already runs via
      curl_cffi. Honest call over a mostly-useless probe.

Verified: 124/124, flags/threading confirmed (`--keep-session`, `--proxies`, `session_dir` param).
The persistent-session clearance is best observed on the user's desktop (needs live Chrome).
Usage: `... --browser --keep-session --proxies host1:port,host2:port --delay 3`.

## Phase 15 — Feature 4: person enrichment  ✅ DONE (2026-08-18)
The 08-10 `--osint` already does WEB enrichment (DuckDuckGo + Ollama). Added STRUCTURED enrichment
from the independent open-data APIs, queryable by NAME - new module **`enrich.py`** + `--enrich`.

- [x] **`enrich_fec`** - the resident's FEC contributions -> occupation, employer, date, amount.
      Queries with FIRST+LAST (FEC stores 'LAST, FIRST MIDDLE' and matches a middle initial poorly)
      and narrows by the resident's STATE to reduce same-name mixing. Live win: 637 Tenkhoff ->
      *"FEC: Lawyer, Law Firm (as of 2025-09-29)"*.
- [x] **`enrich_dc_property`** - DC parcels OWNED by this person (`OWNERNAME LIKE`) - do they own the
      unit or others? (Empty for Tenkhoff: she rents - correct.)
- [x] **`enrich_opencorporates`** - companies they're an officer of (best-effort; free tier is
      limited/token-gated, so degrades to nothing).
- [x] `enrich_person` + `format_enrichment`; wired into `report_unit` via `--enrich` (top resident
      only), printed as `[enrich]` evidence lines alongside `--osint`. Test with monkeypatched HTTP.
      **128/128.**

Caveat: name-based enrichment is inherently fuzzy - matched on first+last (+ state for FEC), but two
different people with the same name in the same state can still be conflated. Presented as
supplementary, not authoritative. Usage: add `--enrich` (optionally `--osint`) to a per-unit run.

## Phase 16 — Full OSINT Expansion  ✅ DONE (2026-08-18)
- [x] **Feature 1:** Reverse Phone Lookup (\--phone\) & Name Forward Search (\--name\)
- [x] **Feature 2:** Court & Legal Records enrichment via DDG search of CourtListener & DC Courts
- [x] **Feature 3:** Self-contained HTML Dossier Generation (\--dossier\)
- [x] **Feature 4:** Monitoring Mode (\--monitor\) for scheduled cron/task-scheduler re-scrapes
- [x] **Feature 5:** DC Open Data APIs: BBL (Business Licenses), 311 Requests, and Crime Incidents as Context signals
- [x] **Feature 6:** Relationship Graph Visualization via embedded Mermaid.js inside Dossiers and Multi-hop rel tracing.

## Phase 17 — Advanced OSINT Expansion (Level 3) ✅ DONE (2026-08-18)
- [x] **Corporate Unmasking:** Automatically pierces the LLC veil using the DC Corporate Registration API to find the Registered Agent of entity-owned properties.
- [x] **Social Media Footprinting:** Uses DDGS to enumerate and extract LinkedIn, Twitter, and GitHub profiles.
- [x] **Deep Web Traversal & LLM Biographies:** Replaced simple OSINT evaluation with a multi-query DDGS scrape (LinkedIn, News, Web) fed into Ollama to write 3-4 sentence comprehensive biographic summaries.
- [x] **Property Intelligence:** Enhanced the DC Property API to extract and display Assessed Value, Last Sale Price, and Sale Date directly in the dossier.

## Phase 18 — Nationwide OSINT Scope ✅ DONE (2026-08-18)
- [x] **Verified Core Independence**: Ensured that the primary web OSINT aggregators (TruePeopleSearch, USPhoneBook, FastPeopleSearch), CourtListener, and OpenFEC are fundamentally nationwide. No logic is locked to DC.
- [x] **Nationwide Geospatial Context (OSM Nominatim)**: Implemented a new nationwide building context API using OpenStreetMap Nominatim to resolve building type (e.g. house, apartments, office) and neighborhood metadata for any address in the world.
- [x] **Nationwide PDF Document OSINT**: Implemented a Google Dorking module to extract public PDFs (resumes, board minutes, legal filings) for targets nationwide.
