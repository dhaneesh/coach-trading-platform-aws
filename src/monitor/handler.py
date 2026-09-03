import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, time
from zoneinfo import ZoneInfo
from decimal import Decimal

from common.aws import get_secret, table
from monitor.sheets import (
    build_services,
    current_file_name,
    find_spreadsheet,
    get_sheet_rows,
)
from monitor.signals import extract_signals

TZ = ZoneInfo("Asia/Kolkata")

STATE_PK_PREFIX = "SIGNAL#"
PENDING_PK = "PENDING#"
SUMMARY_PK = "SUMMARY#"

def dynamodb_safe(value):
    if isinstance(value, float):
        return Decimal(str(value))

    if isinstance(value, dict):
        return {
            key: dynamodb_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [
            dynamodb_safe(item)
            for item in value
        ]

    return value

def now_ist() -> datetime:
    return datetime.now(TZ)


def iso_now() -> str:
    return now_ist().isoformat()


def today_key() -> str:
    return now_ist().strftime("%Y-%m-%d")


def display_date() -> str:
    return now_ist().strftime("%d-%b-%Y")


def in_market_hours() -> bool:
    now = now_ist()

    if now.weekday() >= 5:
        return False

    return time(9, 15) <= now.time() <= time(15, 30)


def send_telegram(text: str) -> None:
    token = get_secret(
        os.environ["TELEGRAM_BOT_TOKEN_SECRET_ARN"]
    )["bot_token"]

    chat_id = os.environ["TELEGRAM_AUTHORIZED_USER_ID"]

    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
        }
    ).encode()

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


def signal_pk(monthly_file: str) -> str:
    return f"{STATE_PK_PREFIX}{monthly_file}"


def signal_key(signal: dict) -> str:
    return signal["id"]


def get_signal_item(db, monthly_file: str, signal_id: str):
    response = db.get_item(
        Key={
            "PK": signal_pk(monthly_file),
            "SK": signal_id,
        }
    )
    return response.get("Item")


def put_signal_item(db, signal: dict, **extra) -> None:
    item = {
        "PK": signal_pk(signal["monthlyFile"]),
        "SK": signal["id"],
        **signal,
        **extra,
    }

    db.put_item(Item=dynamodb_safe(item))


def delete_signal_item(
    db,
    monthly_file: str,
    signal_id: str,
) -> None:
    db.delete_item(
        Key={
            "PK": signal_pk(monthly_file),
            "SK": signal_id,
        }
    )


def get_pending_items(db, monthly_file: str):
    response = db.query(
        IndexName="MonthlyFileIndex",
        KeyConditionExpression="#mf = :mf",
        ExpressionAttributeNames={
            "#mf": "monthlyFile"
        },
        ExpressionAttributeValues={
            ":mf": monthly_file
        },
    )

    return response.get("Items", [])


def signal_text(signal: dict) -> str:
    return (
        "NEW BUY SIGNAL\n"
        f"NSE code: {signal['nse']}\n"
        f"Entry: {signal['entryLabel']}\n"
        f"Entry value: {signal['entryValue']}\n"
        "Status: ACTIVATED"
    )


def save_pending(db, signal: dict, error: str) -> None:
    item = {
        "PK": f"{PENDING_PK}{signal['monthlyFile']}",
        "SK": signal["id"],
        **signal,
        "pendingAt": iso_now(),
        "error": error,
    }

    db.put_item(Item=dynamodb_safe(item))


def remove_pending(
    db,
    monthly_file: str,
    signal_id: str,
) -> None:
    db.delete_item(
        Key={
            "PK": f"{PENDING_PK}{monthly_file}",
            "SK": signal_id,
        }
    )


def retry_pending(
    db,
    monthly_file: str,
) -> list[dict]:
    response = db.query(
        KeyConditionExpression="#pk = :pk",
        ExpressionAttributeNames={"#pk": "PK"},
        ExpressionAttributeValues={
            ":pk": f"{PENDING_PK}{monthly_file}"
        },
    )

    pending = response.get("Items", [])
    sent = []

    for item in pending:
        try:
            send_telegram(signal_text(item))

            put_signal_item(
                db,
                item,
                active=True,
                notified=True,
                baseline=False,
                firstSeenAt=iso_now(),
                notificationTimestamp=iso_now(),
            )

            remove_pending(
                db,
                monthly_file,
                item["id"],
            )

            sent.append(item)

        except Exception as exc:
            db.update_item(
                Key={
                    "PK": f"{PENDING_PK}{monthly_file}",
                    "SK": item["id"],
                },
                UpdateExpression=(
                    "SET lastRetryAt = :t, retryError = :e"
                ),
                ExpressionAttributeValues={
                    ":t": iso_now(),
                    ":e": str(exc),
                },
            )

    return sent


def establish_baseline(
    db,
    monthly_file: str,
    signals: list[dict],
) -> dict:
    for signal in signals:
        put_signal_item(
            db,
            signal,
            active=True,
            notified=False,
            baseline=True,
            firstSeenAt=iso_now(),
        )

    marker_id = "__BASELINE__"

    db.put_item(
        Item={
            "PK": signal_pk(monthly_file),
            "SK": marker_id,
            "entityType": "MONTH_STATE",
            "monthlyFile": monthly_file,
            "baselineEstablishedAt": iso_now(),
        }
    )

    return {
        "status": "baseline",
        "monthlyFile": monthly_file,
        "activeCount": len(signals),
    }


def baseline_exists(db, monthly_file: str) -> bool:
    item = db.get_item(
        Key={
            "PK": signal_pk(monthly_file),
            "SK": "__BASELINE__",
        }
    ).get("Item")

    return bool(item)


def deactivate_missing_signals(
    db,
    monthly_file: str,
    active_ids: set[str],
) -> None:
    response = db.query(
        KeyConditionExpression="#pk = :pk",
        ExpressionAttributeNames={"#pk": "PK"},
        ExpressionAttributeValues={
            ":pk": signal_pk(monthly_file)
        },
    )

    for item in response.get("Items", []):
        signal_id = item.get("SK")

        if signal_id == "__BASELINE__":
            continue

        if item.get("active") is True and signal_id not in active_ids:
            db.update_item(
                Key={
                    "PK": signal_pk(monthly_file),
                    "SK": signal_id,
                },
                UpdateExpression="SET active = :false",
                ExpressionAttributeValues={
                    ":false": False,
                },
            )


def record_daily_signal(db, signal: dict) -> None:
    item = {
        "PK": f"DAILY#{signal['monthlyFile']}",
        "SK": f"{today_key()}|{signal['id']}",
        "entityType": "DAILY_SIGNAL",
        **signal,
        "notificationTimestamp": iso_now(),
    }

    db.put_item(Item=dynamodb_safe(item))


def monitor(event=None, context=None, baseline_only=False, ignore_hours=False):
    db = table()

    if (
        not ignore_hours
        and not baseline_only
        and not in_market_hours()
    ):
        return {
            "status": "skipped_outside_market_hours"
        }

    monthly_file = current_file_name()

    try:
        drive, sheets = build_services(
            os.environ["GOOGLE_SECRET_ARN"]
        )

        spreadsheet = find_spreadsheet(
            drive,
            monthly_file,
            os.environ.get(
                "GOOGLE_SPREADSHEET_FOLDER_ID",
                "",
            ),
        )

        if not spreadsheet:
            send_telegram(
                "⚠️ COACH SHEET NOT FOUND\n"
                f"Expected file:\n{monthly_file}"
            )

            return {
                "status": "sheet_not_found",
                "expected": monthly_file,
            }

        spreadsheet_id = spreadsheet["id"]

        rows = get_sheet_rows(
            sheets,
            spreadsheet_id,
            os.environ.get(
                "GOOGLE_SHEET_READ_RANGE",
                "A1:I250",
            ),
        )

        signals = extract_signals(
            rows,
            monthly_file,
        )

        active_ids = {
            signal["id"]
            for signal in signals
        }

        if not baseline_exists(db, monthly_file):
            return establish_baseline(
                db,
                monthly_file,
                signals,
            )

        if baseline_only:
            return establish_baseline(
                db,
                monthly_file,
                signals,
            )

        deactivate_missing_signals(
            db,
            monthly_file,
            active_ids,
        )

        retried = retry_pending(
            db,
            monthly_file,
        )

        sent = []

        for signal in signals:
            existing = get_signal_item(
                db,
                monthly_file,
                signal["id"],
            )

            # Still active from previous successful cycle.
            if existing and existing.get("active") is True:
                continue

            try:
                send_telegram(
                    signal_text(signal)
                )

                put_signal_item(
                    db,
                    signal,
                    active=True,
                    notified=True,
                    baseline=False,
                    firstSeenAt=(
                        existing or {}
                    ).get(
                        "firstSeenAt",
                        iso_now(),
                    ),
                    notificationTimestamp=iso_now(),
                )

                record_daily_signal(
                    db,
                    signal,
                )

                sent.append(signal)

            except Exception as exc:
                save_pending(
                    db,
                    signal,
                    str(exc),
                )

        return {
            "status": "ok",
            "monthlyFile": monthly_file,
            "detected": len(signals),
            "sent": len(sent),
            "retried": len(retried),
        }

    except Exception as exc:
        try:
            send_telegram(
                "⚠️ COACH SHEET MONITOR ERROR\n"
                "Unable to access the current month's coach sheet."
            )
        except Exception:
            pass

        return {
            "status": "error",
            "error": str(exc),
            "monthlyFile": monthly_file,
        }


def summary(event=None, context=None):
    db = table()
    monthly_file = current_file_name()
    today = today_key()

    summary_key = f"{today}"

    existing = db.get_item(
        Key={
            "PK": f"{SUMMARY_PK}{monthly_file}",
            "SK": summary_key,
        }
    ).get("Item")

    if existing:
        return {
            "status": "already_sent",
            "monthlyFile": monthly_file,
            "today": today,
        }

    response = db.query(
        KeyConditionExpression="#pk = :pk",
        ExpressionAttributeNames={"#pk": "PK"},
        ExpressionAttributeValues={
            ":pk": f"DAILY#{monthly_file}"
        },
    )

    signals = [
        item
        for item in response.get("Items", [])
        if item.get("SK", "").startswith(f"{today}|")
    ]

    if not signals:
        text = (
            "DAILY BUY SIGNAL SUMMARY\n"
            f"Date: {display_date()}\n"
            "No new BUY signals were activated today.\n"
            "Total new activated entries: 0"
        )
    else:
        body = "\n".join(
            (
                f"{index}.\n"
                f"NSE code: {signal['nse']}\n"
                f"Entry: {signal['entryLabel']}\n"
                f"Entry value: {signal['entryValue']}\n"
                "Status: ACTIVATED"
            )
            for index, signal in enumerate(signals, start=1)
        )

        text = (
            "DAILY BUY SIGNAL SUMMARY\n"
            f"Date: {display_date()}\n"
            f"{body}\n"
            f"Total new activated entries: {len(signals)}"
        )

    send_telegram(text)

    db.put_item(
        Item={
            "PK": f"{SUMMARY_PK}{monthly_file}",
            "SK": summary_key,
            "sentAt": iso_now(),
            "count": len(signals),
        }
    )

    return {
        "status": "summary_sent",
        "monthlyFile": monthly_file,
        "today": today,
        "count": len(signals),
    }


def lambda_handler(event, context):
    return monitor(event, context)
    # event = event or {}

    # return monitor(
    #     event,
    #     context,
    #     baseline_only=bool(event.get("baseline_only", False)),
    #     ignore_hours=bool(event.get("ignore_hours", False)),
    # )


def lambda_summary(event, context):
    return summary(event, context)