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
import re
import threading
from datetime import date, datetime, time, timedelta
from openpyxl import load_workbook

_BILLING_EXPORT_LOCK = threading.Lock()

BILLING_EXPORT_HEADERS = (
    "date_of_birth", "last_name", "first_name", "PHN", "date_of_service",
    "doc_last_name", "doc_first_name", "billing_item", "diagnosis",
    "location", "province", "start_time", "end_time",
)


def _get_billing_item(
    visit_type: str,
    patient_dob: str = "",
    date_of_service: str = "",
    prolonged_counselling: bool = False,
) -> str:
    """Choose the telehealth family-practice item for the patient's age band."""
    if prolonged_counselling and visit_type != "counselling":
        raise ValueError("The prolonged telehealth item requires a counselling visit.")

    try:
        dob = datetime.strptime(patient_dob, "%Y-%m-%d").date()
        service_date = datetime.strptime(
            date_of_service, "%Y-%m-%d"
        ).date() if date_of_service else datetime.now().date()
    except ValueError as exc:
        if prolonged_counselling:
            raise ValueError(
                "A valid date of birth is required to select the age-appropriate prolonged counselling item."
            ) from exc
        return "13437"

    age = service_date.year - dob.year - (
        (service_date.month, service_date.day) < (dob.month, dob.day)
    )
    if age < 0:
        if prolonged_counselling:
            raise ValueError("The date of birth cannot be after the date of service.")
        return "13437"
    age_band = (
        "0-1" if age < 2 else
        "2-49" if age < 50 else
        "50-59" if age < 60 else
        "60-69" if age < 70 else
        "70-79" if age < 80 else
        "80+"
    )
    codes = {
        "0-1": ("13237", "13238"),
        "2-49": ("13437", "13438"),
        "50-59": ("13537", "13538"),
        "60-69": ("13637", "13638"),
        "70-79": ("13737", "13738"),
        "80+": ("13837", "13838"),
    }
    visit_code, prolonged_code = codes[age_band]
    return prolonged_code if prolonged_counselling else visit_code


def _normalise_diagnosis(code) -> str:
    """Remove punctuation and keep the first four ICD-9 digits for export."""
    return re.sub(r"\D", "", str(code))[:4]


def normalise_diagnosis_value(value) -> str:
    """Normalise one or more comma-separated diagnosis codes."""
    return ", ".join(
        normalized
        for code in str(value or "").split(",")
        if (normalized := _normalise_diagnosis(code))
    )


def first_billing_diagnosis(codes) -> str:
    """Return only the first usable diagnosis code for a billing row."""
    if isinstance(codes, str):
        codes = [codes]
    for value in codes or []:
        for code in str(value).split(","):
            if normalized := _normalise_diagnosis(code):
                return normalized
    return ""


def round_billing_time(value: str) -> str:
    """Round an HH:MM time to the nearest five-minute increment."""
    parsed = datetime.strptime(value, "%H:%M")
    total_minutes = parsed.hour * 60 + parsed.minute
    rounded_minutes = ((total_minutes + 2) // 5 * 5) % (24 * 60)
    hour, minute = divmod(rounded_minutes, 60)
    return f"{hour:02d}:{minute:02d}"


def add_elapsed_time(start_time: str, elapsed_seconds: int) -> str:
    """Return the rounded end time after the elapsed session duration."""
    start = datetime.strptime(start_time, "%H:%M")
    end = start + timedelta(seconds=elapsed_seconds)
    return round_billing_time(end.strftime("%H:%M"))


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    raise ValueError(f"Unrecognized billing service date: {value!r}")


def _as_time(value) -> time:
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        for format_string in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(value, format_string).time()
            except ValueError:
                continue
    raise ValueError(f"Unrecognized billing time: {value!r}")


def _interval_for_row(service_date, start_time, end_time):
    start = datetime.combine(_as_date(service_date), _as_time(start_time))
    end = datetime.combine(_as_date(service_date), _as_time(end_time))
    if end < start:
        end += timedelta(days=1)
    if end == start:
        raise ValueError("Billing start and end times must define a positive-duration visit.")
    return start, end


def _round_up_to_five_minutes(value: datetime) -> datetime:
    rounded = datetime.strptime(round_billing_time(value.strftime("%H:%M")), "%H:%M")
    rounded = rounded.replace(year=value.year, month=value.month, day=value.day)
    if rounded < value:
        rounded += timedelta(minutes=5)
    return rounded


def _move_row_to_next_open_interval(row: dict, sheet) -> None:
    """Shift a new billing interval forward until it no longer overlaps saved rows."""
    if row.get("start_time") in (None, "") or row.get("end_time") in (None, ""):
        raise ValueError("Billing start and end times are required to prevent overlapping visits.")

    headers = {
        str(sheet.cell(row=1, column=column).value or "").strip(): column
        for column in range(1, sheet.max_column + 1)
    }
    required = ("date_of_service", "start_time", "end_time")
    if any(header not in headers for header in required):
        raise ValueError("Billing workbook is missing service date or time columns.")

    candidate_start, candidate_end = _interval_for_row(
        row["date_of_service"], row["start_time"], row["end_time"]
    )
    duration = candidate_end - candidate_start
    intervals = []
    for row_number in range(2, sheet.max_row + 1):
        values = {
            header: sheet.cell(row=row_number, column=headers[header]).value
            for header in required
        }
        if all(value in (None, "") for value in values.values()):
            continue
        if values["start_time"] in (None, "") and values["end_time"] in (None, ""):
            continue
        if any(values[header] in (None, "") for header in required):
            raise ValueError(
                f"Existing billing row {row_number} has incomplete service timing; "
                "correct it before exporting another row."
            )
        intervals.append(_interval_for_row(
            values["date_of_service"], values["start_time"], values["end_time"]
        ))

    while True:
        conflicts = [
            end for start, end in intervals
            if candidate_start < end and start < candidate_end
        ]
        if not conflicts:
            break
        candidate_start = _round_up_to_five_minutes(max(conflicts))
        candidate_end = candidate_start + duration

    row["date_of_service"] = candidate_start.strftime("%Y-%m-%d")
    row["start_time"] = candidate_start.strftime("%H:%M")
    row["end_time"] = candidate_end.strftime("%H:%M")


def build_billing_export_row(
    patient_name: str,
    patient_dob: str,
    health_card: str,
    icd9_codes: list,
    visit_type: str = "standard",
    date_of_service: str = "",
    start_time: str = "",
    end_time: str = "",
    doc_last_name: str = "Das",
    doc_first_name: str = "Prasenjit",
    province: str = "BC",
    location: str = "V",
    prolonged_counselling: bool = False,
) -> dict:
    """Build one billing row with age-appropriate telehealth service items."""
    parts = patient_name.strip().split()
    last_name = (parts[-1] if len(parts) > 1 else patient_name.strip()).upper()
    first_name = (" ".join(parts[:-1]) if len(parts) > 1 else "").upper()
    return {
        "date_of_birth": patient_dob,
        "last_name": last_name,
        "first_name": first_name,
        "PHN": health_card,
        "date_of_service": date_of_service,
        "doc_last_name": doc_last_name,
        "doc_first_name": doc_first_name,
        "billing_item": _get_billing_item(
            visit_type, patient_dob, date_of_service, prolonged_counselling
        ),
        "diagnosis": first_billing_diagnosis(icd9_codes),
        "location": location,
        "province": province,
        "start_time": start_time,
        "end_time": end_time,
    }


def append_billing_export_row(row: dict, workbook_path: str) -> int:
    """Append one billing row to an existing workbook with the expected schema."""
    if not os.path.isfile(workbook_path):
        raise FileNotFoundError(f"Billing workbook not found: {workbook_path}")

    with _BILLING_EXPORT_LOCK:
        workbook = load_workbook(workbook_path)
        try:
            if "Billing Data" not in workbook.sheetnames:
                raise ValueError("Billing workbook is missing the 'Billing Data' sheet.")
            sheet = workbook["Billing Data"]
            actual_headers = tuple(
                sheet.cell(row=1, column=column).value
                for column in range(1, len(BILLING_EXPORT_HEADERS) + 1)
            )
            if actual_headers != BILLING_EXPORT_HEADERS:
                raise ValueError("Billing workbook columns do not match the expected export format.")

            _move_row_to_next_open_interval(row, sheet)
            next_row = sheet.max_row + 1
            sheet.append([row.get(header, "") for header in BILLING_EXPORT_HEADERS])
            workbook.save(workbook_path)
            return next_row
        finally:
            workbook.close()


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
    prolonged_counselling: bool = False,
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
    last_name  = (parts[-1] if len(parts) > 1 else patient_name).upper()
    first_name = (" ".join(parts[:-1]) if len(parts) > 1 else "").upper()

    billing_item = _get_billing_item(
        visit_type, patient_dob, date_of_service, prolonged_counselling
    )
    diagnosis    = first_billing_diagnosis(icd9_codes)

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
        result = export_to_excel(row)
        return {"success": True, "result": result, "data": row}
    except ImportError:
        return {
            "success": False,
            "error": "Medical Billing Bot not found. Check setup.",
            "data": row,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "data": row}
