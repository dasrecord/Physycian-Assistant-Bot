"""
Quick smoke tests for vocab package. Run with:
    python -m vocab.tests.test_vocab
"""

import sys
from vocab import get_vocab, lookup_terms_in
from vocab.prompts import build_whisper_prompt
from vocab.corrector import correct_transcript


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}{(' -- ' + detail) if detail else ''}")
    if not cond:
        check.failed = True


check.failed = False


def main():
    v = get_vocab()
    check("medications loaded", len(v["medications"]) > 100, f"{len(v['medications'])} entries")
    check("abbreviations loaded", "HTN" in v["abbreviations"])
    check("anatomy loaded", "aorta" in v["anatomy"])

    # Whisper prompt
    p_default = build_whisper_prompt()
    check("whisper prompt default <=950 chars", len(p_default) <= 950, f"len={len(p_default)}")
    p_psych = build_whisper_prompt("psych")
    check("whisper prompt psych contains MDD", "MDD" in p_psych)
    p_cardiac = build_whisper_prompt("cardiac")
    check("whisper prompt cardiac contains AFib", "AFib" in p_cardiac)

    # Corrector: mishear map
    text, changes = correct_transcript("Pt: I take lazy x 40 mg every morning.")
    check("mishear lazy x -> Lasix", "Lasix" in text and any(c["to"] == "Lasix" for c in changes))

    # Corrector: token rejoin (only meds with multi-token mishear of "metformin" via spaces)
    text, changes = correct_transcript("Pt: I am on met form in 500 mg BID.")
    check("rejoin met form in -> metformin", "metformin" in text and any(c["to"] == "metformin" for c in changes))

    # Corrector: abbreviation casing
    text, changes = correct_transcript("Dr: htn well controlled, hba1c stable.")
    check("abbrev casing htn -> HTN", "HTN" in text)
    check("abbrev casing hba1c -> HbA1c", "HbA1c" in text)

    # No-change safety: normal English should not be rewritten.
    safe = "The patient feels well today and walked around the park."
    text, changes = correct_transcript(safe)
    check("no-change on plain English", text == safe and not changes,
          f"changes={changes}")

    # Lookup terms in text
    found = lookup_terms_in("Pt with HTN on metformin and HbA1c 7.2.")
    check("lookup finds HTN", "HTN" in found["abbreviations"])
    check("lookup finds metformin", any(m["name"] == "metformin" for m in found["medications"]))

    print("\n" + ("FAILED" if check.failed else "ALL PASSED"))
    sys.exit(1 if check.failed else 0)


if __name__ == "__main__":
    main()
