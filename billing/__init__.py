"""
billing/__init__.py
Formats billing data and calls the Medical Billing Bot.

Output columns:
  date_of_birth, last_name, first_name, PHN, date_of_service,
  billing_item, diagnosis, location, province, start_time, end_time

doc_last_name / doc_first_name / location are fixed in the uploaded .xlsx template.
"""

import sys
import os

# BC virtual-care billing items (update as needed)
# 13437 = GP telephone / video visit (standard & counselling)
# TODO: add age-based overrides once confirmed (e.g. under-2, over-65)
_BILLING_ITEMS = {
    "standard":    "13437",
    "counselling": "13437",
}


def _get_billing_item(visit_type: str, patient_dob: str = "") -> str:
    """Return the billing item code for the given visit type."""
    return _BILLING_ITEMS.get(visit_type, "13437")


def submit_billing(
    patient_name: str,
    patient_dob: str,
    health_card: str,
    icd9_codes: list,
    visit_type: str = "standard",
    date_of_service: str = "",
    start_time: str = "",
    end_time: str = "",
    province: str = "BC",
    location: str = "V",
) -> dict:
    """
    Format billing data and pass to the Medical Billing Bot.

    patient_name    : "First Last"  — split into first_name / last_name
    health_card     : PHN
    icd9_codes      : list of code strings/ints  → joined as diagnosis
    visit_type      : standard | counselling
    date_of_service : YYYY-MM-DD
    start_time      : HH:MM
    end_time        : HH:MM
    province        : BC (default)
    location        : V  (virtual, default)
    """
    # Split "First Last" → first / last  (handles middle names)
    parts = patient_name.strip().split()
    last_name  = parts[-1] if len(parts) > 1 else patient_name
    first_name = " ".join(parts[:-1]) if len(parts) > 1 else ""

    billing_item = _get_billing_item(visit_type, patient_dob)
    diagnosis    = ", ".join(str(c) for c in icd9_codes) if icd9_codes else ""

    row = {
        "date_of_birth":   patient_dob,
        "last_name":       last_name,
        "first_name":      first_name,
        "PHN":             health_card,
        "date_of_service": date_of_service,
        "billing_item":    billing_item,
        "diagnosis":       diagnosis,
        "location":        location,
        "province":        province,
        "start_time":      start_time,
        "end_time":        end_time,
    }

    billing_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "Medical-Billing-Bot"
    )
    billing_path = os.path.normpath(billing_path)
    if billing_path not in sys.path:
        sys.path.insert(0, billing_path)

    try:
        from billing_bot import export_to_excel
        result = export_to_excel(**row)
        return {"success": True, "result": result, "data": row}
    except ImportError:
        return {
            "success": False,
            "error": "Medical Billing Bot not found. Check setup.",
            "data": row,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "data": row}
