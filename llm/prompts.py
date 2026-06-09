"""
llm/prompts.py - System prompt and user-prompt builder for family-medicine SOAP notes.
Supports template_config for different note types.
"""

try:
    from config import ENABLE_LLM_GLOSSARY
except Exception:
    ENABLE_LLM_GLOSSARY = True
try:
    from vocab import lookup_terms_in
except Exception:
    def lookup_terms_in(_text):
        return {"abbreviations": {}, "medications": []}

SOAP_SYSTEM_PROMPT = (
    "You are an expert Canadian family medicine physician assistant with 20 years of clinical "
    "experience. Your only task is to analyse a doctor-patient consultation transcript and "
    "produce a precise, structured clinical note.\n\n"
    "STRICT RULES - violation of any rule is a critical error:\n"
    "1. Use proper Canadian/UK medical terminology and standard abbreviations "
    "(HPI, PMHx, FHx, SHx, ROS, Hx, Dx, Rx, Tx, c/o, h/o, s/p, r/o, N/V/D, SOB, etc.).\n"
    "2. NEVER fabricate ANY information not explicitly stated in the transcript. "
    "If a piece of information was not mentioned, write Not reported (for history) "
    "or Not examined (for exam findings). "
    "If a medication, term, or concept is unclear, misspelled, or unknown, do NOT guess or invent. "
    "Instead, write: Unknown medication: [original text] or Unknown term: [original text]. "
    "NEVER write phrases like appears well-nourished, no acute distress, chest clear to auscultation, "
    "abdomen soft non-tender, normal range of motion, or ANY other exam finding "
    "unless the transcript explicitly states a physical examination was performed.\n"
    "3. VIRTUAL / REMOTE ENCOUNTERS: If the visit appears to be virtual, telephone, or video "
    "(i.e., no physical examination is described in the transcript), the O: section MUST begin "
    "with: Physical examination: Not performed (virtual encounter). "
    "Only include under O: objective data the patient reported themselves "
    "(e.g., home BP reading, self-reported weight) or results reviewed remotely. "
    "Do NOT add any examination findings, auscultation findings, or inspection findings.\n"
    "4. Vitals: include ONLY vitals explicitly stated in the transcript. "
    "Never invent BP, HR, RR, temperature, SpO2, weight, or height.\n"
    "5. MANDATORY: Use ICD-9-CM codes ONLY (NOT ICD-10). Every diagnosis MUST include its ICD-9 code. "
    "Inline format: Diagnosis - (ICD9: XXX.X)   Summary line format: ICD9_CODES: XXX.X, YYY.Y\n"
    "6. Differential diagnoses: rank by probability, fold into A: as a single DDx line.\n"
    "7. PLAN - only include items explicitly mentioned or clearly implied in the transcript. "
    "OMIT any sub-item entirely if it was not discussed. "
    "Never write 'None ordered', 'None prescribed', 'None', or any placeholder for undiscussed items.\n"
    "8. Auto-detect specialty sections: "
    "If primary Dx is psychiatric include MENTAL STATUS EXAM: after O:. "
    "If primary Dx is neurological include NEURO EXAM: after O:.\n"
    "9. Output ONLY the note in the exact format requested. No preamble, no commentary, no apologies.\n"
    "10. SPEAKER LABELS: The transcript may contain 'Dr:' and 'Pt:' prefixes indicating speaker turns. "
    "Use them to correctly attribute speech — Pt: lines populate S: history; Dr: lines inform A:/P:. "
    "Do NOT include the labels literally in the note.\n"
    "11. FORMATTING: Every sub-section label (HPI, PMHx, FHx, SHx, ROS, Medications, Allergies, "
    "Vitals, Physical examination, Investigations, Referrals, Follow-up, DDx, etc.) MUST start "
    "on its own new line. Never run two sub-sections together on the same line.\n"
)

_BASE_FORMAT = (
    "S:\n"
    "HPI: [Chief complaint, onset, duration, severity, quality, associated symptoms, alleviating/aggravating factors]\n"
    "PMHx: [Past medical history — if not mentioned: Not reported]\n"
    "Medications: [Current medications — if not mentioned: Not reported]\n"
    "Allergies: [Drug/food allergies — if not mentioned: Not reported]\n"
    "FHx: [Family history — if not mentioned: Not reported]\n"
    "SHx: [Social history — if not mentioned: Not reported]\n"
    "ROS: [Review of systems — if not mentioned: Not reported]\n\n"
    "O:\n"
    "Physical examination: [For virtual encounters write: Not performed (virtual encounter). "
    "For in-person: list ONLY vitals and findings explicitly stated in the transcript.]\n"
    "Vitals: [Only vitals explicitly stated — omit entirely if none mentioned]\n\n"
    "A:\n"
    "- [Primary diagnosis] (ICD9: XXX.X)\n"
    "- [Secondary diagnoses if applicable] (ICD9: XXX.X)\n"
    "DDx: [Diagnosis 1], [Diagnosis 2], [Diagnosis 3] (ranked by probability)\n\n"
    "P:\n"
    "[Include ONLY sub-items explicitly discussed. Each sub-item on its OWN line. Omit any sub-item entirely if not mentioned.]\n"
    "Investigations: [tests ordered]\n"
    "Medications: [drug, dose, route, frequency, duration]\n"
    "Referrals: [specialist and reason]\n"
    "Patient education: [topics covered]\n"
    "Follow-up: [interval and return precautions]\n\n"
    "ICD9_CODES: [comma-separated list of every ICD9 code used above]\n"
)


def get_system_prompt(template_config=None):
    # Full override (e.g. non-medical meeting templates) — bypass the medical base prompt entirely.
    if template_config and template_config.get("system_prompt_override"):
        base = template_config["system_prompt_override"].strip() + "\n"
    else:
        base = SOAP_SYSTEM_PROMPT
    if template_config and template_config.get("system_prompt_extra"):
        base = base.rstrip() + "\n" + template_config["system_prompt_extra"].strip() + "\n"
    return base


def _normalise_checklist(text: str) -> str:
    """Convert tab-separated Yes/No symptom checklists into readable lines.

    Input format (copied from a web form with two columns: No | Yes):
      symptom\\t✓\\t        → ✓ in No column  → "NO:  symptom"
      symptom\\t\\t✓        → ✓ in Yes column → "YES: symptom"

    The rule the user observed: a Yes check has an *extra* tab (or space) before
    the ✓ compared with a No check.  In practice the raw paste looks like:
      "black or dark coloured urine\\t✓\\t"   → No
      "stinging / burning with urination\\t\\t✓" → Yes

    Lines that don't match the checklist pattern are left untouched.
    If the block contains a "No / Yes" header pair it is replaced with a
    plain label so the LLM understands the section.
    """
    import re

    # Collapse standalone "No" / "Yes" header lines that appear on consecutive lines
    # (some forms render the two column headers on separate lines)
    text = re.sub(r'(?m)^No\r?\n\s*Yes\s*$', 'Symptom checklist (Yes = present, No = absent):', text)

    lines = text.splitlines()
    out = []
    in_checklist = False

    for line in lines:
        # Detect the column-header row produced by these forms
        stripped = line.strip()
        if re.fullmatch(r'No\s+Yes', stripped) or re.fullmatch(r'No\s*/\s*Yes', stripped):
            out.append("Symptom checklist (Yes = present, No = absent):")
            in_checklist = True
            continue

        # Only attempt checklist parsing once we've seen the header,
        # or if the line itself strongly looks like a checklist row.
        if '✓' not in line:
            out.append(line)
            continue

        # Split on tab; ✓ position relative to symptom text tells us the column.
        # Pattern A: "symptom\t✓\t..."  → No column (one tab before ✓)
        # Pattern B: "symptom\t\t✓..."  → Yes column (two or more tabs before ✓)
        # We also handle spaces-as-tabs for robustness.
        m_yes = re.match(r'^(.+?)\t{2,}✓', line)
        m_no  = re.match(r'^(.+?)\t✓',     line)

        if m_yes:
            symptom = m_yes.group(1).strip()
            out.append(f"YES: {symptom}")
            in_checklist = True
        elif m_no:
            symptom = m_no.group(1).strip()
            out.append(f"NO:  {symptom}")
            in_checklist = True
        else:
            out.append(line)

    return "\n".join(out)


def build_soap_prompt(transcript, patient_name="", template_config=None, patient_submitted_info=None):
    is_meeting = bool(template_config and template_config.get("category") == "meeting")

    if is_meeting:
        title_line = f"Meeting / Subject: {patient_name}\n" if patient_name else ""
        intro = (
            "Analyse the following meeting / conversation transcript and generate "
            "structured notes. Strictly follow ALL rules from the system prompt.\n\n"
        )
        pt_line = title_line
    else:
        pt_line = f"Patient: {patient_name}\n" if patient_name else ""
        intro = (
            "Analyse the following doctor-patient consultation transcript and generate "
            "a complete clinical note. Strictly follow ALL rules from the system prompt.\n\n"
        )
    output_format = (
        template_config.get("output_format") or _BASE_FORMAT
        if template_config else _BASE_FORMAT
    )
    prompt = (
        f"{pt_line}{intro}"
        f"=== TRANSCRIPT ===\n{transcript}\n=== END TRANSCRIPT ===\n\n"
    )

    # Inject a focused glossary of terms / meds actually present in the transcript.
    if ENABLE_LLM_GLOSSARY and not is_meeting:
        found = lookup_terms_in(transcript)
        glossary_lines = []
        for canon, expand in sorted(found.get("abbreviations", {}).items()):
            glossary_lines.append(f"  {canon}: {expand}")
        for m in found.get("medications", [])[:40]:
            cls = f" ({m['class']})" if m.get("class") else ""
            glossary_lines.append(f"  {m['name']}{cls}")
        if glossary_lines:
            prompt += (
                "=== GLOSSARY (reference only; do not copy verbatim, use to disambiguate the transcript) ===\n"
                + "\n".join(glossary_lines)
                + "\n=== END GLOSSARY ===\n\n"
            )

    if patient_submitted_info:
        patient_submitted_info = _normalise_checklist(patient_submitted_info)
        if is_meeting:
            prompt += (
                "\n==============================\n"
                "PRE-MEETING CONTEXT / NOTES (provided ahead of the meeting):\n"
                f"{patient_submitted_info.strip()}\n"
                "==============================\n"
                " IMPORTANT: Use this context to enrich the minutes (e.g., agenda items, attendee roles, background) but do NOT fabricate decisions or action items that did not occur in the transcript.\n"
            )
        else:
            prompt += (
                "\n==============================\n"
                "PATIENT-SUPPLIED INFORMATION (provided by patient, e.g., intake form, email):\n"
                "(Format: bullet points or labeled fields. Example: 'Allergies: No known drug allergies')\n"
                f"{patient_submitted_info.strip()}\n"
                "==============================\n"
                " IMPORTANT: You MUST extract and merge all clinically relevant details from BOTH the transcript and the patient-supplied info. For each SOAP section (especially Allergies, Medications, PMHx, SHx), always check both sources. If the patient-supplied info contains allergy or medication details, ensure they are reflected in the note, unless contradicted by the transcript. If both sources mention the same field, prefer the transcript."
            )
    prompt += (
        f"Generate the note now. Exact format required, no extra commentary:\n\n"
        f"{output_format}\n"
    )
    return prompt
