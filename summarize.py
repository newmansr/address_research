"""
summarize.py — OPTIONAL plain-English summary via local Ollama.

Deliberately runs over the STRUCTURED, already-scored result — never raw HTML — so the
LLM only narrates a decision the deterministic engine already made. This removes the
hallucination risk that the original design had (where llama3 *decided* who was current
and got it wrong). Fully optional: if Ollama isn't running, callers get None and the
deterministic output stands on its own.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Optional

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3"


def summarize(target_label: str, current, former, building_only: int,
              model: str = OLLAMA_MODEL, timeout: int = 120) -> Optional[str]:
    """Return a short narrative, or None if Ollama is unavailable."""
    if not current and not former:
        return None

    facts = {
        "address": target_label,
        "current_candidates": [
            {"name": c.name, "confidence": c.confidence, "sources": c.sources,
             "evidence": c.evidence} for c in current[:5]
        ],
        "former_residents": [c.name for c in former],
        "building_only_count": building_only,
    }

    prompt = (
        "You are writing a 2-3 sentence plain-English summary of an address-research "
        "result. Use ONLY the structured facts below — do not invent anyone or change the "
        "ranking. State the single most likely current resident and the confidence, then "
        "briefly note any caveats (e.g. only one undated source, or known former residents).\n\n"
        f"FACTS (JSON):\n{json.dumps(facts, indent=2)}\n\nSummary:"
    )

    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()).get("response", "").strip() or None
    except Exception:
        return None  # Ollama not running / model missing — deterministic output is enough.
