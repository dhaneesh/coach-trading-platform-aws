import json
import logging
import os
import boto3
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key, Attr

from common.aws import get_secret, table
from telegram.commands import parse

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TZ = ZoneInfo("Asia/Kolkata")

lambda_client = boto3.client("lambda")

MONTH_NAMES = {
    1: "JAN",
    2: "FEB",
    3: "MAR",
    4: "APR",
    5: "MAY",
    6: "JUN",
    7: "JUL",
    8: "AUG",
    9: "SEP",
    10: "OCT",
    11: "NOV",
    12: "DEC",
}


def current_file_name():
    now = datetime.now(TZ)
    return f"T20-20_{MONTH_NAMES[now.month]}_{now.strftime('%y')}"

def send(chat_id, text):
    token = get_secret(
        os.environ["TELEGRAM_BOT_TOKEN_SECRET_ARN"]
    )["bot_token"]

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
    }).encode()

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read().decode()

    logger.info(
        "Telegram sendMessage response: status=%s body=%s",
        response.status,
        body,
    )


def lambda_handler(event, context):
    try:
        logger.info("Incoming event: %s", json.dumps(event))

        body = json.loads(event.get("body") or "{}")
        message = body.get("message") or {}

        sender = message.get("from") or {}
        chat = message.get("chat") or {}

        user_id = str(sender.get("id", ""))
        chat_id = str(chat.get("id", ""))
        text = str(message.get("text") or "")

        logger.info(
            "Telegram message: user_id=%s chat_id=%s text=%r",
            user_id,
            chat_id,
            text,
        )

        # Only allow the configured Telegram user.
        if user_id != os.environ["TELEGRAM_AUTHORIZED_USER_ID"]:
            logger.warning("Unauthorized user: %s", user_id)
            return {
                "statusCode": 200,
                "body": json.dumps({"ok": True}),
            }

        parsed = parse(text)

        # Unknown command -> help
        if not parsed:
            send(
                chat_id,
                "Available commands:\n\n"
                "BUY YESBANK 10\n"
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


        # ---------------------------------------------------------
        # BUY
        # ---------------------------------------------------------
        if cmd == "buy":
            symbol = args["symbol"]
            quantity = args["quantity"]
            entry_label = args.get("entry") or "1st"

            if quantity <= 0:
                send(chat_id, "Quantity must be greater than zero.")
                return {
                    "statusCode": 200,
                    "body": json.dumps({"ok": True}),
                }

            monthly_file = current_file_name()

            logger.info(
                "Looking for active coach signal: file=%s symbol=%s entry=%s",
                monthly_file,
                symbol,
                entry_label,
            )

            # Find the currently active signal for this symbol/entry.
            response = table().query(
                KeyConditionExpression=Key("PK").eq(
                    f"SIGNAL#{monthly_file}"
                ),
                FilterExpression=(
                    Attr("nse").eq(symbol)
                    & Attr("entryLabel").eq(entry_label)
                    & Attr("active").eq(True)
                    & Attr("status").eq("ACTIVATED")
                ),
            )

            signals = response.get("Items", [])

            if not signals:
                send(
                    chat_id,
                    f"No active coach signal found for "
                    f"{symbol} ({entry_label}).\n\n"
                    f"BUY request was not created.",
                )
                return {
                    "statusCode": 200,
                    "body": json.dumps({"ok": True}),
                }

            if len(signals) > 1:
                logger.error(
                    "Multiple active signals found: %s",
                    signals,
                )

                send(
                    chat_id,
                    f"Multiple active coach signals found for "
                    f"{symbol} ({entry_label}).\n\n"
                    f"BUY request was not created.",
                )
                return {
                    "statusCode": 200,
                    "body": json.dumps({"ok": True}),
                }

            signal = signals[0]

            entry_value = signal["entryValue"]

            logger.info(
                "Matched signal: id=%s symbol=%s entry=%s price=%s",
                signal.get("id"),
                signal.get("nse"),
                signal.get("entryLabel"),
                entry_value,
            )

            now = datetime.now(TZ).isoformat()

            pending = {
                "PK": f"USER#{user_id}",
                "SK": "PENDING_BUY",
                "status": "PENDING_CONFIRMATION",

                "symbol": symbol,
                "quantity": quantity,

                "entry": entry_label,

                # Coach signal details
                "monthlyFile": monthly_file,
                "signalId": signal["id"],
                "entryNumber": signal["entryNumber"],
                "entryLabel": signal["entryLabel"],
                "coachEntryPrice": entry_value,
                "signalRowNumber": signal.get("rowNumber"),

                "createdAt": now,
                "chatId": chat_id,

                # Safety flag while migration is in progress
                "dryRun": True,
            }

            table().put_item(Item=pending)

            send(
                chat_id,
                f"BUY request created.\n\n"
                f"Symbol: {symbol}\n"
                f"Quantity: {quantity}\n"
                f"Entry: {entry_label}\n"
                f"Coach entry: {entry_value}\n\n"
                f"Reply CONFIRM to continue.\n"
                f"Reply CANCEL to cancel.\n\n"
                f"DRY RUN: No Groww order will be placed.",
            )


        # ---------------------------------------------------------
        # CANCEL
        # ---------------------------------------------------------
        elif cmd == "cancel":
            result = table().get_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": "PENDING_BUY",
                }
            )

            if "Item" not in result:
                send(chat_id, "There is no pending BUY request.")
            else:
                table().delete_item(
                    Key={
                        "PK": f"USER#{user_id}",
                        "SK": "PENDING_BUY",
                    }
                )

                send(chat_id, "Pending BUY request cancelled.")

        # ---------------------------------------------------------
        # CONFIRM
        # ---------------------------------------------------------
        elif cmd == "confirm":

            result = table().get_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": "PENDING_BUY",
                }
            )

            pending = result.get("Item")

            if not pending:
                send(chat_id, "There is no pending BUY request.")
            elif pending.get("status") != "PENDING_CONFIRMATION":
                send(
                    chat_id,
                    f"This BUY request has already been processed.\n\n"
                    f"Symbol: {pending['symbol']}\n"
                    f"Quantity: {pending['quantity']}\n"
                    f"Status: {pending.get('status')}",
                )
            else:
                try:
                    # Atomically claim the confirmation.
                    table().update_item(
                        Key={
                            "PK": f"USER#{user_id}",
                            "SK": "PENDING_BUY",
                        },
                        UpdateExpression=(
                            "SET #s = :confirmed, confirmedAt = :t"
                        ),
                        ConditionExpression="#s = :pending",
                        ExpressionAttributeNames={
                            "#s": "status"
                        },
                        ExpressionAttributeValues={
                            ":pending": "PENDING_CONFIRMATION",
                            ":confirmed": "CONFIRMED_BUT_NOT_EXECUTED",
                            ":t": datetime.now(TZ).isoformat(),
                        },
                    )

                    # Invoke Trading Lambda asynchronously.
                    payload = json.dumps({
                        "telegram_user_id": user_id
                    }).encode()

                    invoke_response = lambda_client.invoke(
                        FunctionName=os.environ["TRADING_FUNCTION_NAME"],
                        InvocationType="RequestResponse",
                        Payload=payload,
                    )

                    raw_payload = invoke_response["Payload"].read()
                    trading_result = json.loads(
                        raw_payload.decode() or "{}"
                    )

                    logger.info(
                        "Trading Lambda response: %s",
                        trading_result,
                    )

                    # Lambda invocation itself succeeded.
                    if trading_result.get("statusCode") == 200:
                        body = trading_result.get("body", "{}")
                        result = json.loads(body)

                        send(
                            chat_id,
                            f"DRY-RUN EXECUTION COMPLETE.\n\n"
                            f"Symbol: {result['symbol']}\n"
                            f"Quantity: {result['quantity']}\n"
                            f"Coach entry: {result['coach_entry_price']}\n"
                            f"Profit: {result['profit_percent']}%\n"
                            f"Target: {result['gtt_target_price']}\n\n"
                            f"Order placed: NO",
                        )
                    else:
                        send(
                            chat_id,
                            "Trading dry-run failed. "
                            "No Groww order was placed.",
                        )

                except ClientError as exc:
                    if exc.response["Error"]["Code"] == (
                        "ConditionalCheckFailedException"
                    ):
                        send(
                            chat_id,
                            "This BUY request was already confirmed "
                            "or processed.",
                        )
                    else:
                        raise

    except Exception:
        logger.exception("Telegram Lambda failed")

        # Return 200 so Telegram does not repeatedly retry
        # the same webhook update while we are debugging.
        return {
            "statusCode": 200,
            "body": json.dumps({"ok": True}),
        }