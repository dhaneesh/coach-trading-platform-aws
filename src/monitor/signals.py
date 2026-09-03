from typing import Any

def is_activated(value: Any) -> bool:
    return str(value or "").strip().lower() == "activated"

def numeric(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None

def signal_id(monthly_file: str, nse: str, entry_number: int, entry_value: float) -> str:
    return f"{monthly_file}|{nse}|ENTRY_{entry_number}|{entry_value:.2f}"

def extract_signals(rows: list[list[Any]], monthly_file: str) -> list[dict]:
    if not rows:
        return []
    header_map = {str(h).strip().lower(): i for i, h in enumerate(rows[0])}
    nse_idx = header_map.get("nse code", 1)
    specs = {
        1: (header_map.get("1st entry", 3), header_map.get("status of column d", 4), "1st"),
        2: (header_map.get("2nd entry", 5), header_map.get("status of column f", 6), "2nd"),
        3: (header_map.get("3rd entry", 7), header_map.get("status of column h", 8), "3rd"),
    }
    out = []
    for row_number, row in enumerate(rows[1:], start=2):
        nse = str(row[nse_idx] if nse_idx < len(row) else "").strip().upper()
        if not nse:
            continue
        for entry_number, (value_idx, status_idx, label) in specs.items():
            status = row[status_idx] if status_idx < len(row) else ""
            if not is_activated(status):
                continue
            value = numeric(row[value_idx] if value_idx < len(row) else None)
            if value is None:
                continue
            out.append({
                "id": signal_id(monthly_file, nse, entry_number, value),
                "monthlyFile": monthly_file, "rowNumber": row_number, "nse": nse,
                "entryNumber": entry_number, "entryLabel": label,
                "entryValue": value, "status": "ACTIVATED",
            })
    return out
