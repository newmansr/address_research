"""
llm_parser.py — Auto-healing parser fallback using local Ollama.

Invoked when a page clears Cloudflare and has results, but the deterministic
regex parser extracts 0 residents (layout drift).
"""
import json
import urllib.request
from resident_core import Person

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3"

def fallback_parse_llm(source: str, text: str, model: str = OLLAMA_MODEL, timeout: int = 120) -> list[Person]:
    """
    Feed the raw text to Ollama and ask it to extract residents as JSON.
    Returns a list of Person objects.
    """
    if len(text) > 30000:
        # truncate slightly if too huge, but usually detail pages are < 30k chars
        text = text[:30000]

    prompt = (
        "You are an expert data extractor. Extract all people listed as residents in the text below.\n"
        "Return the output as a RAW JSON array of objects, with no markdown formatting or other text.\n"
        "Each object must have these keys exactly (use empty strings or lists if missing):\n"
        '{"name": "...", "age": 45, "current_address": "...", "prior_addresses": ["..."], "phones": ["..."], "relatives": ["..."]}\n\n'
        f"TEXT:\n{text}\n\nJSON OUTPUT:"
    )

    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    
    people = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode()).get("response", "").strip()
            
            # Clean up markdown if the LLM hallucinated it
            if out.startswith("```json"):
                out = out[7:]
            if out.startswith("```"):
                out = out[3:]
            if out.endswith("```"):
                out = out[:-3]
            out = out.strip()
            
            data = json.loads(out)
            if not isinstance(data, list):
                data = [data]
                
            from resident_core import normalize_address
            for d in data:
                name = d.get("name", "").strip()
                if not name:
                    continue
                age_val = d.get("age")
                try:
                    age = int(age_val) if age_val else None
                except (ValueError, TypeError):
                    age = None
                
                people.append(Person(
                    name=name,
                    source=source,
                    age=age,
                    current_address=normalize_address(d.get("current_address", "")),
                    prior_addresses=[normalize_address(a) for a in d.get("prior_addresses", [])],
                    phones=d.get("phones", []),
                    relatives=d.get("relatives", []),
                    note="extracted via LLM fallback"
                ))
    except Exception as e:
        print(f"    !! LLM fallback failed: {e}")
        
    return people

def osint_evaluate(name: str, city: str, state: str, model: str = OLLAMA_MODEL, timeout: int = 120, proxy: str = "") -> str:
    """
    Feature 3: Deep Web Traversal & LLM Biographies.
    Use duckduckgo-search to find web snippets across general web, LinkedIn, and news,
    and ask Ollama to write a comprehensive biographic dossier.
    """
    try:
        from duckduckgo_search import DDGS
        ddgs = DDGS(proxies=proxy if proxy else None)
        
        # Gather snippets from multiple targeted searches
        results = []
        loc = f'"{city}" "{state}"' if city and state else ""
        results.extend(ddgs.text(f'"{name}" {loc}', max_results=5) or [])
        results.extend(ddgs.text(f'site:linkedin.com/in/ "{name}" {loc}', max_results=3) or [])
        results.extend(ddgs.news(f'"{name}" {loc}', max_results=3) or [])
        
        if not results:
            return ""
            
        # Deduplicate snippets
        seen = set()
        snippets = []
        for r in results:
            body = r.get('body', '')
            if body not in seen:
                seen.add(body)
                snippets.append(f"- {r.get('title')}: {body}")
        
        snippets_text = "\n".join(snippets)
        
        prompt = (
            f"You are an elite OSINT investigator. Based ONLY on the following web search snippets for '{name}' in '{city}, {state}', "
            f"write a comprehensive but concise biographic summary (3-4 sentences).\n"
            f"Focus on their profession, employer, education, and any notable public or news appearances. "
            f"If there is not enough information to write a biography, state exactly what is known.\n"
            f"Do not hallucinate facts not present in the snippets.\n\n"
            f"SNIPPETS:\n{snippets_text}"
        )
        
        payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
        
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()).get("response", "").strip()
    except Exception as e:
        return f"OSINT evaluation failed: {e}"
