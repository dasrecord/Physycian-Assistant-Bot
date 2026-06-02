"""
vocab/enrich.py — Optional medication-list enrichment from public APIs.

Usage:
    python -m vocab.enrich --dry-run               # show what would happen, no network
    python -m vocab.enrich --source rxnorm         # fetch from RxNorm
    python -m vocab.enrich --source openfda        # fetch from OpenFDA

Writes vocab/data/medications_enriched.json which (if present) overrides the
bundled medications.json at load time. Never auto-runs.
"""

import argparse
import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
OVERLAY = DATA_DIR / "medications_enriched.json"
BUNDLED = DATA_DIR / "medications.json"


def _load_bundled() -> dict:
    with open(BUNDLED, encoding="utf-8") as f:
        return json.load(f)


def _fetch_rxnorm() -> list:
    """Fetch RxNorm display-name list (no auth). Returns list of generic names."""
    import requests
    url = "https://rxnav.nlm.nih.gov/REST/displaynames.json"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json().get("displayTermsList", {}).get("term", [])


def _fetch_openfda(limit: int = 1000) -> list:
    """Fetch a sample of OpenFDA drug labels and return their generic names."""
    import requests
    url = "https://api.fda.gov/drug/label.json"
    names = []
    skip = 0
    page = 100
    while skip < limit:
        r = requests.get(url, params={"limit": page, "skip": skip}, timeout=30)
        if not r.ok:
            break
        for entry in r.json().get("results", []):
            openfda = entry.get("openfda", {})
            for n in openfda.get("generic_name", []) or []:
                names.append(n.lower())
        skip += page
    return list(set(names))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["rxnorm", "openfda"], help="API to fetch from")
    ap.add_argument("--dry-run", action="store_true", help="No network, show plan only")
    ap.add_argument("--limit", type=int, default=1000, help="OpenFDA record limit")
    args = ap.parse_args()

    bundled = _load_bundled()
    bundled_names = {m["name"].lower() for m in bundled.get("medications", [])}
    print(f"[enrich] bundled medications: {len(bundled_names)}")

    if args.dry_run:
        print("[enrich] dry-run mode -- no network calls. Source would be:", args.source or "(none)")
        return 0
    if not args.source:
        ap.error("--source required (or use --dry-run)")

    print(f"[enrich] fetching from {args.source} ...")
    if args.source == "rxnorm":
        fetched = _fetch_rxnorm()
    else:
        fetched = _fetch_openfda(limit=args.limit)
    print(f"[enrich] fetched {len(fetched)} names")

    # Merge: keep bundled entries as-is, add new names as minimal entries.
    merged = list(bundled.get("medications", []))
    added = 0
    for name in fetched:
        key = name.strip().lower()
        if not key or key in bundled_names:
            continue
        merged.append({"name": key, "brands": [], "class": "", "freq": 4})
        bundled_names.add(key)
        added += 1

    overlay = {
        "_meta": {"source": args.source, "added": added, "total": len(merged)},
        "medications": merged,
    }
    OVERLAY.write_text(json.dumps(overlay, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[enrich] wrote {OVERLAY} (added {added}, total {len(merged)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
