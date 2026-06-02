"""
vocab/corrector.py — Conservative post-transcription correction.

Rules applied (in order, all safe):
  1. Curated mishear map (e.g. "lazy x" -> "Lasix"). Whole-phrase, case-insensitive.
  2. Sliding-window token rejoin: 2-4 consecutive tokens that collapse to a
     known medication name when whitespace is removed (e.g. "met form in"
     -> "metformin"). Only matched against the exact bundled medication set.
  3. Abbreviation casing normalization (e.g. "htn" -> "HTN", "pmhx" -> "PMHx").

No fuzzy matching, no single-token rewrites, no class-substitution. Every
change is recorded in the returned changes log for clinician audit.
"""

import re
from vocab import get_vocab


_SPEAKER_PREFIX_RE = re.compile(r"^(Dr|Pt):\s*", re.IGNORECASE)


def _apply_mishears(text: str, mishears: dict, changes: list) -> str:
    for phrase, canon in mishears.items():
        # Whole-phrase, case-insensitive, word-boundary-ish.
        pattern = r"(?<![A-Za-z])" + re.escape(phrase) + r"(?![A-Za-z])"
        def _sub(m, _canon=canon, _phrase=phrase):
            changes.append({"type": "mishear", "from": m.group(0), "to": _canon})
            return _canon
        text = re.sub(pattern, _sub, text, flags=re.IGNORECASE)
    return text


def _apply_token_rejoin(text: str, med_names: set, changes: list) -> str:
    """Walk each line; for any window of 2-4 tokens whose concat (lowercased,
    alpha-only) matches a known med name, collapse to the canonical spelling."""
    out_lines = []
    for line in text.splitlines():
        prefix = ""
        m = _SPEAKER_PREFIX_RE.match(line)
        if m:
            prefix = m.group(0)
            body = line[m.end():]
        else:
            body = line

        # Tokenize preserving punctuation as separate tokens.
        tokens = re.findall(r"\w+|[^\w\s]+|\s+", body)
        i = 0
        new_tokens = []
        while i < len(tokens):
            matched = False
            # Try longest window first (4 -> 2 word-tokens)
            for window in (4, 3, 2):
                # Collect the next `window` *word* tokens with their indices.
                word_idxs = []
                j = i
                while j < len(tokens) and len(word_idxs) < window:
                    if re.match(r"^\w+$", tokens[j]):
                        word_idxs.append(j)
                    j += 1
                if len(word_idxs) < window:
                    continue
                joined = "".join(tokens[k] for k in word_idxs).lower()
                if not joined.isalpha():
                    continue
                if joined in med_names:
                    canonical = _canonical_med_name(joined)
                    original = "".join(tokens[i:word_idxs[-1] + 1])
                    changes.append({"type": "rejoin", "from": original, "to": canonical})
                    new_tokens.append(canonical)
                    i = word_idxs[-1] + 1
                    matched = True
                    break
            if not matched:
                new_tokens.append(tokens[i])
                i += 1
        out_lines.append(prefix + "".join(new_tokens))
    return "\n".join(out_lines)


def _canonical_med_name(lower_name: str) -> str:
    """Return the canonical-cased medication name (brand stays Title Case)."""
    v = get_vocab()
    entry = v["med_by_name"].get(lower_name, {})
    # If the lower_name matches a brand entry, return that brand's original casing.
    for m in v["medications"]:
        for b in m.get("brands", []):
            if b.lower() == lower_name:
                return b
        if m.get("name", "").lower() == lower_name:
            return m["name"]
    return entry.get("name", lower_name)


def _apply_abbrev_casing(text: str, abbrev_lookup: dict, changes: list) -> str:
    """Normalize casing of known abbreviations (only when token differs by case)."""
    # Build a regex that matches any abbreviation token (longest first to avoid
    # `T` swallowing `TSH`). Use word-boundary that respects '/' for c/o etc.
    keys = sorted(abbrev_lookup.values(), key=len, reverse=True)
    if not keys:
        return text

    def _sub(m):
        original = m.group(0)
        canonical = abbrev_lookup.get(original.lower())
        if canonical and canonical != original:
            changes.append({"type": "abbrev_case", "from": original, "to": canonical})
            return canonical
        return original

    # (?<![A-Za-z0-9]) ... (?![A-Za-z0-9]) so we don't break "Pt" speaker labels etc.
    pattern = r"(?<![A-Za-z0-9])(" + "|".join(re.escape(k) for k in keys) + r")(?![A-Za-z0-9])"
    return re.sub(pattern, _sub, text, flags=re.IGNORECASE)


def correct_transcript(text: str) -> tuple:
    """Return (corrected_text, changes_list). Pure function."""
    if not text:
        return text, []
    v = get_vocab()
    changes = []
    text = _apply_mishears(text, v["mishears"], changes)
    text = _apply_token_rejoin(text, v["med_names"], changes)
    text = _apply_abbrev_casing(text, v["abbrev_lookup"], changes)
    return text, changes
