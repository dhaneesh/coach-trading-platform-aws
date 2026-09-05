import json
import logging
import os
import secrets
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

from common.aws import get_secret, get_parameter, table
from trading.groww_client import GrowwClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TZ = ZoneInfo("Asia/Kolkata")

DEFAULT_MAX_ORDER_VALUE = 25000
DEFAULT_MAX_ORDER_QUANTITY = 100


def calculate_target(entry_price, profit_percent):
    return round(entry_price * (1 + profit_percent / 100), 2)


def is_market_open():
    now = datetime.now(TZ)

    if now.weekday() >= 5:
        return False

    return time(9, 15) <= now.time() <= time(15, 30)


def make_order_reference(prefix):
    now = datetime.now(TZ)
    return f"{prefix}{now.strftime('%H%M%S')}{secrets.token_hex(4).upper()}"


def extract_value(data, *keys):
    if not isinstance(data, dict):
        return None

    for key in keys:
        if key in data and data[key] is not None:
            return data[key]

    return None


def validate_instrument(instrument, symbol):
    if not instrument:
        raise ValueError(
            f"Groww instrument not found for {symbol}"
        )

    groww_symbol = extract_value(
        instrument,
        "groww_symbol",
        "growwSymbol",
    )

    if not groww_symbol:
        raise ValueError(
            f"Groww trading symbol missing for {symbol}"
        )

    buy_allowed = extract_value(
        instrument,
        "buy_allowed",
        "buyAllowed",
    )

    if buy_allowed is not None:
        allowed = str(buy_allowed).strip().lower()
        if allowed not in {"1", "true", "yes"}:
            raise ValueError(
                f"BUY is not allowed for {symbol}"
            )

    return groww_symbol


def validate_quantity(quantity):
    max_quantity = int(
        os.environ.get(
            "MAX_ORDER_QUANTITY",
            str(DEFAULT_MAX_ORDER_QUANTITY),
        )
    )

    if quantity <= 0:
        raise ValueError(
            "Quantity must be greater than zero"
        )

    if quantity > max_quantity:
        raise ValueError(
            f"Quantity exceeds maximum allowed quantity "
            f"of {max_quantity}"
        )


def execute_buy(*, telegram_user_id, request):
    quantity = int(request["quantity"])
    validate_quantity(quantity)

    symbol = str(request["symbol"]).upper()
    entry_label = request.get("entry", "1st")

    entry_price = request.get("coachEntryPrice")

    if entry_price is None:
        raise ValueError(
            "coachEntryPrice is required"
        )

    entry_price = float(entry_price)

    if entry_price <= 0:
        raise ValueError(
            "coachEntryPrice must be greater than zero"
        )

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

    max_order_value = float(
        os.environ.get(
            "MAX_ORDER_VALUE",
            str(DEFAULT_MAX_ORDER_VALUE),
        )
    )

    groww_credentials = get_secret(
        os.environ["GROWW_SECRET_ARN"]
    )

    groww = GrowwClient(groww_credentials)

    # --------------------------------------------------------------
    # Validate the instrument before any possible order submission.
    # --------------------------------------------------------------

    instrument = groww.get_instrument(symbol)
    groww_symbol = validate_instrument(
        instrument,
        symbol,
    )

    # --------------------------------------------------------------
    # Read-only market-price validation.
    # --------------------------------------------------------------

    ltp = float(groww.get_ltp(symbol))

    if ltp <= 0:
        raise ValueError(
            f"Invalid LTP received for {symbol}: {ltp}"
        )

    estimated_order_value = ltp * quantity

    if estimated_order_value > max_order_value:
        raise ValueError(
            f"Estimated order value ₹{estimated_order_value:.2f} "
            f"exceeds maximum allowed value "
            f"₹{max_order_value:.2f}"
        )

    market_open = is_market_open()

    trading_enabled = (
        os.environ.get(
            "TRADING_ENABLED",
            "false",
        ).strip().lower()
        == "true"
    )

    # --------------------------------------------------------------
    # HARD SAFETY GATE
    # --------------------------------------------------------------

    if not trading_enabled:
        return {
            "status": "DRY_RUN",
            "symbol": symbol,
            "groww_symbol": groww_symbol,
            "quantity": quantity,
            "entry": entry_label,
            "coach_entry_price": entry_price,
            "profit_percent": profit_percent,
            "gtt_target_price": target_price,
            "current_ltp": ltp,
            "estimated_order_value": estimated_order_value,
            "market_open": market_open,
            "order_placed": False,
            "gtt_created": False,
        }

    # --------------------------------------------------------------
    # REAL TRADING STARTS HERE
    # --------------------------------------------------------------

    if not market_open:
        raise ValueError(
            "Market is currently closed"
        )

    # --------------------------------------------------------------
    # Fresh CNC funds check immediately before the real BUY.
    #
    # We check both:
    #   1. currently available CNC balance
    #   2. exact Groww margin requirement for this order
    # --------------------------------------------------------------

    funds = groww.check_cnc_funds(
        trading_symbol=symbol,
        quantity=quantity,
        order_type=groww.client.ORDER_TYPE_MARKET,
        price=0.0,
    )

    logger.info(
        "BUY funds check: symbol=%s quantity=%s available=%s required=%s sufficient=%s",
        symbol,
        quantity,
        funds["cnc_balance_available"],
        funds["total_requirement"],
        funds["sufficient"],
    )

    if not funds["sufficient"]:
        raise ValueError(
            f"Insufficient CNC funds for {symbol}. "
            f"Available ₹{funds['cnc_balance_available']:.2f}; "
            f"required ₹{funds['total_requirement']:.2f}"
        )

    order_reference_id = make_order_reference("OC")

    logger.info(
        "Placing REAL BUY: symbol=%s quantity=%s reference=%s",
        symbol,
        quantity,
        order_reference_id,
    )

    buy_response = groww.place_market_buy(
        trading_symbol=symbol,
        quantity=quantity,
        order_reference_id=order_reference_id,
    )

    groww_order_id = extract_value(
        buy_response,
        "groww_order_id",
        "growwOrderId",
        "order_id",
        "orderId",
    )

    if not groww_order_id:
        raise RuntimeError(
            f"Groww BUY response did not contain an order ID: "
            f"{buy_response}"
        )

    # --------------------------------------------------------------
    # Verify BUY execution.
    # --------------------------------------------------------------

    order_detail = groww.get_order_detail(
        groww_order_id
    )

    buy_status = str(
        extract_value(
            order_detail,
            "order_status",
            "orderStatus",
            "status",
        )
        or ""
    ).upper()

    average_fill_price = extract_value(
        order_detail,
        "average_fill_price",
        "averageFillPrice",
        "avg_fill_price",
        "avgFillPrice",
    )

    logger.info(
        "BUY result: order_id=%s status=%s average_fill_price=%s",
        groww_order_id,
        buy_status,
        average_fill_price,
    )

    if buy_status != "EXECUTED":
        return {
            "status": "BUY_REJECTED",
            "symbol": symbol,
            "quantity": quantity,
            "buy_order_id": groww_order_id,
            "buy_order_reference": order_reference_id,
            "buy_status": buy_status,
            "order_placed": True,
            "gtt_created": False,
        }

    if average_fill_price is None:
        raise RuntimeError(
            "BUY was executed but average fill price was not returned"
        )

    average_fill_price = float(average_fill_price)

    # --------------------------------------------------------------
    # Create SELL GTT at coach entry + configured profit.
    # --------------------------------------------------------------

    gtt_reference_id = make_order_reference("GTT")

    logger.info(
        "Creating SELL GTT: symbol=%s quantity=%s target=%s reference=%s",
        symbol,
        quantity,
        target_price,
        gtt_reference_id,
    )

    gtt_response = groww.create_sell_gtt(
        trading_symbol=symbol,
        quantity=quantity,
        trigger_price=target_price,
        reference_id=gtt_reference_id,
    )

    smart_order_id = extract_value(
        gtt_response,
        "smart_order_id",
        "smartOrderId",
        "smart_order_internal_id",
        "smartOrderInternalId",
    )

    if not smart_order_id:
        raise RuntimeError(
            f"Groww GTT response did not contain a smart order ID: "
            f"{gtt_response}"
        )

    # --------------------------------------------------------------
    # Verify GTT is active.
    # --------------------------------------------------------------

    smart_order_detail = groww.get_smart_order(
        smart_order_id
    )

    gtt_status = str(
        extract_value(
            smart_order_detail,
            "status",
            "order_status",
            "orderStatus",
        )
        or ""
    ).upper()

    logger.info(
        "GTT result: smart_order_id=%s status=%s",
        smart_order_id,
        gtt_status,
    )

    if gtt_status != "ACTIVE":
        return {
            "status": "BUY_PLACED_GTT_FAILED",
            "symbol": symbol,
            "quantity": quantity,
            "buy_order_id": groww_order_id,
            "buy_order_reference": order_reference_id,
            "buy_status": buy_status,
            "buy_average_price": average_fill_price,
            "gtt_id": smart_order_id,
            "gtt_reference": gtt_reference_id,
            "gtt_status": gtt_status,
            "gtt_target_price": target_price,
            "order_placed": True,
            "gtt_created": True,
            "message": (
                "BUY was executed, but the SELL GTT was not confirmed "
                "ACTIVE. Manual GTT action is required."
            ),
        }

    return {
        "status": "ORDER_PLACED",
        "symbol": symbol,
        "groww_symbol": groww_symbol,
        "quantity": quantity,
        "entry": entry_label,
        "coach_entry_price": entry_price,
        "profit_percent": profit_percent,
        "gtt_target_price": target_price,
        "current_ltp": ltp,
        "estimated_order_value": estimated_order_value,
        "buy_order_id": groww_order_id,
        "buy_order_reference": order_reference_id,
        "buy_status": buy_status,
        "buy_average_price": average_fill_price,
        "gtt_id": smart_order_id,
        "gtt_reference": gtt_reference_id,
        "gtt_status": gtt_status,
        "order_placed": True,
        "gtt_created": True,
    }


def lambda_handler(event, context):
    try:
        user_id = str(
            event["telegram_user_id"]
        )

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

        if request.get("status") != "CONFIRMED_BUT_NOT_EXECUTED":
            return {
                "statusCode": 409,
                "body": json.dumps({
                    "status": "error",
                    "message": "BUY request is not ready for execution",
                    "currentStatus": request.get("status"),
                }),
            }

        # ----------------------------------------------------------
        # Safety: requests created by the current Telegram flow
        # must still explicitly be marked as dry-run.
        # ----------------------------------------------------------

        if request.get("dryRun") is not True:
            return {
                "statusCode": 409,
                "body": json.dumps({
                    "status": "error",
                    "message": (
                        "Trading executor received a non-dry-run request"
                    ),
                }),
            }

        result = execute_buy(
            telegram_user_id=user_id,
            request=request,
        )

        timestamp = datetime.now(TZ).isoformat()

        # ----------------------------------------------------------
        # Persist dry-run result.
        #
        # Real execution persistence will be added once we have
        # completed the read-only validation of the Groww response
        # fields against the live account.
        # ----------------------------------------------------------

        if result["status"] == "DRY_RUN":
            table().update_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": "PENDING_BUY",
                },
                UpdateExpression=(
                    "SET #s = :executed, executionAt = :t, "
                    "executionStatus = :es, orderPlaced = :op, "
                    "gttCreated = :gc, gttTargetPrice = :target, "
                    "currentLtp = :ltp, marketOpen = :mo"
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
                    ":gc": False,
                    ":target": Decimal(
                        str(result["gtt_target_price"])
                    ),
                    ":ltp": Decimal(
                        str(result["current_ltp"])
                    ),
                    ":mo": result["market_open"],
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

        # ----------------------------------------------------------
        # Real execution persistence.
        # ----------------------------------------------------------

        table().update_item(
            Key={
                "PK": f"USER#{user_id}",
                "SK": "PENDING_BUY",
            },
            UpdateExpression=(
                "SET #s = :executed, executionAt = :t, "
                "executionStatus = :es, orderPlaced = :op, "
                "gttCreated = :gc, gttTargetPrice = :target, "
                "buyOrderId = :boid, buyOrderReference = :bref, "
                "buyStatus = :bstatus, buyAveragePrice = :avg, "
                "gttId = :gid, gttReference = :gref, "
                "gttStatus = :gstatus"
            ),
            ConditionExpression=(
                "#s = :confirmed AND dryRun = :dry"
            ),
            ExpressionAttributeNames={
                "#s": "status",
            },
            ExpressionAttributeValues={
                ":confirmed": "CONFIRMED_BUT_NOT_EXECUTED",
                ":executed": result["status"],
                ":t": timestamp,
                ":es": result["status"],
                ":op": result.get("order_placed", False),
                ":gc": result.get("gtt_created", False),
                ":target": Decimal(
                    str(result["gtt_target_price"])
                ),
                ":boid": result.get("buy_order_id", "N/A"),
                ":bref": result.get("buy_order_reference", "N/A"),
                ":bstatus": result.get("buy_status", "N/A"),
                ":avg": Decimal(
                    str(result.get("buy_average_price", 0))
                ),
                ":gid": result.get("gtt_id", "N/A"),
                ":gref": result.get("gtt_reference", "N/A"),
                ":gstatus": result.get("gtt_status", "N/A"),
                ":dry": True,
            },
        )

        logger.info(
            "Trading execution completed: %s",
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

        logger.exception(
            "DynamoDB error in Trading Lambda"
        )

        return {
            "statusCode": 500,
            "body": json.dumps({
                "status": "error",
                "message": "Trading execution failed",
            }),
        }

    except ValueError as exc:
        logger.warning(
            "Trading validation failed: %s",
            exc,
        )

        return {
            "statusCode": 400,
            "body": json.dumps({
                "status": "validation_error",
                "message": str(exc),
            }),
        }

    except Exception:
        logger.exception(
            "Trading Lambda failed"
        )

        return {
            "statusCode": 500,
            "body": json.dumps({
                "status": "error",
                "message": "Trading execution failed",
            }),
        }
