# import wmill


from typing import TypedDict
import gspread
from oauth2client.service_account import ServiceAccountCredentials


class PaymentPayload(TypedDict):
    customer_email: str
    amount: float
    customer_id: str
    description: str


def main(
    payload: PaymentPayload,
    spreadsheet_id: str,
    worksheet_name: str,
    service_account_file: str,
    row_identifier_column: str,  # e.g., "customer_id"
    row_identifier_value: str,   # Value to match in that column
    column_mappings: dict[str, str],  # e.g., {"amount": "C", "status": "D"}
) -> dict:
    """
    Updates specific columns in a Google Sheet row matching identifier.
    
    Args:
        column_mappings: Maps payload keys to column letters (A, B, C, etc.)
    
    Raises:
        gspread.exceptions.GSpreadException: If sheet operations fail
        ValueError: If row identifier not found
    """
    
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        service_account_file, scopes
    )
    client = gspread.authorize(creds)
    
    sheet = client.open_by_key(spreadsheet_id).worksheet(worksheet_name)
    
    # Find the row
    try:
        cell = sheet.find(row_identifier_value)
        row_num = cell.row
    except gspread.exceptions.CellNotFound:
        raise ValueError(
            f"Row with {row_identifier_column}={row_identifier_value} not found"
        )
    
    # Update cells
    updates = []
    for payload_key, column_letter in column_mappings.items():
        if payload_key in payload:
            cell_address = f"{column_letter}{row_num}"
            updates.append({
                "range": cell_address,
                "values": [[payload[payload_key]]],
            })
    
    if updates:
        sheet.batch_update(updates)
    
    return {
        "row_updated": row_num,
        "cells_updated": len(updates),
        "spreadsheet_id": spreadsheet_id,
    }