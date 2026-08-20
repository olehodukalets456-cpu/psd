import json
import os
import sys
from datetime import date
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
META_PAGE_LIMIT = int(os.getenv("META_PAGE_LIMIT", "500"))
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
        "website_purchase_roas",
    ]
)

SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]


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


def meta_error_message(payload: Any, status_code: int) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message", "Unknown Meta API error")
            code = error.get("code")
            subcode = error.get("error_subcode")
            details = f"Meta API HTTP {status_code}: {message}"
            if code is not None:
                details += f" (code {code}"
                if subcode is not None:
                    details += f", subcode {subcode}"
                details += ")"
            return details
    return f"Meta API HTTP {status_code}"


def fetch_meta_insights(access_token: str) -> list[dict[str, Any]]:
    url = f"https://graph.facebook.com/{META_API_VERSION}/{META_AD_ACCOUNT_ID}/insights"
    params = {
        "access_token": access_token,
        "level": "campaign",
        "time_range": json.dumps({"since": START_DATE, "until": date.today().isoformat()}),
        "time_increment": "1",
        "fields": META_FIELDS,
        "action_attribution_windows": json.dumps(["7d_click", "1d_view"]),
        "limit": str(META_PAGE_LIMIT),
    }

    rows: list[dict[str, Any]] = []
    session = requests.Session()
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
                response = session.get(next_url, params=params, timeout=60)
                first_request = False
            else:
                response = session.get(next_url, timeout=60)
        except requests.RequestException:
            raise RuntimeError("Meta API request failed due to a network error.") from None

        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(
                f"Meta API returned a non-JSON response (HTTP {response.status_code})."
            ) from None

        if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("error")):
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

        date_start = item.get("date_start")
        if not date_start:
            raise RuntimeError("Meta returned a row without date_start; refusing to overwrite the sheet.")

        output.append(
            [
                str(date_start),
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


def replace_raw_meta(service, rows: list[list[Any]]) -> None:
    if not rows:
        raise RuntimeError(
            "Meta returned zero rows with spend > 0. The existing sheet was NOT cleared."
        )

    values_api = service.spreadsheets().values()

    # Clear only after Meta data has been fully fetched and transformed.
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


def main() -> int:
    meta_access_token = required_env("META_ACCESS_TOKEN")

    print(
        f"Fetching Meta Insights: {META_AD_ACCOUNT_ID}, "
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
        f"{len(transformed)} rows written with RAW value input."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
