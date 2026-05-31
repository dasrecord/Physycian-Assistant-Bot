"""
llm/soap_generator.py - Calls a locally-running Ollama instance to generate the SOAP note.
Zero cost -- runs entirely on your own hardware.
Install Ollama: https://ollama.com/download/windows
Pull a model:   ollama pull llama3.2:3b
"""

import re
import json
import requests

from config import OLLAMA_MODEL, OLLAMA_URL
from llm.prompts import get_system_prompt, build_soap_prompt

_ICD9_RE = re.compile(r"ICD-?9(?:[\s_-]*(?:CM|code[s]?))?[:\s]+([0-9]{3,5}(?:\.[0-9]{1,2})?)" , re.IGNORECASE)
_ICD9_LIST_RE = re.compile(r"ICD-?9[_\s-]*CODES?\s*:\s*([0-9.,\s]+)", re.IGNORECASE)

_HEADER_RE = re.compile(
    r"^(S|SUBJECTIVE|O|OBJECTIVE|A|ASSESSMENT|P|PLAN"
    r"|MENTAL STATUS EXAM|NEURO EXAM|ICD9_CODES):\s*",
    re.IGNORECASE | re.MULTILINE,
)
_KEY_MAP = {
    "S": "subjective",  "SUBJECTIVE": "subjective",
    "O": "objective",   "OBJECTIVE":  "objective",
    "A": "assessment",  "ASSESSMENT": "assessment",
    "P": "plan",        "PLAN":       "plan",
}
_EXTRA_HEADERS = {"MENTAL STATUS EXAM", "NEURO EXAM"}


class SOAPGenerator:
    """Generates structured SOAP notes via Ollama (local LLM)."""

    def __init__(self, model=OLLAMA_MODEL):
        self.model = model
        self.api_url = f"{OLLAMA_URL}/api/generate"

    def generate(self, transcript, patient_name="", template_config=None, patient_submitted_info=None):
        """Blocking generation -- returns fully parsed note dict."""
        prompt = build_soap_prompt(transcript, patient_name, template_config, patient_submitted_info=patient_submitted_info)
        system = get_system_prompt(template_config)
        raw = self._call_ollama(prompt, system)
        return self._parse(raw)

    def generate_streaming(self, transcript, patient_name="", template_config=None, patient_submitted_info=None):
        """Streaming generation -- yields token strings as they arrive."""
        prompt = build_soap_prompt(transcript, patient_name, template_config, patient_submitted_info=patient_submitted_info)
        system = get_system_prompt(template_config)
        yield from self._call_ollama_streaming(prompt, system)

    def warmup(self):
        """Send a tiny prompt to pre-load the model into RAM."""
        try:
            requests.post(self.api_url, json={
                "model": self.model, "prompt": "Hi.",
                "stream": False, "keep_alive": -1,
                "options": {"num_predict": 1},
            }, timeout=120)
        except Exception:
            pass

    def _call_ollama(self, prompt, system):
        payload = {
            "model": self.model, "system": system, "prompt": prompt,
            "stream": False, "keep_alive": -1,
            "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 2048},
        }
        try:
            resp = requests.post(self.api_url, json=payload, timeout=300)
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            return self._template_fallback(prompt)
        except requests.exceptions.HTTPError as e:
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text
            raise RuntimeError(f"Ollama API error {resp.status_code}: {err_body}") from e
        return resp.json().get("response", "").strip()

    def _call_ollama_streaming(self, prompt, system):
        payload = {
            "model": self.model, "system": system, "prompt": prompt,
            "stream": True, "keep_alive": -1,
            "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 2048},
        }
        try:
            resp = requests.post(self.api_url, json=payload, timeout=300, stream=True)
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            yield self._template_fallback(prompt)
            return
        except requests.exceptions.HTTPError as e:
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text
            raise RuntimeError(f"Ollama API error {resp.status_code}: {err_body}") from e
        for line in resp.iter_lines():
            if line:
                try:
                    data = json.loads(line)
                    token = data.get("response", "")
                    if token:
                        yield token
                    if data.get("done"):
                        break
                except json.JSONDecodeError:
                    continue

    @staticmethod
    def _template_fallback(prompt):
        import re as _re
        m = _re.search(
            r"=== TRANSCRIPT ===\n(.+?)\n=== END TRANSCRIPT ===", prompt, _re.DOTALL)
        transcript = m.group(1).strip() if m else "(transcript not found)"
        lines_out = [
            "WARNING: Ollama offline -- template pre-filled.",
            "Download Ollama: https://ollama.com/download/windows",
            "Then run: ollama pull llama3.2:3b",
            "",
            "S:",
            transcript,
            "",
            "O:",
            "Vitals: Not reported. General: Not examined.",
            "",
            "A:",
            "[Primary diagnosis] - ICD9: ",
            "DDx: , , ",
            "",
            "P:",
            "- Investigations: ",
            "- Medications: ",
            "- Referrals: ",
            "- Patient education: ",
            "- Follow-up: ",
            "",
            "ICD9_CODES: ",
        ]
        return "\n".join(lines_out)

    def _parse(self, raw):
        note = {
            "subjective": "", "objective": "", "assessment": "",
            "plan": "", "icd9_codes": [], "extra_sections": {}, "raw": raw,
        }
        m = _ICD9_LIST_RE.search(raw)
        if m:
            note["icd9_codes"] = [c.strip() for c in m.group(1).split(",") if c.strip()]
        else:
            codes = _ICD9_RE.findall(raw)
            note["icd9_codes"] = list(dict.fromkeys(codes))

        parts = _HEADER_RE.split(raw)
        i = 1
        while i < len(parts) - 1:
            header = parts[i].strip().upper()
            content = _ICD9_LIST_RE.sub("", parts[i + 1]).strip()
            if header in _KEY_MAP:
                note[_KEY_MAP[header]] = content
            elif header in _EXTRA_HEADERS:
                note["extra_sections"][header.lower().replace(" ", "_")] = content
            i += 2
        return note
