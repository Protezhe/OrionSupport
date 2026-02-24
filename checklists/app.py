from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional

import requests
from flask import Flask, jsonify, render_template

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_CSV = os.path.join(APP_DIR, "checklists.csv")
LIBRARY_PATH = os.path.join(APP_DIR, "library.json")

SHEET_ID = os.environ.get("CHECKLISTS_SHEET_ID", "1vCV13wXPuCZ-i8XLFrTXu_cZ_LoE61WwS7qHbKb2vg8")
SHEET_GID = os.environ.get("CHECKLISTS_SHEET_GID", "0")
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
CSV_GVIZ_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&gid={SHEET_GID}"

CACHE_TTL_SECONDS = int(os.environ.get("CHECKLISTS_CACHE_TTL", "0"))
DEFAULT_SHEET_TITLE = os.environ.get("CHECKLISTS_DEFAULT_TITLE", "Театр сказок")
SKIP_NAME_SUBSTRINGS = ["проведение работ"]

app = Flask(__name__, template_folder=os.path.join(APP_DIR, "templates"))


@dataclass
class CacheEntry:
    timestamp: float
    data: dict


_cache: Optional[CacheEntry] = None


def _load_library() -> dict:
    if not os.path.exists(LIBRARY_PATH):
        return {"sheets": [], "items": []}
    with open(LIBRARY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize(text: str) -> str:
    if text is None:
        return ""
    lowered = str(text).strip().lower()
    for ch in [" ", "\t", "\n", "\r", "-", "_", "/", "\\", ".", ",", ":", ";", "(", ")"]:
        lowered = lowered.replace(ch, "")
    return lowered


def _find_col(headers: List[str], keywords: List[str]) -> Optional[int]:
    normalized = [_normalize(h) for h in headers]
    normalized_keywords = [_normalize(k) for k in keywords]
    for idx, header in enumerate(normalized):
        if not header:
            continue
        for key in normalized_keywords:
            if key and key in header:
                return idx
    return None


def _first_non_empty(row: List[str], indices: List[int]) -> str:
    for idx in indices:
        if idx is None or idx >= len(row):
            continue
        value = str(row[idx]).strip()
        if value:
            return value
    return ""


def _is_skipped(name: str) -> bool:
    lowered = _normalize(name)
    return any(_normalize(s) in lowered for s in SKIP_NAME_SUBSTRINGS)


def _read_csv_file(path: str) -> List[List[str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        return list(reader)


def _download_csv(url: str, dest: str) -> bool:
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, timeout=20, headers=headers)
    response.raise_for_status()
    with open(dest, "wb") as f:
        f.write(response.content)
    return True


def _load_csv_rows() -> List[List[str]]:
    last_error: Optional[Exception] = None
    for url in (CSV_URL, CSV_GVIZ_URL):
        try:
            _download_csv(url, LOCAL_CSV)
            rows = _read_csv_file(LOCAL_CSV)
            if rows:
                return rows
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue

    if os.path.exists(LOCAL_CSV):
        try:
            return _read_csv_file(LOCAL_CSV)
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    if last_error:
        raise last_error
    return []


def _parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _build_schedule(rows: List[List[str]]) -> List[dict]:
    if not rows:
        return []

    headers = rows[0]
    body = rows[1:]

    date_col = _find_col(headers, ["дата"])
    engineer_col = _find_col(headers, ["инженер", "исполн", "сотрудник", "фио", "ответствен"])
    defects_col = _find_col(headers, ["выявлен", "недостат", "дефект", "замечан"])
    checklist_cols = [idx for idx, h in enumerate(headers) if "чеклист" in _normalize(h)]

    entries: Dict[str, dict] = {}
    for row in body:
        if not any(str(cell).strip() for cell in row):
            continue

        raw_date = _first_non_empty(row, [date_col] if date_col is not None else [])
        parsed = _parse_date(raw_date)
        if not parsed:
            continue

        checklist_name = _first_non_empty(row, checklist_cols)
        if not checklist_name:
            checklist_name = DEFAULT_SHEET_TITLE

        if _is_skipped(checklist_name):
            continue

        engineer = _first_non_empty(row, [engineer_col] if engineer_col is not None else [])
        defects = _first_non_empty(row, [defects_col] if defects_col is not None else [])

        iso = parsed.isoformat()
        if iso in entries:
            continue
        entries[iso] = {
            "date": iso,
            "display_date": parsed.strftime("%d.%m.%Y"),
            "checklist": checklist_name,
            "engineer": engineer,
            "defects": defects,
        }

    return [entries[k] for k in sorted(entries.keys(), reverse=True)]


def _get_schedule(force_refresh: bool = False) -> List[dict]:
    global _cache
    if _cache and not force_refresh:
        if CACHE_TTL_SECONDS <= 0:
            return _cache.data
        now = time.time()
        if (now - _cache.timestamp) < CACHE_TTL_SECONDS:
            return _cache.data
    elif _cache and CACHE_TTL_SECONDS <= 0 and not force_refresh:
        return _cache.data

    rows = _load_csv_rows()
    data = _build_schedule(rows)
    _cache = CacheEntry(timestamp=time.time(), data=data)
    return data


def _get_library_lookup() -> Dict[str, dict]:
    library = _load_library()
    sheets = {s.get("id"): s for s in library.get("sheets", [])}
    items = library.get("items", [])
    return {"sheets": sheets, "items": items}


@app.route("/")
def index():
    return render_template("checklists_ui.html")


@app.route("/api/schedule")
def api_schedule():
    data = _get_schedule()
    return jsonify({"entries": data})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    data = _get_schedule(force_refresh=True)
    return jsonify({"entries": data, "refreshed": True})


@app.route("/api/checklist")
def api_checklist():
    from flask import request

    date_param = request.args.get("date", "").strip()
    schedule = _get_schedule()
    entry = next((e for e in schedule if e["date"] == date_param), None)
    if not entry:
        return jsonify({"error": "not_found"}), 404

    library = _get_library_lookup()
    sheet = library["sheets"].get(entry["checklist"])
    if not sheet:
        return jsonify({"error": "checklist_not_found", "checklist": entry["checklist"]}), 404

    items = [i for i in library["items"] if i.get("sheet_id") == sheet.get("id")]
    payload = {
        "date": entry["date"],
        "display_date": entry["display_date"],
        "engineer": entry.get("engineer", ""),
        "defects": entry.get("defects", ""),
        "sheet": sheet,
        "items": items,
    }
    return jsonify(payload)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5005"))
    app.run(host="0.0.0.0", port=port, debug=True)
