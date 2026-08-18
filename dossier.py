"""
dossier.py — HTML dossier report generator.

Generates self-contained HTML files for per-person and building-level OSINT reports.
All CSS is inlined — no external dependencies. Printable.
"""

from __future__ import annotations

import html
import os
from datetime import datetime
from typing import Optional


def _esc(s: str) -> str:
    return html.escape(str(s)) if s else ""


_CSS = """
body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px;
       background: #f8f9fa; color: #1a1a2e; max-width: 900px; margin: 0 auto; }
h1 { color: #16213e; border-bottom: 3px solid #0f3460; padding-bottom: 8px; }
h2 { color: #0f3460; margin-top: 28px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
h3 { color: #533483; margin-top: 18px; }
.meta { color: #666; font-size: 0.9em; margin-bottom: 20px; }
.card { background: #fff; border-radius: 8px; padding: 16px 20px; margin: 12px 0;
        box-shadow: 0 1px 3px rgba(0,0,0,0.12); }
.badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.85em;
         font-weight: 600; margin-right: 6px; }
.badge-current { background: #d4edda; color: #155724; }
.badge-possible { background: #fff3cd; color: #856404; }
.badge-former { background: #f8d7da; color: #721c24; }
.badge-new { background: #cce5ff; color: #004085; }
.badge-removed { background: #f8d7da; color: #721c24; }
.badge-confirmed { background: #d4edda; color: #155724; }
.evidence { margin: 4px 0 4px 16px; color: #555; font-size: 0.9em; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }
th { background: #16213e; color: #fff; font-weight: 500; }
tr:nth-child(even) { background: #f8f9fa; }
.phones { color: #0f3460; }
.enrich-line { margin: 2px 0 2px 16px; color: #533483; font-size: 0.9em; }
.mermaid-container { background: #fff; border-radius: 8px; padding: 20px; margin: 12px 0;
                     box-shadow: 0 1px 3px rgba(0,0,0,0.12); text-align: center; }
.footer { margin-top: 30px; padding-top: 10px; border-top: 1px solid #ddd;
          color: #999; font-size: 0.8em; text-align: center; }
@media print { body { background: #fff; } .card { box-shadow: none; border: 1px solid #ddd; } }
"""

_MERMAID_JS = '<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>'


def _person_card(person, delta_tag: str = "", enrichment_lines: list[str] = None) -> str:
    """Generate an HTML card for a single scored candidate."""
    parts = []
    age = f", age {person.age}" if person.age else ""
    since = f", since {person.since}" if person.since else ""
    conf = _esc(person.confidence)
    
    delta_badge = ""
    if delta_tag == "[NEW]":
        delta_badge = '<span class="badge badge-new">NEW</span>'
    elif delta_tag == "[CONFIRMED]":
        delta_badge = '<span class="badge badge-confirmed">CONFIRMED</span>'
    elif delta_tag == "[REMOVED]":
        delta_badge = '<span class="badge badge-removed">REMOVED</span>'
    
    parts.append(f'<div class="card">')
    parts.append(f'<h3>{delta_badge}{_esc(person.name)}{_esc(age)}{_esc(since)}</h3>')
    parts.append(f'<p><strong>Confidence:</strong> {conf} (score: {person.score})</p>')
    
    if person.phones:
        parts.append(f'<p class="phones"><strong>Phones:</strong> {_esc(", ".join(person.phones))}</p>')
    
    if person.evidence:
        parts.append('<p><strong>Evidence:</strong></p>')
        for ev in person.evidence:
            parts.append(f'<p class="evidence">• {_esc(ev)}</p>')
    
    if person.relatives:
        parts.append(f'<p><strong>Relatives:</strong> {_esc(", ".join(person.relatives[:10]))}</p>')
    
    if enrichment_lines:
        parts.append('<p><strong>Enrichment:</strong></p>')
        for line in enrichment_lines:
            parts.append(f'<p class="enrich-line">◆ {_esc(line)}</p>')
    
    parts.append('</div>')
    return "\n".join(parts)


def generate_unit_dossier(
    street: str, unit: str, city: str, state: str, zip_code: str,
    current: list, possible: list, former: list,
    delta: dict = None, enrichment: dict = None,
    owner_lines: list[str] = None, osint_notes: dict = None,
    mermaid_code: str = ""
) -> str:
    """Generate a self-contained HTML dossier for a single unit."""
    delta = delta or {}
    enrichment = enrichment or {}
    owner_lines = owner_lines or []
    osint_notes = osint_notes or {}
    
    label = f"{street} Unit {unit}" if unit else street
    title = f"OSINT Dossier — {label}, {city}, {state} {zip_code}"
    
    parts = [f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>{_CSS}</style>
{_MERMAID_JS if mermaid_code else ''}
</head><body>
<h1>🔍 {_esc(title)}</h1>
<p class="meta">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Tool: Address Research OSINT</p>
"""]
    
    # Current Residents
    parts.append('<h2>✅ Current Residents</h2>')
    if current:
        for c in current:
            d_tag = delta.get(c.name.upper(), "")
            e_lines = enrichment.get(c.name, [])
            parts.append(_person_card(c, d_tag, e_lines))
            osint = osint_notes.get(c.name)
            if osint:
                parts.append(f'<div class="card"><p><strong>🌐 OSINT:</strong> {_esc(osint)}</p></div>')
    else:
        parts.append('<div class="card"><p>No unit-confirmed current resident in the data.</p></div>')
    
    # Possible Leads
    if possible:
        parts.append('<h2>❓ Possible Leads (Unconfirmed)</h2>')
        for p in possible:
            d_tag = delta.get(p.name.upper(), "")
            parts.append(_person_card(p, d_tag))
    
    # Former Residents
    if former:
        parts.append('<h2>📦 Former Residents</h2>')
        for f in former:
            d_tag = delta.get(f.name.upper(), "")
            parts.append(_person_card(f, d_tag))
    
    # Removed (delta only)
    removed = [n for n, tag in delta.items() if tag == "[REMOVED]"]
    if removed:
        parts.append('<h2>🚫 Removed Since Last Run</h2>')
        for r in removed:
            parts.append(f'<div class="card"><p><span class="badge badge-removed">REMOVED</span> '
                         f'{_esc(r)}</p></div>')
    
    # Owner of Record
    if owner_lines:
        parts.append('<h2>🏢 Owner of Record</h2>')
        for line in owner_lines:
            parts.append(f'<div class="card"><p>{_esc(line)}</p></div>')
    
    # Relationship Graph
    if mermaid_code:
        parts.append('<h2>🔗 Relationship Graph</h2>')
        parts.append(f'<div class="mermaid-container"><pre class="mermaid">\n{_esc(mermaid_code)}\n</pre></div>')
    
    parts.append(f'<div class="footer">Address Research OSINT Platform • {datetime.now().year}</div>')
    parts.append('</body></html>')
    return "\n".join(parts)


def generate_building_dossier(
    street: str, city: str, state: str, zip_code: str,
    roster_rows: list[dict],
    household_clusters: list = None,
    move_chains: list = None,
    mermaid_code: str = ""
) -> str:
    """Generate a self-contained HTML building report."""
    household_clusters = household_clusters or []
    move_chains = move_chains or []
    
    title = f"Building Report — {street}, {city}, {state} {zip_code}"
    
    parts = [f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>{_CSS}</style>
{_MERMAID_JS if mermaid_code else ''}
</head><body>
<h1>🏢 {_esc(title)}</h1>
<p class="meta">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Tool: Address Research OSINT</p>
"""]
    
    # Roster Table
    parts.append('<h2>📋 Building Roster</h2>')
    parts.append('<table><tr><th>Unit</th><th>Status</th><th>Resident</th>'
                 '<th>Confidence</th><th>Phones</th></tr>')
    for row in roster_rows:
        top = row.get("top")
        unit = _esc(row["unit"])
        status = row["status"]
        badge_cls = {"current": "badge-current", "lead": "badge-possible", "none": "badge-former"}.get(status, "")
        name = _esc(top.name) if top else "—"
        conf = _esc(top.confidence) if top else "—"
        phones = _esc(", ".join(top.phones[:2])) if top and top.phones else "—"
        parts.append(f'<tr><td>{unit}</td><td><span class="badge {badge_cls}">{_esc(status)}</span></td>'
                     f'<td>{name}</td><td>{conf}</td><td>{phones}</td></tr>')
    parts.append('</table>')
    
    # Household Clusters
    if household_clusters:
        parts.append('<h2>👨‍👩‍👧‍👦 Household Clusters</h2>')
        for i, cluster in enumerate(household_clusters):
            names = ", ".join(c.name for c in cluster)
            parts.append(f'<div class="card"><strong>Household {i+1}:</strong> {_esc(names)}</div>')
    
    # Move Chains
    if move_chains:
        parts.append('<h2>🚚 Move Chains</h2>')
        parts.append('<table><tr><th>Name</th><th>From Unit(s)</th><th>Now At</th></tr>')
        for ch in move_chains:
            parts.append(f'<tr><td>{_esc(ch["name"])}</td>'
                         f'<td>{_esc(", ".join(ch["from_units"]))}</td>'
                         f'<td>{_esc(ch["now_at"])}</td></tr>')
        parts.append('</table>')
    
    # Mermaid Graph
    if mermaid_code:
        parts.append('<h2>🔗 Relationship & Move Graph</h2>')
        parts.append(f'<div class="mermaid-container"><pre class="mermaid">\n{_esc(mermaid_code)}\n</pre></div>')
    
    parts.append(f'<div class="footer">Address Research OSINT Platform • {datetime.now().year}</div>')
    parts.append('</body></html>')
    return "\n".join(parts)


def write_dossier(html_content: str, street: str, unit: str = "", out_dir: str = "") -> str:
    """Write the HTML dossier to disk. Returns the file path."""
    if not out_dir:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(out_dir, exist_ok=True)
    
    import re
    slug = re.sub(r"[^A-Za-z0-9]+", "_", street).strip("_")
    unit_part = f"_unit_{unit}" if unit else ""
    stamp = datetime.now().strftime("%Y-%m-%d")
    fname = f"{stamp}_{slug}{unit_part}.html"
    path = os.path.join(out_dir, fname)
    
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_content)
    return path
