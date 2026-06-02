"""
vocab — bundled medical vocabulary (medications, abbreviations, anatomy, labs).

Lazy-loaded JSON datasets used by:
  - vocab.prompts.build_whisper_prompt   (STT priming)
  - vocab.corrector.correct_transcript   (post-STT cleanup)
  - llm.prompts.build_soap_prompt        (LLM glossary)

If a file `medications_enriched.json` exists in the same data dir (produced
by `python -m vocab.enrich`), it overrides the bundled medications.json.
"""

import json
import re
from pathlib import Path
from functools import lru_cache

DATA_DIR = Path(__file__).parent / "data"


def _load(filename: str) -> dict:
    path = DATA_DIR / filename
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def get_vocab() -> dict:
    """Return the full vocabulary dict (cached)."""
    meds_overlay = DATA_DIR / "medications_enriched.json"
    meds = _load("medications_enriched.json") if meds_overlay.exists() else _load("medications.json")
    abbrev = _load("abbreviations.json")
    anatomy = _load("anatomy.json")
    physio = _load("physiology_labs.json")
    mishears = _load("mishears.json")

    med_list = meds.get("medications", [])
    # Flat lookups
    med_names = set()
    med_by_name = {}
    for m in med_list:
        n = m.get("name", "").strip()
        if not n:
            continue
        med_names.add(n.lower())
        med_by_name[n.lower()] = m
        for b in m.get("brands", []):
            med_names.add(b.lower())
            med_by_name[b.lower()] = m

    abbrev_map = abbrev.get("abbreviations", {})
    # Lower -> canonical-casing
    abbrev_lookup = {k.lower(): k for k in abbrev_map.keys()}

    anatomy_terms = set()
    for terms in anatomy.get("systems", {}).values():
        for t in terms:
            anatomy_terms.add(t.lower())

    labs = set(t.lower() for t in physio.get("labs", []))
    physio_terms = set(t.lower() for t in physio.get("physiology", []))

    mishear_map = mishears.get("mishears", {})
    # Normalize keys to lowercase, single-spaced
    mishear_lookup = {
        re.sub(r"\s+", " ", k.lower().strip()): v
        for k, v in mishear_map.items()
    }

    return {
        "medications":          med_list,
        "med_names":            med_names,         # set of lowercase
        "med_by_name":          med_by_name,       # lower -> full entry
        "abbreviations":        abbrev_map,        # canonical -> {expand, category}
        "abbrev_lookup":        abbrev_lookup,     # lower -> canonical casing
        "anatomy":              anatomy_terms,
        "labs":                 labs,
        "physiology":           physio_terms,
        "mishears":             mishear_lookup,
    }


def lookup_terms_in(text: str) -> dict:
    """Scan `text` and return only vocab entries actually present.

    Returns: {"abbreviations": {CANON: expansion}, "medications": [{name, class}, ...]}
    Used by the LLM prompt builder to inject a focused glossary.
    """
    v = get_vocab()
    lower = text.lower()

    found_abbrev = {}
    for canon in v["abbreviations"]:
        # Match as whole token (allow surrounding non-word chars / punctuation)
        # Special-case ones containing '/' (e.g. c/o) handled by regex with escape.
        pattern = r"(?<![A-Za-z0-9])" + re.escape(canon) + r"(?![A-Za-z0-9])"
        if re.search(pattern, text):
            found_abbrev[canon] = v["abbreviations"][canon]["expand"]

    found_meds = []
    seen = set()
    for name in sorted(v["med_names"], key=len, reverse=True):
        if name in seen:
            continue
        if re.search(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", lower):
            entry = v["med_by_name"].get(name, {})
            canonical = entry.get("name", name)
            if canonical in seen:
                continue
            seen.add(canonical)
            found_meds.append({"name": canonical, "class": entry.get("class", "")})

    return {"abbreviations": found_abbrev, "medications": found_meds}
