"""
billing/__init__.py
Thin wrapper that formats billing data and calls the Medical Billing Bot.
"""

import sys
import os


def submit_billing(
    patient_name: str,
    patient_dob: str,
    health_card: str,
    icd9_codes: list,
    province: str = "BC",
    visit_type: str = "standard",
    duration_minutes: int = 0,
) -> dict:
    """
    Format billing data and pass to the Medical Billing Bot.

    visit_type: standard | prolonged | counselling | urgent
    duration_minutes: session length for visit-type code selection
    """
    billing_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "Medical-Billing-Bot"
    )
    billing_path = os.path.normpath(billing_path)
    if billing_path not in sys.path:
        sys.path.insert(0, billing_path)

    try:
        from billing_bot import export_to_excel

        result = export_to_excel(
            patient_name=patient_name,
            patient_dob=patient_dob,
            health_card=health_card,
            icd9_codes=icd9_codes,
            province=province,
            visit_type=visit_type,
            duration_minutes=duration_minutes,
        )
        return {"success": True, "result": result}
    except ImportError:
        # Billing bot not available -- log and return graceful failure
        return {
            "success": False,
            "error": "Medical Billing Bot not found. Check setup.",
            "data": {
                "patient_name":     patient_name,
                "patient_dob":      patient_dob,
                "health_card":      health_card,
                "icd9_codes":       icd9_codes,
                "province":         province,
                "visit_type":       visit_type,
                "duration_minutes": duration_minutes,
            },
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}
