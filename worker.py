import logging
import os
import time
import urllib.parse
import urllib.request

import boto3

from common.aws import get_secret
from trading.executor import execute_request


TABLE_NAME = os.environ["DYNAMODB_TABLE"]
POLL_INTERVAL_SECONDS = 5
TELEGRAM_SECRET_ARN = os.environ["TELEGRAM_BOT_TOKEN_SECRET_ARN"]


EXECUTABLE_STATUSES = {
    "CONFIRMED_BUT_NOT_EXECUTED",
    "BUY_SUBMITTING",
    "BUY_SUBMITTED",
    "BUY_EXECUTED",
    "GTT_SUBMITTING",
    "GTT_SUBMITTED",
    "STOP_SUBMITTED",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger("coach-trading-worker")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def send_telegram(chat_id, text):
    try:
        secret = get_secret(TELEGRAM_SECRET_ARN)
        bot_token = secret["bot_token"]

        data = urllib.parse.urlencode(
            {
                "chat_id": str(chat_id),
                "text": text,
            }
        ).encode()

        request = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()

        logger.info(
            "Telegram notification sent: chat_id=%s",
            chat_id,
        )

        return True

    except Exception:
        logger.exception(
            "Failed to send Telegram notification: chat_id=%s",
            chat_id,
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
            f"Coach entry: {result.get('coach_entry_price', '-')}\n"
            f"Profit: {result.get('profit_percent', '-')}%\n"
            f"Target: {result.get('gtt_target_price', '-')}\n"
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

    if status == "GTT_PENDING":
        return (
            "BUY IS EXECUTED; SELL GTT IS PENDING.\n\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}\n"
            f"GTT status: {result.get('gtt_status', '-')}\n\n"
            "No second GTT will be created automatically."
        )

    if status == "STOP_PENDING":
        return (
            "BUY EXECUTED; STOP-LOSS GTT IS PENDING.\n\n"
            f"Symbol: {symbol}\n"
            f"Quantity: {quantity}\n"
            f"BUY average: {result.get('buy_average_price', '-')}\n"
            f"SELL GTT target: {result.get('gtt_target_price', '-')}\n"
            f"Stop-loss: {result.get('stop_loss_trigger', '-')}\n\n"
            f"{result.get('message', 'Stop-loss GTT requires follow-up.')}"
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


def notify_once(request, result):
    chat_id = request.get("chatId")

    if not chat_id:
        logger.warning(
            "No Telegram chatId found: PK=%s SK=%s",
            request.get("PK"),
            request.get("SK"),
        )
        return

    if isinstance(result, dict):
        result.setdefault(
            "buy_average_price",
            request.get("buyAveragePrice"),
        )
        result.setdefault(
            "gtt_target_price",
            request.get("gttTargetPrice"),
        )
        result.setdefault(
            "stop_loss_trigger",
            request.get("stopLossPrice"),
        )

    message = execution_message(result)

    try:
        table.update_item(
            Key={
                "PK": request["PK"],
                "SK": request["SK"],
            },
            UpdateExpression="SET telegramNotificationClaimedAt = :t",
            ConditionExpression=(
                "attribute_not_exists(telegramNotificationClaimedAt)"
            ),
            ExpressionAttributeValues={
                ":t": int(time.time()),
            },
        )

    except table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info(
            "Telegram notification already claimed: PK=%s SK=%s",
            request["PK"],
            request["SK"],
        )
        return

    except Exception:
        logger.exception(
            "Failed to claim Telegram notification: PK=%s SK=%s",
            request["PK"],
            request["SK"],
        )
        return

    if send_telegram(chat_id, message):
        try:
            table.update_item(
                Key={
                    "PK": request["PK"],
                    "SK": request["SK"],
                },
                UpdateExpression="SET telegramNotificationSentAt = :t",
                ExpressionAttributeValues={
                    ":t": int(time.time()),
                },
            )

        except Exception:
            logger.exception(
                "Telegram sent but failed to record sent timestamp: "
                "PK=%s SK=%s",
                request["PK"],
                request["SK"],
            )

    else:
        logger.error(
            "Telegram notification failed after claim: "
            "PK=%s SK=%s",
            request["PK"],
            request["SK"],
        )


def find_executable_requests():
    found = []

    filter_expression = (
        "#sk = :pending AND "
        "#status IN (:s1, :s2, :s3, :s4, :s5, :s6, :s7)"
    )

    expression_attribute_names = {
        "#sk": "SK",
        "#status": "status",
    }

    expression_attribute_values = {
        ":pending": "PENDING_BUY",
        ":s1": "CONFIRMED_BUT_NOT_EXECUTED",
        ":s2": "BUY_SUBMITTING",
        ":s3": "BUY_SUBMITTED",
        ":s4": "BUY_EXECUTED",
        ":s5": "GTT_SUBMITTING",
        ":s6": "GTT_SUBMITTED",
        ":s7": "STOP_SUBMITTED",
    }

    response = table.scan(
        FilterExpression=filter_expression,
        ExpressionAttributeNames=expression_attribute_names,
        ExpressionAttributeValues=expression_attribute_values,
    )

    found.extend(response.get("Items", []))

    while "LastEvaluatedKey" in response:
        response = table.scan(
            ExclusiveStartKey=response["LastEvaluatedKey"],
            FilterExpression=filter_expression,
            ExpressionAttributeNames=expression_attribute_names,
            ExpressionAttributeValues=expression_attribute_values,
        )

        found.extend(response.get("Items", []))

    return found


def process_request(request):
    user_id = request["PK"].replace("USER#", "", 1)

    logger.info(
        "Processing trading request: "
        "user_id=%s symbol=%s quantity=%s status=%s",
        user_id,
        request.get("symbol"),
        request.get("quantity"),
        request.get("status"),
    )

    result = execute_request(
        user_id=user_id,
        request=request,
    )

    if isinstance(result, dict) and result.get("status") == "DRY_RUN":
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from decimal import Decimal

        try:
            table.update_item(
                Key={
                    "PK": request["PK"],
                    "SK": request["SK"],
                },
                UpdateExpression=(
                    "SET #s = :new, "
                    "executionAt = :execution_at, "
                    "executionStatus = :execution_status, "
                    "orderPlaced = :order_placed, "
                    "gttCreated = :gtt_created, "
                    "gttTargetPrice = :target, "
                    "currentLtp = :ltp, "
                    "marketOpen = :market_open"
                ),
                ConditionExpression="#s = :expected",
                ExpressionAttributeNames={
                    "#s": "status",
                },
                ExpressionAttributeValues={
                    ":expected": "CONFIRMED_BUT_NOT_EXECUTED",
                    ":new": "DRY_RUN_EXECUTED",
                    ":execution_at": datetime.now(
                        ZoneInfo("Asia/Kolkata")
                    ).isoformat(),
                    ":execution_status": "DRY_RUN",
                    ":order_placed": False,
                    ":gtt_created": False,
                    ":target": Decimal(
                        str(result["gtt_target_price"])
                    ),
                    ":ltp": Decimal(
                        str(result["current_ltp"])
                    ),
                    ":market_open": result["market_open"],
                },
            )

            logger.info(
                "DRY_RUN request marked complete: "
                "user_id=%s symbol=%s status=DRY_RUN_EXECUTED",
                user_id,
                request.get("symbol"),
            )

        except Exception:
            logger.exception(
                "Failed to mark DRY_RUN request as complete: "
                "user_id=%s symbol=%s",
                user_id,
                request.get("symbol"),
            )

    logger.info(
        "Trading request result: "
        "user_id=%s symbol=%s status=%s result=%s",
        user_id,
        request.get("symbol"),
        result.get("status") if isinstance(result, dict) else "UNKNOWN",
        result,
    )

    notify_once(request, result)


def main():
    logger.info(
        "EC2 trading worker started. "
        "table=%s poll_interval=%ss",
        TABLE_NAME,
        POLL_INTERVAL_SECONDS,
    )

    while True:
        try:
            requests = find_executable_requests()

            if not requests:
                logger.info("No executable trading requests found.")
            else:
                logger.info(
                    "Found %s executable trading request(s).",
                    len(requests),
                )

                for request in requests:
                    try:
                        process_request(request)
                    except Exception:
                        logger.exception(
                            "Trading execution failed: PK=%s SK=%s",
                            request.get("PK"),
                            request.get("SK"),
                        )

        except Exception:
            logger.exception("Worker polling error")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
