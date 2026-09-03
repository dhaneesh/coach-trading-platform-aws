import json
import logging
import os
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

from common.aws import get_secret, get_parameter, table
from trading.groww_client import GrowwClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TZ = ZoneInfo("Asia/Kolkata")


def calculate_target(entry_price, profit_percent):
    return round(entry_price * (1 + profit_percent / 100), 2)


def execute_buy(*, telegram_user_id, request):
    quantity = int(request["quantity"])

    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero")

    symbol = str(request["symbol"]).upper()
    entry_label = request.get("entry", "1st")

    # Telegram stores the coach signal price under this exact name.
    entry_price = request.get("coachEntryPrice")

    if entry_price is None:
        raise ValueError("coachEntryPrice is required")

    entry_price = float(entry_price)

    profit_percent = float(
        get_parameter(
            os.environ.get(
                "PROFIT_PARAMETER_NAME",
                "/coach-trading/profit-percent",
            )
        )
    )

    target_price = calculate_target(
        entry_price,
        profit_percent,
    )

    trading_enabled = (
        os.environ.get("TRADING_ENABLED", "false").lower() == "true"
    )

    if trading_enabled is not False:
        raise RuntimeError(
            "Invalid TRADING_ENABLED configuration"
        )

    # Dry-run only:
    # authenticate to Groww to validate credentials, but do NOT
    # submit an order or GTT.
    groww_credentials = get_secret(
        os.environ["GROWW_SECRET_ARN"]
    )

    GrowwClient(groww_credentials)

    return {
        "status": "DRY_RUN",
        "symbol": symbol,
        "quantity": quantity,
        "entry": entry_label,
        "coach_entry_price": entry_price,
        "profit_percent": profit_percent,
        "gtt_target_price": target_price,
        "order_placed": False,
    }


def lambda_handler(event, context):
    try:
        user_id = str(event["telegram_user_id"])

        result = table().get_item(
            Key={
                "PK": f"USER#{user_id}",
                "SK": "PENDING_BUY",
            }
        )

        request = result.get("Item")

        if not request:
            return {
                "statusCode": 404,
                "body": json.dumps({
                    "status": "error",
                    "message": "No pending BUY request",
                }),
            }

        # The request must have been confirmed by Telegram first.
        if request.get("status") != "CONFIRMED_BUT_NOT_EXECUTED":
            return {
                "statusCode": 409,
                "body": json.dumps({
                    "status": "error",
                    "message": "BUY request is not ready for execution",
                    "currentStatus": request.get("status"),
                }),
            }

        # This is an extra safety check.
        if request.get("dryRun") is not True:
            return {
                "statusCode": 409,
                "body": json.dumps({
                    "status": "error",
                    "message": "Trading executor received a non-dry-run request",
                }),
            }

        result = execute_buy(
            telegram_user_id=user_id,
            request=request,
        )

        timestamp = datetime.now(TZ).isoformat()

        # Atomic transition prevents duplicate execution attempts.
        table().update_item(
            Key={
                "PK": f"USER#{user_id}",
                "SK": "PENDING_BUY",
            },
            UpdateExpression=(
                "SET #s = :executed, executionAt = :t, "
                "executionStatus = :es, orderPlaced = :op, "
                "gttTargetPrice = :target"
            ),
            ConditionExpression=(
                "#s = :confirmed AND dryRun = :dry"
            ),
            ExpressionAttributeNames={
                "#s": "status",
            },
            ExpressionAttributeValues={
                ":confirmed": "CONFIRMED_BUT_NOT_EXECUTED",
                ":executed": "DRY_RUN_EXECUTED",
                ":t": timestamp,
                ":es": "DRY_RUN",
                ":op": False,
                ":target": Decimal(
                    str(result["gtt_target_price"])
                ),
                ":dry": True,
            },
        )

        logger.info(
            "Dry-run trading execution completed: %s",
            result,
        )

        return {
            "statusCode": 200,
            "body": json.dumps(result),
        }

    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning(
                "Trading request was already processed for user %s",
                event.get("telegram_user_id"),
            )
            return {
                "statusCode": 409,
                "body": json.dumps({
                    "status": "already_processed",
                    "message": "BUY request was already processed",
                }),
            }

        logger.exception("DynamoDB error in Trading Lambda")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "status": "error",
                "message": "Trading dry-run failed",
            }),
        }

    except Exception:
        logger.exception("Trading Lambda failed")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "status": "error",
                "message": "Trading dry-run failed",
            }),
        }