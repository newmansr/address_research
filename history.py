"""
history.py - SQLite-based delta tracking for resident records.
Allows the tool to compare the current run against the last run to detect
NEW residents, REMOVED residents, and CONFIRMED residents.
"""
import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                street TEXT,
                zip TEXT,
                unit TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                run_id INTEGER,
                name TEXT,
                status TEXT,
                score INTEGER,
                confidence TEXT,
                json_data TEXT,
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            )
        """)

def record_run(street: str, zip_code: str, unit: str, current, possible, former):
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("INSERT INTO runs (timestamp, street, zip, unit) VALUES (?, ?, ?, ?)",
                       (now, street.lower(), zip_code, unit.lower() if unit else ""))
        run_id = cursor.lastrowid
        
        def insert_people(people_list, status):
            for p in people_list:
                # dump basic info to JSON
                j = json.dumps({
                    "evidence": p.evidence,
                    "relatives": p.relatives,
                    "phones": p.phones
                })
                cursor.execute(
                    "INSERT INTO results (run_id, name, status, score, confidence, json_data) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, p.name, status, p.score, p.confidence, j)
                )
                
        insert_people(current, "current")
        insert_people(possible, "possible")
        insert_people(former, "former")

def get_delta(street: str, zip_code: str, unit: str, current_people: list) -> dict:
    """
    Compare current_people against the last run for this exact street/zip/unit.
    Returns a dict mapping names to their delta tag: "[NEW]", "[CONFIRMED]", or "[REMOVED]".
    If a person was current and is no longer current, they are [REMOVED].
    If they are new, [NEW]. If they were there and still there, [CONFIRMED].
    """
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT run_id FROM runs 
            WHERE street=? AND zip=? AND unit=? 
            ORDER BY timestamp DESC LIMIT 1
        """, (street.lower(), zip_code, unit.lower() if unit else ""))
        row = cursor.fetchone()
        
        if not row:
            return {} # No history
            
        last_run_id = row[0]
        cursor.execute("SELECT name, status FROM results WHERE run_id=?", (last_run_id,))
        last_results = cursor.fetchall()
        
    last_names = {row[0].upper(): row[1] for row in last_results if row[1] == "current"}
    current_names = {p.name.upper() for p in current_people}

    delta = {}
    for c_name in current_names:
        if c_name in last_names:
            delta[c_name] = "[CONFIRMED]"
        else:
            delta[c_name] = "[NEW]"

    for l_name in last_names:
        if l_name not in current_names:
            delta[l_name] = "[REMOVED]"

    return delta


# ── monitoring views (read-only; power the --history dashboard) ──────────────

def last_run(street: str, zip_code: str, unit: str):
    """(timestamp, run_id) of the most recent recorded run for this unit, or None."""
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT timestamp, run_id FROM runs WHERE street=? AND zip=? AND unit=? "
            "ORDER BY timestamp DESC LIMIT 1",
            (street.lower(), zip_code, unit.lower() if unit else "")).fetchone()
    return (row[0], row[1]) if row else None


def get_timeline(street: str, zip_code: str, unit: str, limit: int = 20):
    """Newest-first [(timestamp, [current resident names]), ...] for a unit."""
    init_db()
    out = []
    with sqlite3.connect(DB_PATH) as conn:
        runs = conn.execute(
            "SELECT run_id, timestamp FROM runs WHERE street=? AND zip=? AND unit=? "
            "ORDER BY timestamp DESC LIMIT ?",
            (street.lower(), zip_code, unit.lower() if unit else "", limit)).fetchall()
        for rid, ts in runs:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM results WHERE run_id=? AND status='current'", (rid,)).fetchall()]
            out.append((ts, names))
    return out


def building_changes(street: str, zip_code: str):
    """For every unit with >= 2 recorded runs, the NEW/REMOVED current residents between its two
    most recent runs. Returns [{unit, name, change, when}] - the building's change log."""
    init_db()
    changes = []
    with sqlite3.connect(DB_PATH) as conn:
        units = [r[0] for r in conn.execute(
            "SELECT DISTINCT unit FROM runs WHERE street=? AND zip=?",
            (street.lower(), zip_code)).fetchall()]
        for unit in units:
            runs = conn.execute(
                "SELECT run_id, timestamp FROM runs WHERE street=? AND zip=? AND unit=? "
                "ORDER BY timestamp DESC LIMIT 2",
                (street.lower(), zip_code, unit)).fetchall()
            if len(runs) < 2:
                continue
            (cur_id, cur_ts), (prev_id, _) = runs[0], runs[1]
            cur = {r[0].upper() for r in conn.execute(
                "SELECT name FROM results WHERE run_id=? AND status='current'", (cur_id,)).fetchall()}
            prev = {r[0].upper() for r in conn.execute(
                "SELECT name FROM results WHERE run_id=? AND status='current'", (prev_id,)).fetchall()}
            for n in sorted(cur - prev):
                changes.append({"unit": unit, "name": n, "change": "NEW", "when": cur_ts})
            for n in sorted(prev - cur):
                changes.append({"unit": unit, "name": n, "change": "REMOVED", "when": cur_ts})
    return changes
