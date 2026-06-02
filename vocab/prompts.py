"""
vocab/prompts.py — Build Whisper initial_prompt strings.

Whisper's initial_prompt is limited to ~224 tokens (~1000 chars). We pick
high-yield tokens (top-frequency meds + common abbreviations) and optionally
add a small specialty pack tailored to the visit type.
"""

from vocab import get_vocab

_MAX_PROMPT_CHARS = 950  # safety margin under Whisper's 224-token limit

_BASE_INTRO = (
    "Family medicine consultation between doctor and patient. "
    "Chief complaint, history, physical exam, diagnoses, prescriptions."
)

# Visit-type specific token packs to bias Whisper recognition.
_SPECIALTY_PACKS = {
    "psych":     "MDD, GAD, PTSD, OCD, ADHD, SI, MSE, suicidal ideation, sertraline, escitalopram, quetiapine, lithium, lamotrigine.",
    "cardiac":   "HTN, CAD, CHF, MI, AFib, DVT, PE, ECG, echo, troponin, metoprolol, bisoprolol, ramipril, apixaban, atorvastatin.",
    "diabetes":  "T2DM, HbA1c, fasting glucose, metformin, empagliflozin, semaglutide, Ozempic, Jardiance, insulin glargine.",
    "respiratory": "COPD, asthma, URI, pneumonia, SOB, DOE, CXR, salbutamol, Ventolin, Spiriva, Advair, Symbicort, fluticasone.",
    "GI":        "GERD, IBS, PUD, N/V/D, pantoprazole, omeprazole, esomeprazole, ondansetron, loperamide.",
    "GU":        "UTI, BPH, CKD, hematuria, dysuria, nitrofurantoin, Macrobid, ciprofloxacin, tamsulosin, finasteride.",
    "MSK":       "OA, RA, LBP, ROM, sprain, strain, ibuprofen, naproxen, acetaminophen, prednisone, methotrexate.",
}

# Map visit_type values produced by the UI to a specialty pack key.
_VISIT_TYPE_MAP = {
    "psych":         "psych",
    "psychiatric":   "psych",
    "mental_health": "psych",
    "cardiac":       "cardiac",
    "cardio":        "cardiac",
    "diabetes":      "diabetes",
    "endocrine":     "diabetes",
    "respiratory":   "respiratory",
    "asthma":        "respiratory",
    "copd":          "respiratory",
    "gi":            "GI",
    "gerd":          "GI",
    "gu":            "GU",
    "urinary":       "GU",
    "msk":           "MSK",
    "ortho":         "MSK",
}


def _top_med_names(limit: int) -> list:
    """Return the top-N most-commonly-prescribed med generic names (freq=1)."""
    v = get_vocab()
    freq1, freq2 = [], []
    for m in v["medications"]:
        n = m.get("name", "")
        if not n:
            continue
        if m.get("freq", 5) == 1:
            freq1.append(n)
        elif m.get("freq", 5) == 2:
            freq2.append(n)
    return (freq1 + freq2)[:limit]


def _abbrev_csv(limit: int) -> str:
    v = get_vocab()
    # Pick the highest-yield abbreviations (history + common dx + vitals).
    priority = (
        "PMHx", "FHx", "SHx", "ROS", "HPI", "c/o", "h/o", "s/p", "r/o",
        "SOB", "N/V/D", "HTN", "DM2", "T2DM", "GERD", "URI", "UTI", "CAD",
        "CHF", "COPD", "CKD", "OA", "RA", "BP", "HR", "RR", "SpO2", "BMI",
        "HbA1c", "eGFR", "TSH", "INR", "CBC", "BMP", "ECG", "CXR",
    )
    have = v["abbreviations"]
    chosen = [a for a in priority if a in have][:limit]
    return ", ".join(chosen)


def build_whisper_prompt(visit_type: str | None = None) -> str:
    """Build a Whisper initial_prompt under ~1000 chars."""
    parts = [_BASE_INTRO]

    abbrev = _abbrev_csv(limit=40)
    if abbrev:
        parts.append(f"Abbreviations: {abbrev}.")

    meds = _top_med_names(limit=40)
    if meds:
        parts.append("Medications: " + ", ".join(meds) + ".")

    pack_key = _VISIT_TYPE_MAP.get((visit_type or "").lower().strip())
    if pack_key and pack_key in _SPECIALTY_PACKS:
        parts.append(_SPECIALTY_PACKS[pack_key])

    prompt = " ".join(parts)
    if len(prompt) > _MAX_PROMPT_CHARS:
        prompt = prompt[:_MAX_PROMPT_CHARS].rsplit(" ", 1)[0] + "."
    return prompt
