"""
gsheets_connector.py
--------------------
Google Sheets connection helper using gspread and a service account.

Dependencies:
    pip install gspread google-auth

Environment variables (recommended):
    GSHEETS_CREDENTIALS_FILE : path to the service account JSON key file
    GSHEETS_SPREADSHEET_ID   : the ID of the target spreadsheet
"""

import os
from typing import Any, Dict, List, Optional, Union

import gspread
from google.oauth2.service_account import Credentials


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
def load_config(
    credentials_file: Optional[str] = None,
    spreadsheet_id: Optional[str] = None,
) -> tuple:
    """
    Load credentials file path and spreadsheet ID.
    Values can be passed directly or read from environment variables.
    """
    creds = credentials_file or os.getenv("GSHEETS_CREDENTIALS_FILE")
    sid = spreadsheet_id or os.getenv("GSHEETS_SPREADSHEET_ID")
    if not creds:
        raise ValueError(
            "No credentials file provided. "
            "Set GSHEETS_CREDENTIALS_FILE environment variable or pass it directly."
        )
    if not sid:
        raise ValueError(
            "No spreadsheet ID provided. "
            "Set GSHEETS_SPREADSHEET_ID environment variable or pass it directly."
        )
    return creds, sid


# ----------------------------------------------------------------------
# Google Sheets client and helpers
# ----------------------------------------------------------------------
def authorize(credentials_file: str) -> gspread.Client:
    """
    Authenticate using a service account JSON key and return a gspread client.

    Args:
        credentials_file: Path to the service account JSON key file.

    Returns:
        gspread.Client authorised instance.
    """
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(credentials_file, scopes=scopes)
    return gspread.authorize(creds)


def get_spreadsheet(client: gspread.Client, spreadsheet_id: str) -> gspread.Spreadsheet:
    """
    Open a spreadsheet by its ID.

    Args:
        client: Authorised gspread client.
        spreadsheet_id: The ID from the spreadsheet URL.

    Returns:
        gspread.Spreadsheet object.
    """
    return client.open_by_key(spreadsheet_id)


def get_worksheet(
    spreadsheet: gspread.Spreadsheet,
    sheet_identifier: Union[str, int] = 0,
) -> gspread.Worksheet:
    """
    Retrieve a worksheet by title (str) or index (int, 0-based).

    Args:
        spreadsheet: gspread.Spreadsheet object.
        sheet_identifier: Sheet name (str) or index (int). Defaults to the first sheet.

    Returns:
        gspread.Worksheet object.
    """
    if isinstance(sheet_identifier, int):
        return spreadsheet.get_worksheet(sheet_identifier)
    else:
        return spreadsheet.worksheet(sheet_identifier)


# ----------------------------------------------------------------------
# Read Functions
# ----------------------------------------------------------------------
def read_all_records(worksheet: gspread.Worksheet) -> List[Dict[str, Any]]:
    """
    Return all rows of the worksheet as a list of dictionaries.
    The first row is treated as the header.
    """
    return worksheet.get_all_records()


def read_all_values(worksheet: gspread.Worksheet) -> List[List[Any]]:
    """
    Return all cell values as a list of rows (list of lists).
    """
    return worksheet.get_all_values()


def read_range(worksheet: gspread.Worksheet, range_str: str) -> List[List[Any]]:
    """
    Read a specific range (e.g. 'A1:C10' or 'Sheet1!A1:C10').

    Args:
        worksheet: Worksheet object.
        range_str: A1 notation range.

    Returns:
        List of rows (list of lists) containing the cell values.
    """
    return worksheet.get(range_str)


def read_cell(worksheet: gspread.Worksheet, cell: str) -> Any:
    """
    Read a single cell value.

    Args:
        worksheet: Worksheet object.
        cell: Cell address (e.g. 'B3').

    Returns:
        The cell's value (string, number, etc.).
    """
    return worksheet.acell(cell).value


# ----------------------------------------------------------------------
# Write Functions
# ----------------------------------------------------------------------
def write_cell(
    worksheet: gspread.Worksheet,
    cell: str,
    value: Any,
) -> None:
    """
    Write a value to a single cell.

    Args:
        worksheet: Worksheet object.
        cell: Cell address (e.g. 'B3').
        value: The value to write.
    """
    worksheet.update_acell(cell, value)


def write_range(
    worksheet: gspread.Worksheet,
    range_str: str,
    values: List[List[Any]],
) -> None:
    """
    Write values to a range. The shape of 'values' must match the range dimensions.

    Args:
        worksheet: Worksheet object.
        range_str: A1 notation range (e.g. 'A1:C3').
        values: List of rows (list of lists) containing the data.
    """
    worksheet.update(range_str, values)


def append_rows(
    worksheet: gspread.Worksheet,
    rows: List[List[Any]],
    value_input_option: str = "USER_ENTERED",
) -> None:
    """
    Append one or more rows after the last non‑empty row in the sheet.

    Args:
        worksheet: Worksheet object.
        rows: List of rows, each row being a list of values.
        value_input_option: 'RAW' or 'USER_ENTERED' (default: USER_ENTERED,
                            which respects formatting and data types).
    """
    worksheet.append_rows(rows, value_input_option=value_input_option)


def clear_sheet(worksheet: gspread.Worksheet) -> None:
    """
    Clear all content and formatting of the entire worksheet.
    """
    worksheet.clear()


# ----------------------------------------------------------------------
# Convenience: full connection builder
# ----------------------------------------------------------------------
def connect(
    credentials_file: Optional[str] = None,
    spreadsheet_id: Optional[str] = None,
    sheet: Union[str, int, None] = None,
) -> gspread.Worksheet:
    """
    One-step connection: authenticate, open spreadsheet, and optionally get a sheet.

    Args:
        credentials_file: Path to service account JSON (or None to use env).
        spreadsheet_id: Spreadsheet ID (or None to use env).
        sheet: Sheet name or index (0-based). If None, returns the spreadsheet object.

    Returns:
        A gspread.Worksheet object.
    """
    creds_file, sid = load_config(credentials_file, spreadsheet_id)
    client = authorize(creds_file)
    spreadsheet = get_spreadsheet(client, sid)
    if sheet is not None:
        return get_worksheet(spreadsheet, sheet)
    return spreadsheet


# ----------------------------------------------------------------------
# Example usage (uncomment to test)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # Set environment variables or pass paths directly:
    # export GSHEETS_CREDENTIALS_FILE="path/to/service_account.json"
    # export GSHEETS_SPREADSHEET_ID="your_spreadsheet_id"

    try:
        # Open the first worksheet
        ws = connect(sheet=0)

        # --- Read examples ---
        print("All records (dicts):", read_all_records(ws))
        print("Range A1:B2:", read_range(ws, "A1:B2"))

        # --- Write examples ---
        write_cell(ws, "A1", "Hello, World!")
        write_range(ws, "B1:C2", [["Name", "Age"], ["Alice", 30]])
        append_rows(ws, [["Bob", 25], ["Charlie", 35]])

        print("Data written successfully.")

    except Exception as e:
        print("Error:", e)