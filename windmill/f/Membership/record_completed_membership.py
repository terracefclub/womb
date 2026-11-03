"""Record one mapped Documenso completion in a dedicated Terrace roster."""
import hashlib
import json
import re

import gspread


def main(document_id: int, member: dict, spreadsheet_id: str, sheet_name: str, service_account_json: dict) -> dict:
    if type(document_id) is not int or not 1 <= document_id <= 2**31 - 1:
        raise ValueError("A positive numeric Documenso document ID is required.")
    if not isinstance(member, dict) or member.keys() != {"name", "email", "membership", "installments"}:
        raise ValueError("The completion must contain the four mapped member fields.")
    for field, limit in (("name", 200), ("email", 254)):
        value = member[field]
        if not isinstance(value, str) or not value.strip() or len(value) > limit or not value.isprintable():
            raise ValueError(f"Invalid {field} field.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", member["email"]):
        raise ValueError("Invalid email field.")
    if member["membership"] not in ("standard", "rca", "abroad", "sophomore"):
        raise ValueError("Unknown membership choice.")
    if type(member["installments"]) is not int or member["installments"] not in (1, 2):
        raise ValueError("Invalid installment choice.")
    if not isinstance(spreadsheet_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", spreadsheet_id):
        raise ValueError("A spreadsheet ID is required.")

    try:
        if isinstance(service_account_json, str):
            service_account_json = json.loads(service_account_json)
        client = gspread.service_account_from_dict(service_account_json, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client.set_timeout((5, 20))
        book = client.open_by_key(spreadsheet_id)
        sheet = book.worksheet(sheet_name)
        headers = sheet.row_values(1)
    except Exception:
        raise RuntimeError("Could not read the roster. Check the resource, tab, and Sheets availability.") from None
    if any(headers.count(field) != 1 for field in member):
        raise ValueError("The roster must have each required header exactly once.")

    cells = [{} for _ in headers]
    for field, value in member.items():
        cell_type = "numberValue" if field == "installments" else "stringValue"
        cells[headers.index(field)] = {"userEnteredValue": {cell_type: value}}
    fingerprint = hashlib.sha256(json.dumps(member, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    marker = {
        "metadataId": document_id,
        "metadataKey": "terrace.membership.documenso.v1",
        "metadataValue": fingerprint,
        "location": {"sheetId": sheet.id},
        "visibility": "DOCUMENT",
    }
    try:
        book.batch_update({"requests": [
            {"createDeveloperMetadata": {"developerMetadata": marker}},
            {"appendCells": {"sheetId": sheet.id, "rows": [{"values": cells}], "fields": "userEnteredValue"}},
        ]})
        return {"status": "recorded"}
    except Exception:
        # A response may be lost after both requests committed. Only an exact marker confirms success.
        try:
            endpoint = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/developerMetadata/{document_id}"
            stored = book.client.request("get", endpoint).json()
        except Exception:
            raise RuntimeError("Roster write unconfirmed. Check Sheets and replay the same completion.") from None
        if (stored.get("metadataId") == document_id and stored.get("metadataKey") == marker["metadataKey"]
                and stored.get("metadataValue") == fingerprint and stored.get("location", {}).get("sheetId") == sheet.id
                and stored.get("visibility") == marker["visibility"]):
            return {"status": "recorded"}
        raise ValueError("The document ID has a conflicting roster marker. Reconcile before replay.") from None
