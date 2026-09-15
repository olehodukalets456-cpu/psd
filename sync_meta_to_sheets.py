import json
import os
import sys
from datetime import date, timedelta
from typing import Any

import google.auth
import requests
from googleapiclient.discovery import build


META_API_VERSION = os.getenv("META_API_VERSION", "v21.0")
META_AD_ACCOUNT_ID = os.getenv("META_AD_ACCOUNT_ID", "act_369275069245313")
GOOGLE_SHEET_ID = os.getenv(
    "GOOGLE_SHEET_ID", "1yJQsmaNUpyaRxr_jR9pSwotlh0HjOGzfZYPOR9QEEc0"
)
RAW_SHEET_NAME = os.getenv("RAW_SHEET_NAME", "raw_meta")
START_DATE = os.getenv("START_DATE", "2026-01-01")
META_PAGE_LIMIT = int(os.getenv("META_PAGE_LIMIT", "250"))
META_CHUNK_DAYS = int(os.getenv("META_CHUNK_DAYS", "14"))
WRITE_CHUNK_SIZE = int(os.getenv("WRITE_CHUNK_SIZE", "5000"))

META_FIELDS = ",".join(
    [
        "campaign_name",
        "spend",
        "impressions",
        "reach",
        "clicks",
        "inline_link_clicks",
        "actions",
        "action_values",
    ]
)

SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
GOOGLE_SHEETS_EPOCH = date(1899, 12, 30)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required GitHub Actions secret/environment variable: {name}")
    return value


def to_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def action_value(items: Any, action_type: str) -> float:
    if not isinstance(items, list):
        return 0.0
    for item in items:
        if isinstance(item, dict) and item.get("action_type") == action_type:
            return to_float(item.get("value"))
    return 0.0


def meta_error_details(payload: Any, status_code: int) -> tuple[str, int | None, int | None]:
    message = f"Meta API HTTP {status_code}"
    code = None
    subcode = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message", message))
            code = error.get("code")
            subcode = error.get("error_subcode")
    return message, code, subcode


def meta_error_message(payload: Any, status_code: int) -> str:
    message, code, subcode = meta_error_details(payload, status_code)
    details = f"Meta API HTTP {status_code}: {message}"
    if code is not None:
        details += f" (code {code}"
        if subcode is not None:
            details += f", subcode {subcode}"
        details += ")"
    return details


def request_meta_range(
    session: requests.Session,
    access_token: str,
    since: date,
    until: date,
) -> list[dict[str, Any]]:
    url = f"https://graph.facebook.com/{META_API_VERSION}/{META_AD_ACCOUNT_ID}/insights"
    params = {
        "access_token": access_token,
        "level": "campaign",
        "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
        "time_increment": "1",
        "fields": META_FIELDS,
        "action_attribution_windows": json.dumps(["7d_click", "1d_view"]),
        "limit": str(META_PAGE_LIMIT),
    }

    rows: list[dict[str, Any]] = []
    next_url: str | None = url
    first_request = True
    seen_next_urls: set[str] = set()
    page = 0

    while next_url:
        page += 1
        if page > 10000:
            raise RuntimeError("Meta pagination exceeded the safety limit of 10,000 pages.")

        try:
            if first_request:
                response = session.get(next_url, params=params, timeout=90)
                first_request = False
            else:
                response = session.get(next_url, timeout=90)
        except requests.RequestException:
            raise RuntimeError(
                f"Meta API network error for {since.isoformat()} -> {until.isoformat()}."
            ) from None

        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(
                f"Meta API returned non-JSON (HTTP {response.status_code}) for "
                f"{since.isoformat()} -> {until.isoformat()}."
            ) from None

        if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("error")):
            message, code, _ = meta_error_details(payload, response.status_code)

            # Meta commonly returns HTTP 500 / code 1 when the Insights request is too large.
            # Split the date window recursively until the request becomes small enough.
            if (response.status_code >= 500 or code in {1, 2, 4, 17, 32, 613}) and since < until:
                days = (until - since).days
                midpoint = since + timedelta(days=days // 2)
                left_until = midpoint
                right_since = midpoint + timedelta(days=1)
                print(
                    f"Meta rejected range {since.isoformat()} -> {until.isoformat()} "
                    f"({message}). Splitting into smaller ranges..."
                )
                left_rows = request_meta_range(session, access_token, since, left_until)
                right_rows = request_meta_range(session, access_token, right_since, until)
                return left_rows + right_rows

            raise RuntimeError(meta_error_message(payload, response.status_code))

        page_rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(page_rows, list):
            raise RuntimeError("Meta API response did not contain a valid data array.")
        rows.extend(page_rows)

        paging = payload.get("paging", {}) if isinstance(payload, dict) else {}
        candidate = paging.get("next") if isinstance(paging, dict) else None
        if candidate:
            if candidate in seen_next_urls:
                raise RuntimeError("Meta API pagination loop detected; refusing to overwrite the sheet.")
            seen_next_urls.add(candidate)
        next_url = candidate

    return rows


def fetch_meta_insights(access_token: str) -> list[dict[str, Any]]:
    try:
        start = date.fromisoformat(START_DATE)
    except ValueError:
        raise RuntimeError(f"Invalid START_DATE: {START_DATE}") from None

    end = date.today()
    if start > end:
        raise RuntimeError(f"START_DATE {START_DATE} is after today {end.isoformat()}.")

    rows: list[dict[str, Any]] = []
    session = requests.Session()
    chunk_start = start
    chunk_number = 0

    while chunk_start <= end:
        chunk_number += 1
        chunk_end = min(chunk_start + timedelta(days=META_CHUNK_DAYS - 1), end)
        print(
            f"Meta chunk {chunk_number}: {chunk_start.isoformat()} -> {chunk_end.isoformat()}"
        )
        chunk_rows = request_meta_range(session, access_token, chunk_start, chunk_end)
        print(f"Meta chunk {chunk_number}: {len(chunk_rows)} rows")
        rows.extend(chunk_rows)
        chunk_start = chunk_end + timedelta(days=1)

    return rows


def google_date_serial(iso_date: str) -> int:
    try:
        parsed = date.fromisoformat(iso_date)
    except ValueError:
        raise RuntimeError(f"Meta returned an invalid date_start: {iso_date}") from None
    return (parsed - GOOGLE_SHEETS_EPOCH).days


def transform_rows(meta_rows: list[dict[str, Any]]) -> list[list[Any]]:
    output: list[list[Any]] = []

    for item in meta_rows:
        spend = to_float(item.get("spend"))
        if spend <= 0:
            continue

        impressions = to_float(item.get("impressions"))
        reach = to_float(item.get("reach"))
        all_clicks = to_float(item.get("clicks"))
        site_clicks = to_float(item.get("inline_link_clicks"))
        ig_clicks = max(0.0, all_clicks - site_clicks)

        purchases = action_value(item.get("actions"), "onsite_web_purchase")
        purchase_value = action_value(item.get("action_values"), "onsite_web_purchase")

        campaign_name = str(item.get("campaign_name") or "")
        upper_name = campaign_name.upper()
        if "PSD" in upper_name:
            campaign_type = "PSD"
        elif "TRAFF" in upper_name:
            campaign_type = "TRAFF"
        else:
            campaign_type = "other"

        date_start = str(item.get("date_start") or "")
        if not date_start:
            raise RuntimeError("Meta returned a row without date_start; refusing to overwrite the sheet.")

        output.append(
            [
                google_date_serial(date_start),
                campaign_name,
                spend,
                impressions,
                reach,
                all_clicks,
                site_clicks,
                ig_clicks,
                purchases,
                purchase_value,
                campaign_type,
            ]
        )

    output.sort(key=lambda row: (row[0], row[1]))
    return output


def build_sheets_service():
    try:
        credentials, _ = google.auth.default(scopes=SHEETS_SCOPE)
    except Exception as exc:
        raise RuntimeError(f"Could not load Google ADC credentials: {exc}") from None

    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def get_raw_sheet_id(service) -> int:
    metadata = service.spreadsheets().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        fields="sheets(properties(sheetId,title))",
    ).execute()
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == RAW_SHEET_NAME:
            return int(properties["sheetId"])
    raise RuntimeError(f"Google Sheet tab '{RAW_SHEET_NAME}' was not found.")


def apply_date_format(service, raw_sheet_id: int, row_count: int) -> None:
    service.spreadsheets().batchUpdate(
        spreadsheetId=GOOGLE_SHEET_ID,
        body={
            "requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": raw_sheet_id,
                            "startRowIndex": 2,
                            "endRowIndex": 2 + row_count,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {
                                    "type": "DATE",
                                    "pattern": "yyyy-mm-dd",
                                }
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            ]
        },
    ).execute()


def replace_raw_meta(service, rows: list[list[Any]]) -> None:
    if not rows:
        raise RuntimeError(
            "Meta returned zero rows with spend > 0. The existing sheet was NOT cleared."
        )

    raw_sheet_id = get_raw_sheet_id(service)
    values_api = service.spreadsheets().values()

    # Clear only after ALL Meta chunks have been fetched and transformed successfully.
    values_api.clear(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"'{RAW_SHEET_NAME}'!A3:K",
        body={},
    ).execute()

    for offset in range(0, len(rows), WRITE_CHUNK_SIZE):
        chunk = rows[offset : offset + WRITE_CHUNK_SIZE]
        start_row = 3 + offset
        values_api.update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{RAW_SHEET_NAME}'!A{start_row}",
            valueInputOption="RAW",
            body={"majorDimension": "ROWS", "values": chunk},
        ).execute()

    # Column A must contain real spreadsheet dates, not text. This keeps all
    # existing SUMIFS date filters and dashboard formulas working.
    apply_date_format(service, raw_sheet_id, len(rows))


def main() -> int:
    meta_access_token = required_env("META_ACCESS_TOKEN")

    print(
        f"Fetching Meta Insights in {META_CHUNK_DAYS}-day chunks: {META_AD_ACCOUNT_ID}, "
        f"{START_DATE} -> {date.today().isoformat()}, API {META_API_VERSION}"
    )

    meta_rows = fetch_meta_insights(meta_access_token)
    transformed = transform_rows(meta_rows)

    if not meta_rows:
        raise RuntimeError("Meta returned no insight rows. Existing Google Sheet was NOT changed.")

    print(
        f"Meta fetch complete: {len(meta_rows)} raw campaign-day rows, "
        f"{len(transformed)} rows after spend > 0 filter."
    )

    service = build_sheets_service()
    replace_raw_meta(service, transformed)

    print(
        f"Google Sheet updated successfully: {GOOGLE_SHEET_ID} / {RAW_SHEET_NAME}, "
        f"{len(transformed)} rows written; column A stored as real dates."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
