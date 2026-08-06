from __future__ import annotations

import json
import datetime
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request
import gspread
from gspread.utils import ValueInputOption

from . import sheets as sheets_auth
from .reporting import (
    card_stats,
    _format_duration,
    _format_gb,
    _now_12h_time,
)

# ----------------------------------------------------------------------
# Paths – db.json only stores spreadsheetId
# ----------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_FILE = PROJECT_ROOT / "db.json"
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"

def read_db() -> Dict[str, Any]:
    if DB_FILE.exists():
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def write_db(data: Dict[str, Any]) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_spreadsheet_id() -> Optional[str]:
    return read_db().get("spreadsheetId")


def set_spreadsheet_id(sheet_id: str) -> None:
    db = read_db()
    db["spreadsheetId"] = sheet_id
    write_db(db)


def _parse_gb_string(gb_str: str) -> float:
    """Converts '1,025.65 GB' or '1025.65' into a float."""
    if not gb_str:
        return 0.0
    clean_str = str(gb_str).replace(" GB", "").replace(",", "").strip()
    try:
        return float(clean_str)
    except ValueError:
        return 0.0

def _parse_duration_to_seconds(time_str: str) -> float:
    """Converts '88:52:04' into total seconds."""
    if not time_str:
        return 0.0
    parts = str(time_str).split(":")
    if len(parts) == 3:
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return 0.0
    return 0.0

def _format_hours_mins(seconds: float) -> str:
    """Converts seconds to '88h 52m' format."""
    if not seconds or seconds < 0:
        return "0h 0m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"

def _summary_headers() -> list[str]:
    """Headers for the Summary sheet."""
    return [
        "Date",
        "Total cards received",
        "Total Used Space (GB) Before Labeling",
        "Total Used Space (GB) After Labeling",
        "Total storage before (TB)",
        "Total storage after (TB)",
        "Total Original Duration Before Labeling",
        "Total Original Duration After Labeling",
        "Total Hours Before",
        "Total Hours After"
    ]

def _ensure_summary_sheet():
    """Returns the Summary worksheet, creating it with headers if it doesn't exist."""
    sheet_name = "Summary"
    try:
        ws = get_sheet(sheet_name)
    except RuntimeError:
        try:
            create_new_worksheet(sheet_name)
            ws = get_sheet(sheet_name)
            ws.append_rows([_summary_headers()], value_input_option=ValueInputOption.user_entered)
        except Exception:
            ws = get_sheet(sheet_name)
    return ws

def _style_vertical_summary_card(ws: gspread.Worksheet, start_row: int) -> None:
    """Styles a 11-row vertical summary card starting at the given row."""
    try:
        # 1. Merge Header Columns (B and C)
        ws.merge_cells(f"B{start_row}:C{start_row}")
        
        # 2. Format Header (Purple background, white bold text)
        ws.format(f"B{start_row}:C{start_row}", {
            "backgroundColor": {"red": 0.45, "green": 0.35, "blue": 0.95}, # Vibrant purple
            "textFormat": {
                "fontFamily": "Lexend",
                "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                "fontSize": 14,
                "bold": True
            },
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE"
        })
        
        # 3. Format Labels in Col B (Right aligned, bold)
        ws.format(f"B{start_row+1}:B{start_row+10}", {
            "textFormat": {
                "fontFamily": "Lexend",
                "fontSize": 10,
                "bold": True,
                "foregroundColor": {"red": 0.0, "green": 0.0, "blue": 0.0}
            },
            "horizontalAlignment": "RIGHT",
            "verticalAlignment": "MIDDLE"
        })
        
        # 4. Format Values in Col C (Center aligned, bold)
        ws.format(f"C{start_row+1}:C{start_row+10}", {
            "textFormat": {
                "fontFamily": "Lexend",
                "fontSize": 10,
                "bold": True,
                "foregroundColor": {"red": 0.0, "green": 0.0, "blue": 0.0}
            },
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE"
        })
        
        # 5. Set Column Widths for B and C
        requests = [
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2}, # Col B
                    "properties": {"pixelSize": 260},
                    "fields": "pixelSize"
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3}, # Col C
                    "properties": {"pixelSize": 180},
                    "fields": "pixelSize"
                }
            }
        ]
        ws.spreadsheet.batch_update({"requests": requests})
    except Exception as e:
        print(f"Card style error: {e}") 

# ----------------------------------------------------------------------
# Google Sheets helpers
# ----------------------------------------------------------------------
def get_sheets_client():
    if not CREDENTIALS_FILE.exists():
        raise RuntimeError("credentials.json not found")
    return sheets_auth.authorize(str(CREDENTIALS_FILE))


def get_sheet(worksheet_name: Optional[str] = None):
    sheet_id = get_spreadsheet_id()
    if not sheet_id:
        raise RuntimeError("No spreadsheet ID configured")
    client = get_sheets_client()
    spreadsheet = sheets_auth.get_spreadsheet(client, sheet_id)
    spreadsheet.fetch_sheet_metadata()

    if worksheet_name is not None:
        try:
            return spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            raise RuntimeError(f"Worksheet '{worksheet_name}' not found")
    return spreadsheet.get_worksheet(0)


def create_new_worksheet(title: str) -> str:
    sheet_id = get_spreadsheet_id()
    if not sheet_id:
        raise RuntimeError("No spreadsheet ID configured")
    client = get_sheets_client()
    spreadsheet = sheets_auth.get_spreadsheet(client, sheet_id)
    spreadsheet.fetch_sheet_metadata()
    try:
        spreadsheet.worksheet(title)
        raise RuntimeError(f"Worksheet '{title}' already exists")
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=100, cols=20)
        return worksheet.title


def append_row_to_worksheet(worksheet, row_data: List[Any]) -> int:
    existing_rows = len(worksheet.get_all_values())
    worksheet.append_rows([row_data], value_input_option="USER_ENTERED")
    return existing_rows + 1


def update_row_cells(worksheet, row_index: int, col_value_map: Dict[int, Any]) -> None:
    for col, value in col_value_map.items():
        if value is not None:
            worksheet.update_cell(row_index, col, value)


# ----------------------------------------------------------------------
# Today’s sheet helpers
# ----------------------------------------------------------------------
def _today_date_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _today_sheet_name() -> str:
    return datetime.datetime.now().strftime("%d %b %y")


def _headers() -> list[str]:
    """Columns for the daily sheet. 'ID' is kept for potential internal use."""
    return [
        "ID",                   # optional internal UUID
        "Date",
        "Card Name",
        "Card Path",
        "Insert Time",
        "Finish Time",
        "Total MP4 Videos",
        "Original Duration",
        "Final Duration",
        "Duration Difference",
        "Card Capacity",
        "Used Space",
        "Status",
        "Used Space Before Labeling (GB)",
        "Used Space After Labeling (GB)",
        "Original Duration Before Labeling",
        "Original Duration After Labeling",
    ]


def _ensure_today_sheet():
    """Return the worksheet for today’s date. Create it (with headers) if missing."""
    sheet_name = _today_sheet_name()
    try:
        ws = get_sheet(sheet_name)
    except RuntimeError:
        try:
            create_new_worksheet(sheet_name)
            ws = get_sheet(sheet_name)
            
            # FIX: Use append_rows (plural) and wrap _headers() in a list to match gspread v6+
            ws.append_rows([_headers()], value_input_option=ValueInputOption.user_entered)
            
            try:
                style_worksheet(ws)
            except Exception as e:
                print(f"Style error: {e}")
        except RuntimeError as e:
            # Caught if create_new_worksheet fetches metadata and sees Thread A just made it
            if "already exists" in str(e).lower():
                ws = get_sheet(sheet_name)
            else:
                raise
        except gspread.exceptions.APIError as e:
            # Caught if add_worksheet fails because Google Sheets says it already exists
            if "already exists" in str(e).lower():
                ws = get_sheet(sheet_name)
            else:
                raise
    return ws


def _parse_sheet_to_cards(ws) -> list[dict]:
    """Read the sheet and return a list of card dicts with 'cardName' and 'sheetRowIndex'."""
    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return []

    raw_headers = all_values[0]
    col_map = {}
    for idx, h in enumerate(raw_headers):
        clean = h.strip().lower().replace(" ", "_").replace("(", "").replace(")", "")
        col_map[clean] = idx

    cards = []
    for row_idx, row in enumerate(all_values[1:], start=2):
        # Card Name is the unique identifier – required
        name_col = col_map.get("card_name")
        if name_col is None or name_col >= len(row):
            continue
        card_name = row[name_col].strip()
        if not card_name:
            continue

        card = {"cardName": card_name, "sheetRowIndex": row_idx}
        # Populate all columns for convenience
        for clean_name, col_idx in col_map.items():
            if clean_name == "card_name":
                continue
            value = row[col_idx] if col_idx < len(row) else ""
            card[clean_name] = value.strip() if isinstance(value, str) else value
        cards.append(card)
    return cards


def _find_card_by_name(ws, card_name: str) -> Optional[dict]:
    """Find the first row with the given card name. Returns a dict with 'row_index' and column values."""
    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return None

    headers = all_values[0]
    name_col = None
    for idx, h in enumerate(headers):
        if h.strip().lower() == "card name":
            name_col = idx
            break
    if name_col is None:
        return None

    for row_idx, row in enumerate(all_values[1:], start=2):
        if name_col < len(row) and row[name_col].strip().lower() == card_name.lower():
            # Build a dict of column values keyed by header (cleaned)
            col_values = {}
            for col_idx, h in enumerate(headers):
                clean = h.strip().lower().replace(" ", "_").replace("(", "").replace(")", "")
                col_values[clean] = row[col_idx] if col_idx < len(row) else ""
            col_values["row_index"] = row_idx
            return col_values
    return None


# ----------------------------------------------------------------------
# Sheet styling (unchanged)
# ----------------------------------------------------------------------
def style_worksheet(ws: gspread.Worksheet) -> None:
    try:
        headers = _headers()
        ws.freeze(rows=1)
        ws.format(
            "1:1",
            {
                "backgroundColor": {"red": 0.4, "green": 0.4, "blue": 0.4},
                "textFormat": {
                    "fontFamily": "Lexend",
                    "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                    "fontSize": 9,
                },
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
            },
        )
        ws.format(
            "A2:Z500",
            {
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "textFormat": {
                    "fontSize": 9,
                    "fontFamily": "Lexend",
                    "foregroundColor": {"red": 0.45, "green": 0.45, "blue": 0.45},
                },
            },
        )

        custom_widths = {
            "ID": 90,
            "Date": 110,
            "Card Name": 110,
            "Card Path": 160,
            "Insert Time": 110,
            "Finish Time": 110,
            "Total MP4 Videos": 110,
            "Original Duration": 140,
            "Final Duration": 140,
            "Duration Difference": 150,
            "Card Capacity": 130,
            "Used Space": 120,
            "Status": 120,
            "Used Space Before Labeling (GB)": 210,
            "Used Space After Labeling (GB)": 210,
            "Original Duration Before Labeling": 210,
            "Original Duration After Labeling": 210,
        }

        requests = []
        for col_idx, header_name in enumerate(headers):
            pixel_size = custom_widths.get(header_name, 140)
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": col_idx,
                        "endIndex": col_idx + 1,
                    },
                    "properties": {"pixelSize": pixel_size},
                    "fields": "pixelSize",
                }
            })
        ws.spreadsheet.batch_update({"requests": requests})
    except Exception as e:
        print(f"Style error: {e}")


# ----------------------------------------------------------------------
# Blueprint
# ----------------------------------------------------------------------
def create_sheets_blueprint() -> Blueprint:
    bp = Blueprint("sheets", __name__, url_prefix="/api/sheets")

    # --------------------------------------------------------------
    # 1. Setup: upload credentials & set spreadsheet ID
    # --------------------------------------------------------------
    @bp.route("/setup", methods=["POST"])
    def setup_sheets():
        cred_file = request.files.get("credentials")
        if cred_file:
            cred_file.save(str(CREDENTIALS_FILE))
        elif not CREDENTIALS_FILE.exists():
            return jsonify({"error": "No credentials file provided"}), 400

        sheet_id = request.form.get("spreadsheetId", "").strip()
        if sheet_id:
            set_spreadsheet_id(sheet_id)
        else:
            try:
                client = get_sheets_client()
                title = f"Eager Review {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
                spreadsheet = client.create(title)
                new_id = spreadsheet.id
                set_spreadsheet_id(new_id)
                sheet_id = new_id
            except Exception as e:
                return jsonify({"error": f"Failed to create spreadsheet: {str(e)}"}), 500

        return jsonify({
            "ok": True,
            "spreadsheetId": sheet_id,
            "credentialsExists": CREDENTIALS_FILE.exists(),
        })

    # --------------------------------------------------------------
    # 2. Status check
    # --------------------------------------------------------------
    @bp.route("/status", methods=["GET"])
    def sheets_status():
        db = read_db()
        sheet_id = db.get("spreadsheetId")
        return jsonify({
            "credentialsExists": CREDENTIALS_FILE.exists(),
            "spreadsheetIdExists": bool(sheet_id),
            "spreadsheetId": sheet_id,
        })

    # --------------------------------------------------------------
    # 3. Get current day’s process (all cards from sheet)
    # --------------------------------------------------------------
    @bp.route("/process/current", methods=["GET"])
    def get_current():
        try:
            ws = _ensure_today_sheet()
            cards = _parse_sheet_to_cards(ws)
            return jsonify({
                "date": _today_date_str(),
                "sheetName": _today_sheet_name(),
                "cards": cards,
            })
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 500

    # --------------------------------------------------------------
    # 4. Add a card – checks for duplicate by card name
    # --------------------------------------------------------------
    @bp.route("/process/card", methods=["POST"])
    def add_card():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON payload required"}), 400
        card_path = data.get("cardPath", "").strip()
        card_name = data.get("cardName", "").strip()
        if not card_path or not card_name:
            return jsonify({"error": "cardPath and cardName are required"}), 400

        # Ensure today's sheet exists
        try:
            ws = _ensure_today_sheet()
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 500

        # Check if a card with this name already exists
        existing = _find_card_by_name(ws, card_name)
        if existing:
            # Return the existing card info – no duplicate row
            return jsonify({
                "ok": True,
                "already_exists": True,
                "card": {
                    "card_name": card_name,
                    "card_path": existing.get("card_path", ""),
                    "status": existing.get("status", ""),
                    "sheetRowIndex": existing["row_index"],
                    # include any other useful fields from existing row
                },
                "message": f"Card '{card_name}' already exists in today's sheet"
            }), 200

        # Not a duplicate – compute stats and append a new row
        stats = card_stats(card_path)

        # We still generate an internal UUID (not used for lookups)
        row_id = f"row-{uuid.uuid4().hex[:8]}"

        headers = _headers()
        header_to_value = {
            "ID": row_id,
            "Date": _today_date_str(),
            "Card Name": card_name,
            "Card Path": str(Path(card_path).expanduser().resolve()),
            "Insert Time": _now_12h_time(),
            "Finish Time": "",
            "Total MP4 Videos": stats["total_mp4_videos"],
            "Original Duration": _format_duration(stats["original_duration"]),
            "Final Duration": "",
            "Duration Difference": "",
            "Card Capacity": _format_gb(stats["card_capacity"]) if stats["card_capacity"] else "",
            "Used Space": _format_gb(stats["used_space_before_labeling_gb"]),
            "Status": "Pending",
            "Used Space Before Labeling (GB)": _format_gb(stats["used_space_before_labeling_gb"]),
            "Used Space After Labeling (GB)": "",
            "Original Duration Before Labeling": _format_duration(stats["original_duration"]),
            "Original Duration After Labeling": "",
        }

        row_values = [str(header_to_value.get(h, "")) for h in headers]
        try:
            new_row_index = append_row_to_worksheet(ws, row_values)
        except Exception as e:
            return jsonify({"error": f"Failed to append to sheet: {str(e)}"}), 500

        card_data = {
            "card_name": card_name,
            "card_path": header_to_value["Card Path"],
            "insert_time": header_to_value["Insert Time"],
            "status": "Pending",
            "original_duration": stats["original_duration"],
            "total_mp4_videos": stats["total_mp4_videos"],
            "card_capacity": stats["card_capacity"],
            "used_space_before_labeling_gb": stats["used_space_before_labeling_gb"],
            "sheetRowIndex": new_row_index,
        }
        return jsonify({"ok": True, "already_exists": False, "card": card_data, "sheetRowIndex": new_row_index})

    # --------------------------------------------------------------
    # 5. Finish a card – finds card by name (not by ID)
    # --------------------------------------------------------------
    @bp.route("/process/card/finish", methods=["POST"])
    def finish_card():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON payload required"}), 400
        # Expect cardName as the unique identifier
        card_name = data.get("cardName", "").strip()
        if not card_name:
            return jsonify({"error": "cardName is required"}), 400

        try:
            ws = _ensure_today_sheet()
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 500

        # Find the row by card name
        found = _find_card_by_name(ws, card_name)
        if not found:
            return jsonify({"error": f"Card '{card_name}' not found in today's sheet"}), 404

        row_index = found["row_index"]
        card_path_str = found.get("card_path", "").strip()
        if not card_path_str:
            return jsonify({"error": "Card path is empty in sheet"}), 400

        # Re-scan the card for final stats
        stats = card_stats(card_path_str)

        # Determine column indexes for updating
        headers = ws.row_values(1)
        header_to_col = {}
        for idx, h in enumerate(headers):
            clean = h.strip().lower().replace(" ", "_").replace("(", "").replace(")", "")
            header_to_col[clean] = idx + 1   # 1-based

        finish_time = _now_12h_time()
        final_duration = stats["final_duration"]
        # Use the original duration from stats (same as before labeling)
        original_duration = stats["original_duration"]
        duration_diff = round(original_duration - final_duration, 3)

        updates = {}
        if "finish_time" in header_to_col:
            updates[header_to_col["finish_time"]] = finish_time
        if "final_duration" in header_to_col:
            updates[header_to_col["final_duration"]] = _format_duration(final_duration)
        if "duration_difference" in header_to_col:
            updates[header_to_col["duration_difference"]] = _format_duration(duration_diff)
        if "status" in header_to_col:
            updates[header_to_col["status"]] = "Completed"
        if "card_capacity" in header_to_col:
            updates[header_to_col["card_capacity"]] = _format_gb(stats["card_capacity"]) if stats["card_capacity"] else ""
        if "used_space_after_labeling_gb" in header_to_col:
            updates[header_to_col["used_space_after_labeling_gb"]] = _format_gb(stats["used_space_after_labeling_gb"])
        if "original_duration_after_labeling" in header_to_col:
            updates[header_to_col["original_duration_after_labeling"]] = _format_duration(final_duration)

        try:
            update_row_cells(ws, row_index, updates)
        except Exception as e:
            return jsonify({"error": f"Failed to update sheet: {str(e)}"}), 500

        updated_card = {
            "card_name": card_name,
            "sheetRowIndex": row_index,
            "finish_time": finish_time,
            "final_duration": final_duration,
            "duration_difference": duration_diff,
            "status": "Completed",
        }
        return jsonify({"ok": True, "card": updated_card})

    # --------------------------------------------------------------
    # 6. Test connection
    # --------------------------------------------------------------
    @bp.route("/test", methods=["GET"])
    def test_connection():
        if not CREDENTIALS_FILE.exists():
            return jsonify({"ok": False, "error": "credentials.json not found"}), 400
        sheet_id = get_spreadsheet_id()
        if not sheet_id:
            return jsonify({"ok": False, "error": "No spreadsheet ID configured"}), 400
        try:
            client = get_sheets_client()
            spreadsheet = sheets_auth.get_spreadsheet(client, sheet_id)
            spreadsheet.get_worksheet(0)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

# --------------------------------------------------------------
    # 7. Generate Daily Summary (Vertical Card Style)
    # --------------------------------------------------------------
    @bp.route("/process/summary", methods=["POST"])
    def generate_daily_summary():
        try:
            ws_today = _ensure_today_sheet()
            cards = _parse_sheet_to_cards(ws_today)
        except RuntimeError as e:
            return jsonify({"error": f"Failed to read today's sheet: {str(e)}"}), 500

        if not cards:
            return jsonify({"error": "No cards found in today's sheet to summarize."}), 400

        # Calculate Totals
        total_cards = len(cards)
        space_before_gb = 0.0
        space_after_gb = 0.0
        duration_before_sec = 0.0
        duration_after_sec = 0.0

        for card in cards:
            space_before_gb += _parse_gb_string(card.get("used_space_before_labeling_gb"))
            space_after_gb += _parse_gb_string(card.get("used_space_after_labeling_gb"))
            duration_before_sec += _parse_duration_to_seconds(card.get("original_duration_before_labeling"))
            duration_after_sec += _parse_duration_to_seconds(card.get("original_duration_after_labeling"))

        today_date_formatted = datetime.datetime.now().strftime("%d-%b-%Y")
        
        # Build the 11-row vertical card matrix
        card_matrix = [
            ["Daily summary", ""], # Row 1 (Header, will be merged)
            ["Date", today_date_formatted],
            ["Total cards received", str(total_cards)], # Screenshot mein "recvied" tha, I fixed the spelling to "received"
            ["Total Used Space (GB) Before Labeling", f"{space_before_gb:,.2f} GB"],
            ["Total Used Space (GB) After Labeling", f"{space_after_gb:,.2f} GB"],
            ["Total storage before (TB)", f"{(space_before_gb / 1024):.2f} TB"],
            ["Total storage after (TB)", f"{(space_after_gb / 1024):.2f} TB"],
            ["Total Original Duration Before Labeling", _format_duration(duration_before_sec)],
            ["Total Original Duration After Labeling", _format_duration(duration_after_sec)],
            ["Total Hours Before", _format_hours_mins(duration_before_sec)],
            ["Total Hours After", _format_hours_mins(duration_after_sec)]
        ]

        try:
            sheet_name = "Summary"
            try:
                ws_summary = get_sheet(sheet_name)
            except RuntimeError:
                create_new_worksheet(sheet_name)
                ws_summary = get_sheet(sheet_name)
                
            all_values = ws_summary.get_all_values()
            
            # Find if today's card already exists (checking Col B for "Date" and Col C for actual date)
            start_row = None
            for r_idx, row in enumerate(all_values):
                # row is a list where index 1 is Col B, index 2 is Col C
                if len(row) >= 3 and row[1].strip().lower() == "date" and row[2].strip() == today_date_formatted:
                    start_row = r_idx # 1-based index pointing to the "Daily summary" header row just above the date
                    break
            
            if start_row is not None:
                actual_start_row = start_row
                action = "updated"
            else:
                # Append new card at the bottom with a 2-row gap
                actual_start_row = len(all_values) + 2 if len(all_values) > 0 else 2
                action = "appended"

            # 1. Update the block range (B to C)
            range_str = f"B{actual_start_row}:C{actual_start_row + 10}"
            ws_summary.update(values=card_matrix, range_name=range_str)
            
            # 2. Apply styling to this specific card
            _style_vertical_summary_card(ws_summary, actual_start_row)

            return jsonify({
                "ok": True,
                "action": action,
                "start_row": actual_start_row
            })

        except Exception as e:
            return jsonify({"error": f"Failed to update Summary sheet: {str(e)}"}), 500

    return bp