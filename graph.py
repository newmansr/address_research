"""
graph.py — building-level views over the collected Person records (Feature 1).

Turns the flat list of scraped people into three OSINT views, all reusing data we already fetch
(one ThatsThem building call returns the whole building, so a roster is near-free):

  - ROSTER          : the likely resident of EVERY unit that appears in the data, not one at a time.
  - HOUSEHOLDS      : clusters of people linked by a shared phone or a relative/AKA tie.
  - MOVE-CHAINS     : former residents of the building and where they now live.

These read the same `people` list `report_unit` scores, so they stay consistent with the per-unit
answer (same scoring, same elevation gate, same context-source exclusion).
"""

from __future__ import annotations

import re

from resident_core import normalize_address, match_address, MatchLevel, name_key
from scoring import score_candidates, merge_people, CONTEXT_SOURCES


def _building(street, city, state, zip_code):
    return normalize_address(f"{street}, {city}, {state} {zip_code}")


def _unit_sort_key(u: str):
    m = re.match(r"(\d+)", u or "")
    return (0, int(m.group(1)), u) if m else (1, 0, u)


def _residents(people):
    """Merge-and-dedupe the scorable people (drops context-only sources like absentee owners)."""
    return merge_people([p for p in people if p.source not in CONTEXT_SOURCES])


def discover_units(people, building) -> list[str]:
    """Every unit of this building that appears anywhere in the data (current or prior addresses)."""
    units = set()
    for p in people:
        for a in [p.current_address, *p.prior_addresses]:
            if a and a.unit and match_address(building, a) != MatchLevel.NONE:
                units.add(a.unit)
    return sorted(units, key=_unit_sort_key)


def build_roster(people, street, city, state, zip_code) -> list[dict]:
    """Per-unit summary for every discovered unit: the top confirmed resident, else the top lead."""
    building = _building(street, city, state, zip_code)
    rows = []
    for unit in discover_units(people, building):
        target = normalize_address(f"{street} Unit {unit}, {city}, {state} {zip_code}")
        current, possible, former, _ = score_candidates(people, target)
        if current:
            top, status = current[0], "current"
        elif possible:
            top, status = possible[0], "lead"
        else:
            top, status = None, "none"
        rows.append({"unit": unit, "status": status, "top": top,
                     "current": current, "possible": possible, "former": former})
    return rows


def household_clusters(people) -> list[list]:
    """Cluster residents into households by a shared phone or a relative-name link (union-find).
    Returns clusters (lists of Candidate) with >= 2 distinct members."""
    cands = _residents(people)
    n = len(cands)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    phone_ix: dict[str, list[int]] = {}
    key_ix: dict[str, list[int]] = {}
    for i, c in enumerate(cands):
        for ph in c.phones:
            phone_ix.setdefault(ph, []).append(i)
        key_ix.setdefault(c.name_key(), []).append(i)

    for ids in phone_ix.values():          # same phone -> same household
        for j in ids[1:]:
            union(ids[0], j)
    for i, c in enumerate(cands):          # lists another candidate as a relative -> same household
        for rel in c.relatives:
            for j in key_ix.get(name_key(rel), []):
                if j != i:
                    union(i, j)

    groups: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(cands[i])
    return [g for g in groups.values() if len(g) >= 2]


def move_chains(people, street, city, state, zip_code) -> list[dict]:
    """Former residents of the building — the unit is in their PRIOR addresses and their CURRENT
    address is elsewhere — with where they moved to. Reconstructs the building's move-out chain."""
    building = _building(street, city, state, zip_code)
    out, seen = [], set()
    for c in _residents(people):
        from_units = {a.unit for r in c.records for a in r.prior_addresses
                      if a.unit and match_address(building, a) != MatchLevel.NONE}
        now = next((r.current_address for r in c.records if r.current_address
                    and r.current_address.house_number
                    and match_address(building, r.current_address) == MatchLevel.NONE), None)
        if from_units and now:
            k = c.name_key()
            if k in seen:
                continue
            seen.add(k)
            out.append({"name": c.name, "from_units": sorted(from_units, key=_unit_sort_key),
                        "now_at": now.display()})
    return out


def generate_mermaid(clusters: list[list], chains: list[dict] = None,
                     roster: list[dict] = None) -> str:
    """Generate a Mermaid flowchart from household clusters and move-chains.
    
    Nodes = people, edges = shared phone/relative link or move path.
    Returns the Mermaid code string (without the ```mermaid fences).
    """
    lines = ["graph LR"]
    node_ids = {}
    counter = [0]
    
    def _id(name: str) -> str:
        key = name.upper()
        if key not in node_ids:
            counter[0] += 1
            node_ids[key] = f"P{counter[0]}"
        return node_ids[key]
    
    def _label(name: str) -> str:
        """Sanitize name for Mermaid node labels."""
        return name.replace('"', "'").replace('[', '(').replace(']', ')')
    
    # Household clusters: connect members
    for i, cluster in enumerate(clusters or []):
        if len(cluster) < 2:
            continue
        for j, member in enumerate(cluster):
            nid = _id(member.name)
            lines.append(f'    {nid}["{_label(member.name)}"]')
            if j > 0:
                prev_id = _id(cluster[j-1].name)
                lines.append(f'    {prev_id} --- {nid}')
    
    # Move chains: directed edges from old unit to new address
    for ch in (chains or []):
        src_id = _id(ch["name"])
        lines.append(f'    {src_id}["{_label(ch["name"])}"]')
        dest_label = _label(ch["now_at"][:40])
        dest_id = f"D{counter[0]+1}"
        counter[0] += 1
        lines.append(f'    {dest_id}["{dest_label}"]')
        unit_label = ", ".join(ch["from_units"])
        lines.append(f'    {src_id} -->|"from unit {unit_label}"| {dest_id}')
    
    # Roster: show unit -> person links
    for row in (roster or []):
        top = row.get("top")
        if top:
            uid = f"U{row['unit']}"
            pid = _id(top.name)
            lines.append(f'    {uid}["Unit {row["unit"]}"]')
            lines.append(f'    {pid}["{_label(top.name)}"]')
            style = "==>" if row["status"] == "current" else "-->"
            lines.append(f'    {uid} {style} {pid}')
    
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


def trace_relatives(people, street, city, state, zip_code) -> list[dict]:
    """Multi-hop: for each confirmed current resident, check if any of their relatives
    appear anywhere in the data (potentially from a different building's scrape stored in history).
    Returns cross-building connections."""
    building = _building(street, city, state, zip_code)
    cands = _residents(people)
    
    # Build a lookup of all known people by name_key
    all_keys = {}
    for c in cands:
        all_keys[c.name_key()] = c
    
    connections = []
    for c in cands:
        # Only trace from current residents (those with an address matching the building)
        has_building_addr = False
        for r in c.records:
            if r.current_address and match_address(building, r.current_address) != MatchLevel.NONE:
                has_building_addr = True
                break
        if not has_building_addr:
            continue
        
        for rel_name in c.relatives:
            rel_key = name_key(rel_name)
            rel_cand = all_keys.get(rel_key)
            if rel_cand:
                # Check if the relative lives at a DIFFERENT building
                for r in rel_cand.records:
                    if r.current_address and r.current_address.house_number \
                            and match_address(building, r.current_address) == MatchLevel.NONE:
                        connections.append({
                            "resident": c.name,
                            "relative": rel_cand.name,
                            "relative_address": r.current_address.display(),
                        })
                        break
    
    # Deduplicate
    seen = set()
    unique = []
    for conn in connections:
        key = (conn["resident"].upper(), conn["relative"].upper())
        if key not in seen:
            seen.add(key)
            unique.append(conn)
    return unique

