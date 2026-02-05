#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simple fuzzy search over CSV / Google Sheets to find a solution for a given problem text.

Usage:
  python3 search_solution.py "текст проблемы"
  python3 search_solution.py --top 3 "текст проблемы"
  python3 search_solution.py                          # interactive mode
"""

import argparse
import csv
import difflib
import json
import re
import sys
import threading
import urllib.request
from pathlib import Path


DEFAULT_SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "13Kdj9pCMfha3UwzYi_pWsLt6V5yayrzpTsqqOvM0Tzg/export?format=csv"
)
LOCAL_CSV = Path(__file__).parent / "ТехПроблемы Орион.csv"
CONFIG_PATH = Path(__file__).parent / "config.json"
DEFAULT_REFRESH_INTERVAL = 1800  # 30 minutes


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\sа-яёa-z0-9]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {
            "sheet_csv_url": DEFAULT_SHEET_CSV_URL,
            "refresh_interval_sec": DEFAULT_REFRESH_INTERVAL,
            "object_synonyms": {},
        }
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as exc:
        print(f"[!] Не удалось прочитать config.json: {exc}", file=sys.stderr)
        return {}


def get_sheet_url(config: dict) -> str:
    url = config.get("sheet_csv_url") or DEFAULT_SHEET_CSV_URL
    return url


def get_refresh_interval(config: dict) -> int:
    val = config.get("refresh_interval_sec", DEFAULT_REFRESH_INTERVAL)
    try:
        return max(60, int(val))
    except Exception:
        return DEFAULT_REFRESH_INTERVAL


def get_object_synonyms(config: dict) -> dict:
    data = config.get("object_synonyms", {})
    if isinstance(data, dict):
        return data
    return {}


def download_csv(url: str, dest: Path) -> bool:
    """Download CSV from public Google Sheets URL and save locally."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        dest.write_bytes(data)
        return True
    except Exception as exc:
        print(f"[!] Не удалось скачать таблицу: {exc}", file=sys.stderr)
        return False


def load_rows(csv_path: Path):
    rows = []
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row:
                continue
            rows.append(row)
    return rows


def fetch_rows(sheet_url: str) -> list[dict] | None:
    """Download sheet from Google and return rows, or None on failure."""
    ok = download_csv(sheet_url, LOCAL_CSV)
    if not ok:
        return None
    try:
        return load_rows(LOCAL_CSV)
    except Exception as exc:
        print(f"[!] Ошибка чтения скачанного CSV: {exc}", file=sys.stderr)
        return None


def load_rows_with_fallback(sheet_url: str) -> list[dict]:
    """Try Google Sheets first, fall back to local CSV."""
    rows = fetch_rows(sheet_url)
    if rows:
        print("[+] Данные загружены из Google Sheets.", file=sys.stderr)
        return rows

    # Fallback to local file
    if LOCAL_CSV.exists():
        print("[i] Используем локальную копию.", file=sys.stderr)
        return load_rows(LOCAL_CSV)

    return []


def start_background_refresh(
    shared: dict,
    lock: threading.Lock,
    sheet_url: str,
    refresh_interval: int,
):
    """Periodically re-download the sheet and update shared rows."""

    def _refresh():
        new_rows = fetch_rows(sheet_url)
        if new_rows:
            with lock:
                shared["rows"] = new_rows
            print(
                "\n[i] Данные обновлены из Google Sheets.",
                file=sys.stderr,
            )
        # schedule next
        t = threading.Timer(refresh_interval, _refresh)
        t.daemon = True
        t.start()

    t = threading.Timer(refresh_interval, _refresh)
    t.daemon = True
    t.start()


def _get_field_case_insensitive(row, field_name: str) -> str:
    for k, v in row.items():
        if k and k.strip().lower() == field_name.strip().lower():
            return v or ""
    return ""


def _get_object_code(row) -> str:
    val = _get_field_case_insensitive(row, "Объект")
    if not val and "" in row:
        val = row.get("") or ""
    return normalize(val)


def _split_queries(text: str):
    if not text:
        return []
    # Split by common separators and sentence punctuation
    parts = re.split(r"[|;/\n]+|[.!?]+", text)
    return [p.strip() for p in parts if p.strip()]


def find_best(problem_text: str, rows, top_n: int = 1):
    return find_best_with_object(problem_text, rows, top_n, None)


def find_best_with_object(
    problem_text: str,
    rows,
    top_n: int = 1,
    object_code: str | None = None,
):
    needle = normalize(problem_text)
    scored = []
    for row in rows:
        if object_code:
            row_obj = _get_object_code(row)
            if row_obj != normalize(object_code):
                continue
        problem = _get_field_case_insensitive(row, "Проблема")
        queries = _get_field_case_insensitive(row, "запросы")
        candidates = [problem] + _split_queries(queries)
        best_score = 0.0
        for cand in candidates:
            if not cand:
                continue
            cand_norm = normalize(cand)
            score = similarity(needle, cand_norm)
            if needle and needle in cand_norm:
                score = max(score, 0.95)
            if score > best_score:
                best_score = score
        scored.append((best_score, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[: max(top_n, 1)]


def format_answer(scored):
    lines = []
    for i, (score, row) in enumerate(scored, 1):
        problem = _get_field_case_insensitive(row, "Проблема")
        solution = _get_field_case_insensitive(row, "Решение")
        solution2 = _get_field_case_insensitive(row, "Решение_2")
        lines.append(f"{i}. Совпадение: {score:.2f}")
        lines.append(f"   Проблема: {problem}")
        lines.append(f"   Решение: {solution}")
        if solution2.strip():
            lines.append(f"   Решение_2: {solution2}")
    return "\n".join(lines)


def detect_object_code(query: str, object_synonyms: dict) -> str | None:
    qn = normalize(query)
    best_code = None
    best_len = 0
    for code, synonyms in object_synonyms.items():
        if not isinstance(synonyms, list):
            continue
        candidates = synonyms + [code]
        for s in candidates:
            sn = normalize(str(s))
            if sn and sn in qn and len(sn) > best_len:
                best_code = code
                best_len = len(sn)
    return best_code


def main():
    parser = argparse.ArgumentParser(
        description="Fuzzy search solution by problem text."
    )
    parser.add_argument("query", nargs="?", help="Problem text to search for.")
    parser.add_argument(
        "--top", dest="top_n", type=int, default=1,
        help="Number of top matches to show.",
    )
    args = parser.parse_args()

    config = load_config()
    sheet_url = get_sheet_url(config)
    refresh_interval = get_refresh_interval(config)
    object_synonyms = get_object_synonyms(config)

    rows = load_rows_with_fallback(sheet_url)
    if not rows:
        print("Нет данных (таблица пуста и нет локальной копии).", file=sys.stderr)
        sys.exit(1)

    if args.query:
        obj_code = detect_object_code(args.query, object_synonyms)
        scored = find_best_with_object(args.query, rows, args.top_n, obj_code)
        print(format_answer(scored))
        return

    # Interactive mode — start background refresh
    shared = {"rows": rows}
    lock = threading.Lock()
    start_background_refresh(shared, lock, sheet_url, refresh_interval)

    print("Введите проблему (пустая строка для выхода):")
    while True:
        try:
            query = input("> ").strip()
        except EOFError:
            break
        if not query:
            break
        with lock:
            current_rows = shared["rows"]
        obj_code = detect_object_code(query, object_synonyms)
        scored = find_best_with_object(query, current_rows, args.top_n, obj_code)
        print(format_answer(scored))
        print()


if __name__ == "__main__":
    main()
