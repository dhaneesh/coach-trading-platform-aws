import json
import logging
import os
import uuid
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from common.aws import get_secret, table
from telegram.commands import parse

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TZ = ZoneInfo("Asia/Kolkata")

def send(chat_id, text):
    import urllib.request
    import urllib.parse

    token = get_secret(
        os.environ["TELEGRAM_BOT_TOKEN_SECRET_ARN"]
    )["bot_token"]

    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text}
    ).encode()

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


def current_monthly_file():
    now = datetime.now(TZ)
    return f"T20-20_{now.strftime('%b').upper()}_{now.strftime('%y')}"


def normalize_symbol(value):
    value = str(value or "").strip().upper()
    if ":" in value:
        return value.split(":", 1)[1]
    return value


def normalize_entry(value):
    value = str(value or "").strip().lower()
    return {
        "1": "1st",
        "1st": "1st",
        "first": "1st",
        "2": "2nd",
        "2nd": "2nd",
        "second": "2nd",
        "3": "3rd",
        "3rd": "3rd",
        "third": "3rd",
    }.get(value)


def find_active_signal(symbol, entry):
    monthly_file = current_monthly_file()
    response = table().query(
        KeyConditionExpression=Key("PK").eq(
            f"SIGNAL#{monthly_file}"
        )
    )

    target_symbol = normalize_symbol(symbol)
    target_entry = normalize_entry(entry)

    for item in response.get("Items", []):
        item_symbol = normalize_symbol(item.get("nse"))
        item_entry = normalize_entry(
            item.get("entryLabel") or item.get("entry")
        )

        active = item.get("active") is True
        status = str(item.get("status", "")).upper()

        if (
            item_symbol == target_symbol
            and item_entry == target_entry
            and active
            and status == "ACTIVATED"
        ):
            return item

    return None

# Completed requests are archived before the single active-request slot is reused.
ARCHIVABLE_STATUSES = {
    "ORDER_PLACED",
    "DRY_RUN_EXECUTED",
    "BUY_REJECTED",
    "VALIDATION_FAILED",
    "INSUFFICIENT_FUNDS",
    "CANCELLED",
}

BLOCKING_STATUSES = {
    "PENDING_CONFIRMATION",
    "CONFIRMED_BUT_NOT_EXECUTED",
    "BUY_SUBMITTING",
    "BUY_SUBMITTED",
    "BUY_EXECUTED",
    "GTT_SUBMITTING",
    "GTT_SUBMITTED",
    "BUY_PENDING",
    "GTT_PENDING",
    "GTT_FAILED_MANUAL_ACTION_REQUIRED",
    "TARGET_GTT_ACTIVE",
    "EXECUTION_UNKNOWN_MANUAL_REVIEW",
}


def archive_completed_request(user_id, request):
    status = str(request.get("status", "")).upper()
    if status not in ARCHIVABLE_STATUSES:
        return False

    archive_key = (
        f"TRADE#{request.get('createdAt', datetime.now(TZ).isoformat())}"
        f"#{uuid.uuid4().hex[:12]}"
    )
    archived = dict(request)
    archived["SK"] = archive_key
    archived["archivedAt"] = datetime.now(TZ).isoformat()

    # Preserve the completed request before releasing PENDING_BUY.
    table().put_item(
        Item=archived,
        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
    )

    try:
        table().delete_item(
            Key={"PK": f"USER#{user_id}", "SK": "PENDING_BUY"},
            ConditionExpression="#s = :status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": status},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning(
                "PENDING_BUY changed while archiving; archive=%s", archive_key
            )
            return False
        raise

    logger.info(
        "Archived completed BUY request: user=%s status=%s archiveKey=%s",
        user_id, status, archive_key
    )
    return True


def prepare_pending_buy_slot(user_id, chat_id, requested_symbol):
    existing = table().get_item(
        Key={"PK": f"USER#{user_id}", "SK": "PENDING_BUY"}
    ).get("Item")

    if not existing:
        return True

    status = str(existing.get("status", "")).upper()

    if status in BLOCKING_STATUSES:
        existing_symbol = str(existing.get("symbol", "")).upper()
        requested_symbol = str(requested_symbol).upper()

        if existing_symbol != requested_symbol:
            return True

        send(
            chat_id,
            (
                "A BUY request is already active or needs manual review.\n\n"
                f"Symbol: {existing.get('symbol', '-')}\n"
                f"Quantity: {existing.get('quantity', '-')}\n"
                f"Status: {status}\n\n"
                "Complete or cancel that request before starting another BUY."
            ),
        )
        return False

    if status in ARCHIVABLE_STATUSES:
        return archive_completed_request(user_id, existing)

    send(
        chat_id,
        (
            "A previous BUY request has an unrecognized status and is blocked "
            "for safety.\n\n"
            f"Status: {status}"
        ),
    )
    return False

def execution_message(result):
    status = str(result.get("status", "")).upper()
    symbol = result.get("symbol", "-")
    quantity = result.get("quantity", "-")

    if status == "DRY_RUN":
        return (
            "DRY-RUN EXECUTION COMPLETE.\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}\n"
            f"Coach entry: {result.get('coach_entry_price', '-') }\n"
            f"Profit: {result.get('profit_percent', '-') }%\n"
            f"Target: {result.get('gtt_target_price', '-') }\n"
            "Order placed: NO"
        )

    if status == "ORDER_PLACED":
        average = result.get("buy_average_price")
        target = result.get("gtt_target_price")
        return (
            "BUY + SELL GTT COMPLETED.\n\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}\n"
            f"BUY average: {average if average is not None else '-'}\n"
            f"SELL GTT target: {target if target is not None else '-'}\n"
            f"BUY order ID: {result.get('buy_order_id', '-')}\n"
            f"GTT ID: {result.get('gtt_id', '-')}"
        )

    if status == "INSUFFICIENT_FUNDS":
        return (
            "BUY NOT PLACED — INSUFFICIENT FUNDS.\n\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}\n"
            f"{result.get('message', 'Insufficient CNC funds.')}"
        )

    if status == "BUY_REJECTED":
        return (
            "BUY WAS REJECTED.\n\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}\n"
            f"Groww status: {result.get('buy_status', '-')}\n"
            "No SELL GTT was created."
        )

    if status == "BUY_PENDING":
        return (
            "BUY IS STILL PENDING.\n\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}\n"
            f"Groww status: {result.get('buy_status', '-')}\n\n"
            "No second BUY will be submitted automatically."
        )

    if status == "TARGET_GTT_ACTIVE":
        return (
            "TRADE EXECUTED SUCCESSFULLY.\n\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}\n"
            f"BUY average price: ₹{result.get('buy_average_price', '-')}\n"
            f"SELL target: ₹{result.get('gtt_target_price', '-')}\n"
            "SELL target GTT is ACTIVE.\n\n"
            "No further action is required."
        )

    if status == "GTT_PENDING":
        return (
            "BUY IS EXECUTED; SELL GTT IS PENDING.\n\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}\n"
            f"GTT status: {result.get('gtt_status', '-')}\n\n"
            "No second GTT will be created automatically."
        )

    if status == "GTT_FAILED_MANUAL_ACTION_REQUIRED":
        return (
            "BUY WAS EXECUTED, BUT SELL GTT NEEDS MANUAL ACTION.\n\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}\n"
            f"{result.get('message', 'Manual GTT action is required.')}"
        )

    if status == "EXECUTION_UNKNOWN_MANUAL_REVIEW":
        return (
            "BUY STATUS IS UNKNOWN — MANUAL REVIEW REQUIRED.\n\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}\n"
            f"{result.get('message', 'Do not retry the BUY automatically.')}"
        )

    if status == "VALIDATION_FAILED":
        return (
            "BUY VALIDATION FAILED.\n\n"
            f"{result.get('message', 'The BUY request failed validation.')}"
        )

    if status == "ALREADY_PROCESSED":
        return (
            "This BUY request has already been processed.\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}"
        )

    if status == "ALREADY_PROCESSING":
        return (
            "BUY request is already being processed.\n\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}\n\n"
            "Please do not send another CONFIRM."
        )

    return (
        "Trading execution failed.\n\n"
        f"Symbol: {symbol}\n"
        f"Quantity: {quantity}\n"
        f"{result.get('message', 'Unexpected trading status.')}"
    )


def handle_buy(user_id, chat_id, args):
    symbol = args["symbol"]
    quantity = int(args["quantity"])
    entry = args.get("entry") or "1st"

    signal = find_active_signal(symbol, entry)
    if not signal:
        send(
            chat_id,
            (
                "No active signal found.\n\n"
                f"Symbol: {normalize_symbol(symbol)}\n"
                f"Entry: {normalize_entry(entry)}\n"
                f"File: {current_monthly_file()}"
            ),
        )
        return

    coach_entry_price = signal.get("entryValue")
    if coach_entry_price is None:
        send(chat_id, "Signal does not contain an entry price.")
        return

    monthly_file = signal.get(
        "monthlyFile", current_monthly_file()
    )
    signal_id = signal.get("id") or (
        f"{monthly_file}|{signal.get('nse')}|"
        f"ENTRY_{signal.get('entryNumber')}|"
        f"{signal.get('entryValue')}"
    )

    now = datetime.now(TZ).isoformat()

    # Do not overwrite an existing PENDING_BUY request.
    if not prepare_pending_buy_slot(user_id, chat_id, symbol):
        return

    table().put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": "PENDING_BUY",
            "status": "PENDING_CONFIRMATION",
            "symbol": normalize_symbol(symbol),
            "quantity": quantity,
            "entry": normalize_entry(entry),
            "entryLabel": normalize_entry(entry),
            "entryNumber": int(signal.get("entryNumber", 1)),
            "monthlyFile": monthly_file,
            "signalId": signal_id,
            "signalRowNumber": int(
                signal.get("rowNumber", signal.get("signalRowNumber", 0))
            ),
            "coachEntryPrice": Decimal(str(coach_entry_price)),
            "createdAt": now,
            "chatId": chat_id,
            "dryRun": True,
        },
        ConditionExpression="attribute_not_exists(PK)",
    )


    send(
        chat_id,
        (
            "BUY request created.\n\n"
            f"Symbol: {normalize_symbol(symbol)}\n"
            f"Quantity: {quantity}\n"
            f"Entry: {normalize_entry(entry)}\n"
            f"Coach entry: {float(coach_entry_price):.2f}\n\n"
            "Reply CONFIRM to continue.\n"
            "Reply CANCEL to cancel."
        ),
    )


def handle_confirm(user_id, chat_id):
    try:
        table().update_item(
            Key={
                "PK": f"USER#{user_id}",
                "SK": "PENDING_BUY",
            },
            UpdateExpression=(
                "SET #s = :confirmed, confirmedAt = :t"
            ),
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":pending": "PENDING_CONFIRMATION",
                ":confirmed": "CONFIRMED_BUT_NOT_EXECUTED",
                ":t": datetime.now(TZ).isoformat(),
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != (
            "ConditionalCheckFailedException"
        ):
            raise

        existing = table().get_item(
            Key={
                "PK": f"USER#{user_id}",
                "SK": "PENDING_BUY",
            }
        ).get("Item")

        if not existing:
            send(chat_id, "There is no pending BUY request.")
            return

        status = existing.get("status")
        if status == "PENDING_CONFIRMATION":
            send(chat_id, "BUY confirmation is already being processed.")
            return

        send(
            chat_id,
            (
                "This BUY request has already been processed.\n"
                f"Symbol: {existing.get('symbol', '-')}\n"
                f"Quantity: {existing.get('quantity', '-')}\n"
                f"Status: {status}"
            ),
        )
        return

    logger.info(
        "BUY confirmed and queued for EC2 trading worker: user_id=%s",
        user_id,
    )

    send(
        chat_id,
        (
            "BUY confirmed and queued.\n"
            "The trading worker will process the request shortly."
        ),
    )


def handle_cancel(user_id, chat_id):
    try:
        table().delete_item(
            Key={
                "PK": f"USER#{user_id}",
                "SK": "PENDING_BUY",
            },
            ConditionExpression="attribute_exists(PK)",
        )
        send(chat_id, "Pending BUY request cancelled.")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == (
            "ConditionalCheckFailedException"
        ):
            send(chat_id, "There is no pending BUY request.")
            return
        raise


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        message = body.get("message") or {}
        sender = message.get("from") or {}
        chat = message.get("chat") or {}

        user_id = str(sender.get("id", ""))
        chat_id = str(chat.get("id", ""))

        if user_id != os.environ["TELEGRAM_AUTHORIZED_USER_ID"]:
            return {
                "statusCode": 200,
                "body": json.dumps({"ok": True}),
            }

        text = str(message.get("text") or "")
        logger.info(
            "Telegram message: user_id=%s chat_id=%s text=%r",
            user_id,
            chat_id,
            text,
        )

        parsed = parse(text)
        if not parsed:
            send(
                chat_id,
                "Available commands:\n\n"
                "BUY GAIL 10\n"
                "BUY SPARC 10 2nd\n\n"
                "CONFIRM\n"
                "CANCEL",
            )
            return {
                "statusCode": 200,
                "body": json.dumps({"ok": True}),
            }

        cmd, args = parsed

        if cmd == "buy":
            handle_buy(user_id, chat_id, args)
        elif cmd == "confirm":
            handle_confirm(user_id, chat_id)
        elif cmd == "cancel":
            handle_cancel(user_id, chat_id)

        return {
            "statusCode": 200,
            "body": json.dumps({"ok": True}),
        }

    except Exception:
        logger.exception("Telegram Lambda failed")
        # Return 200 so Telegram does not repeatedly retry the update.
        try:
            if 'chat_id' in locals() and chat_id:
                send(
                    chat_id,
                    "Telegram processing failed. Check CloudWatch logs.",
                )
        except Exception:
            logger.exception("Failed to send Telegram error message")

        return {
            "statusCode": 200,
            "body": json.dumps({"ok": True}),
        }
