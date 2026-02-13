# requirements:
# gspread==6.1.4
# oauth2client==4.1.3

"""
Merge table records into a Google Sheet based on matching headers.

This script takes a list of record dictionaries and merges them into a specified
Google Sheet. It validates that all record columns exist in the sheet and fails
if any columns are missing.
"""

from typing import Any
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json


def main(
    records: list[dict[str, Any]],
    spreadsheet_id: str,
    sheet_name: str,
    service_account_json: dict,
) -> dict:
    """
    Merge records into a Google Sheet by appending them as new rows.

    Args:
        records: List of dictionaries where keys are column names and values are cell values
        spreadsheet_id: The Google Sheets spreadsheet ID
        sheet_name: The name of the worksheet/tab within the spreadsheet
        service_account_json: Service account credentials as a dictionary

    Returns:
        Dictionary with operation results including number of rows added

    Raises:
        ValueError: If any record contains columns not present in the sheet
        gspread.exceptions.GSpreadException: If sheet operations fail
    """

    if not records:
        return {
            "rows_added": 0,
            "spreadsheet_id": spreadsheet_id,
            "sheet_name": sheet_name,
            "message": "No records to add"
        }

    # Handle service account JSON if it's a string (from Windmill variable)
    if isinstance(service_account_json, str):
        service_account_json = json.loads(service_account_json)

    # Authenticate with Google Sheets
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        service_account_json, scopes
    )
    client = gspread.authorize(creds)

    # Open the spreadsheet and worksheet
    spreadsheet = client.open_by_key(spreadsheet_id)
    worksheet = spreadsheet.worksheet(sheet_name)

    # Get existing headers from the first row
    headers = worksheet.row_values(1)

    if not headers:
        raise ValueError(
            f"Sheet '{sheet_name}' has no headers in the first row. "
            "Please initialize the sheet with column headers first."
        )

    # Validate that all record columns exist in the sheet headers
    all_record_columns = set()
    for record in records:
        all_record_columns.update(record.keys())

    missing_columns = all_record_columns - set(headers)
    if missing_columns:
        raise ValueError(
            f"The following columns from records are not present in the sheet: "
            f"{', '.join(sorted(missing_columns))}. "
            f"Sheet columns: {', '.join(headers)}"
        )

    # Create a mapping of column names to indices
    header_indices = {header: idx for idx, header in enumerate(headers)}

    # Convert records to rows matching the sheet's column order
    rows_to_add = []
    for record in records:
        row = [""] * len(headers)  # Initialize with empty strings
        for column_name, value in record.items():
            column_idx = header_indices[column_name]
            # Convert value to string, handling None
            row[column_idx] = "" if value is None else str(value)
        rows_to_add.append(row)

    # Append all rows at once (more efficient than one at a time)
    if rows_to_add:
        worksheet.append_rows(rows_to_add, value_input_option="USER_ENTERED")

    return {
        "rows_added": len(rows_to_add),
        "spreadsheet_id": spreadsheet_id,
        "sheet_name": sheet_name,
        "columns_validated": list(all_record_columns),
        "message": f"Successfully added {len(rows_to_add)} rows to '{sheet_name}'"
    }
