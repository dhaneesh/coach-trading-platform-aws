import json
import logging
import os
import secrets
import time as time_module
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
DEFAULT_RECOVERY_RETRIES = 3
DEFAULT_RECOVERY_DELAY_SECONDS = 1.0

TERMINAL_BUY_FAILURES = {
    "REJECTED",
    "CANCELLED",
    "FAILED",
    "EXPIRED",
}

TERMINAL_GTT_FAILURES = {
    "CANCELLED",
    "FAILED",
    "REJECTED",
    "EXPIRED",
}


def calculate_target(entry_price, profit_percent, tick_size):
    raw_target = entry_price * (1 + profit_percent / 100)

    return round_down_to_tick(
        raw_target,
        tick_size,
    )
def round_down_to_tick(price, tick_size):
    """
    Round a price down to the nearest valid exchange tick.
    """
    from decimal import Decimal, ROUND_FLOOR

    price = Decimal(str(price))
    tick_size = Decimal(str(tick_size))

    return float(
        (price / tick_size).quantize(
            Decimal("1"),
            rounding=ROUND_FLOOR,
        )
        * tick_size
    )


def calculate_stop_loss(entry_price, stop_loss_percent, tick_size):
    raw_price = entry_price * (1 - stop_loss_percent / 100)

    return round_down_to_tick(
        raw_price,
        tick_size,
    )

def calculate_stop_loss_limit(stop_loss_price, tick_size):
    """
    Keep the SELL stop-limit price slightly below the trigger,
    then align it to the exchange tick size.
    """
    raw_limit = stop_loss_price * 0.998

    return round_down_to_tick(
        raw_limit,
        tick_size,
    )

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


def json_safe(value):
    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}

    if isinstance(value, list):
        return [json_safe(v) for v in value]

    return value


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


def get_order_status(order_detail):
    return str(
        extract_value(
            order_detail,
            "order_status",
            "orderStatus",
            "status",
        )
        or ""
    ).upper()


def get_gtt_status(smart_order_detail):
    return str(
        extract_value(
            smart_order_detail,
            "status",
            "order_status",
            "orderStatus",
        )
        or ""
    ).upper()


def decimal_value(value):
    return Decimal(str(value))


def mark_state(
    *,
    user_id,
    expected_status,
    new_status,
    extra=None,
):
    extra = extra or {}

    names = {"#s": "status"}
    values = {
        ":expected": expected_status,
        ":new": new_status,
    }

    assignments = [
        "#s = :new",
    ]

    for index, (key, value) in enumerate(extra.items()):
        name = f"#f{index}"
        val = f":v{index}"

        names[name] = key

        if isinstance(value, float):
            value = decimal_value(value)

        values[val] = value
        assignments.append(f"{name} = {val}")

    table().update_item(
        Key={
            "PK": f"USER#{user_id}",
            "SK": "PENDING_BUY",
        },
        UpdateExpression="SET " + ", ".join(assignments),
        ConditionExpression="#s = :expected",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def safe_mark_state(
    *,
    user_id,
    expected_status,
    new_status,
    extra=None,
):
    try:
        mark_state(
            user_id=user_id,
            expected_status=expected_status,
            new_status=new_status,
            extra=extra,
        )
        return True

    except ClientError as exc:
        if exc.response["Error"]["Code"] == (
            "ConditionalCheckFailedException"
        ):
            return False

        raise


def load_request(user_id):
    response = table().get_item(
        Key={
            "PK": f"USER#{user_id}",
            "SK": "PENDING_BUY",
        }
    )

    return response.get("Item")


def dry_run_execution(*, symbol, quantity, entry_label, entry_price,
                       profit_percent, target_price, groww_symbol,
                       ltp, estimated_order_value, market_open):
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


def execute_new_buy(*, user_id, request):
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

    instrument = groww.get_instrument(symbol)

    groww_symbol = validate_instrument(
        instrument,
        symbol,
    )

    try:
        tick_size = float(instrument["tick_size"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(
            f"Could not determine tick size for {symbol}."
        )

    if tick_size <= 0:
        raise ValueError(
            f"Invalid tick size for {symbol}: {tick_size}"
        )

    target_price = calculate_target(
        entry_price,
        profit_percent,
        tick_size,
    )

    ltp = float(
        groww.get_ltp(symbol)
    )

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

    if not trading_enabled:
        return (
            groww,
            dry_run_execution(
                symbol=symbol,
                quantity=quantity,
                entry_label=entry_label,
                entry_price=entry_price,
                profit_percent=profit_percent,
                target_price=target_price,
                groww_symbol=groww_symbol,
                ltp=ltp,
                estimated_order_value=estimated_order_value,
                market_open=market_open,
            ),
        )

    if not market_open:
        raise ValueError(
            "Market is currently closed"
        )

    # --------------------------------------------------------------
    # Fresh funds check immediately before the BUY state claim.
    # --------------------------------------------------------------

    funds = groww.check_cnc_funds(
        trading_symbol=symbol,
        quantity=quantity,
        order_type=groww.client.ORDER_TYPE_MARKET,
        price=0.0,
    )

    logger.info(
        "BUY funds check: symbol=%s quantity=%s "
        "available=%s required=%s sufficient=%s",
        symbol,
        quantity,
        funds["cnc_balance_available"],
        funds["total_requirement"],
        funds["sufficient"],
    )

    if not funds["sufficient"]:
        safe_mark_state(
            user_id=user_id,
            expected_status="CONFIRMED_BUT_NOT_EXECUTED",
            new_status="INSUFFICIENT_FUNDS",
            extra={
                "fundsAvailable": funds["cnc_balance_available"],
                "fundsRequired": funds["total_requirement"],
                "executionError": (
                    f"Insufficient CNC funds. "
                    f"Available ₹{funds['cnc_balance_available']:.2f}; "
                    f"required ₹{funds['total_requirement']:.2f}"
                ),
                "executionAt": datetime.now(TZ).isoformat(),
            },
        )

        return None, {
            "status": "INSUFFICIENT_FUNDS",
            "symbol": symbol,
            "quantity": quantity,
            "funds_available": funds["cnc_balance_available"],
            "funds_required": funds["total_requirement"],
            "order_placed": False,
            "gtt_created": False,
        }

    # --------------------------------------------------------------
    # Generate and persist reference BEFORE calling Groww.
    # --------------------------------------------------------------

    order_reference_id = make_order_reference("OC")

    claimed = safe_mark_state(
        user_id=user_id,
        expected_status="CONFIRMED_BUT_NOT_EXECUTED",
        new_status="BUY_SUBMITTING",
        extra={
            "buyOrderReference": order_reference_id,
            "buySubmittingAt": datetime.now(TZ).isoformat(),
            "currentLtp": ltp,
            "gttTargetPrice": target_price,
            "estimatedOrderValue": estimated_order_value,
            "fundsAvailable": funds["cnc_balance_available"],
            "fundsRequired": funds["total_requirement"],
        },
    )

    if not claimed:
        return None, {
            "status": "ALREADY_PROCESSING",
            "message": "BUY request is already being processed.",
        }

    logger.info(
        "Placing REAL BUY: symbol=%s quantity=%s reference=%s",
        symbol,
        quantity,
        order_reference_id,
    )

    # IMPORTANT:
    # If this call succeeds at Groww but Lambda crashes before the
    # following DynamoDB update, the persisted reference lets the
    # next invocation recover the BUY without submitting another one.
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
        safe_mark_state(
            user_id=user_id,
            expected_status="BUY_SUBMITTING",
            new_status="EXECUTION_UNKNOWN_MANUAL_REVIEW",
            extra={
                "executionError": (
                    "Groww accepted the BUY call but no order ID "
                    "was returned. Manual review required."
                ),
                "executionAt": datetime.now(TZ).isoformat(),
                "buyResponse": json_safe(buy_response),
            },
        )

        return None, {
            "status": "EXECUTION_UNKNOWN_MANUAL_REVIEW",
            "symbol": symbol,
            "quantity": quantity,
            "message": (
                "BUY execution could not be confirmed safely. "
                "Manual review is required. No automatic retry "
                "will submit another BUY."
            ),
        }

    mark_state(
        user_id=user_id,
        expected_status="BUY_SUBMITTING",
        new_status="BUY_SUBMITTED",
        extra={
            "buyOrderId": groww_order_id,
            "buySubmittedAt": datetime.now(TZ).isoformat(),
        },
    )

    request = load_request(user_id)

    return continue_buy_and_gtt(
        user_id=user_id,
        request=request,
        groww=groww,
    )


def recover_buy_submission(*, user_id, request, groww):
    reference = request.get("buyOrderReference")

    if not reference:
        safe_mark_state(
            user_id=user_id,
            expected_status="BUY_SUBMITTING",
            new_status="EXECUTION_UNKNOWN_MANUAL_REVIEW",
            extra={
                "executionError": (
                    "BUY_SUBMITTING state has no order reference."
                ),
                "executionAt": datetime.now(TZ).isoformat(),
            },
        )

        return {
            "status": "EXECUTION_UNKNOWN_MANUAL_REVIEW",
            "message": (
                "BUY execution state is incomplete. "
                "Manual review is required."
            ),
        }

    logger.info(
        "Recovering BUY by reference: %s",
        reference,
    )

    # Groww's documented response contains groww_order_id, order_status,
    # filled_quantity and order_reference_id. We persist the response
    # fields we need so a Lambda crash/retry never causes another BUY.
    retries = int(os.environ.get("BUY_RECOVERY_RETRIES", str(DEFAULT_RECOVERY_RETRIES)))
    delay_seconds = float(os.environ.get("BUY_RECOVERY_DELAY_SECONDS", str(DEFAULT_RECOVERY_DELAY_SECONDS)))

    response = None
    groww_order_id = None
    for attempt in range(1, retries + 1):
        response = groww.get_order_status_by_reference(reference)
        groww_order_id = extract_value(
            response,
            "groww_order_id",
            "growwOrderId",
            "order_id",
            "orderId",
        )

        if groww_order_id:
            break

        logger.warning(
            "BUY reference lookup returned no order ID: reference=%s attempt=%s/%s response=%s",
            reference,
            attempt,
            retries,
            json_safe(response),
        )

        if attempt < retries:
            time_module.sleep(delay_seconds)

    if not groww_order_id:
        safe_mark_state(
            user_id=user_id,
            expected_status="BUY_SUBMITTING",
            new_status="EXECUTION_UNKNOWN_MANUAL_REVIEW",
            extra={
                "executionError": (
                    "BUY reference could not be resolved to a Groww "
                    "order ID after recovery attempts. Automatic "
                    "re-submission is blocked."
                ),
                "executionAt": datetime.now(TZ).isoformat(),
                "buyRecoveryResponse": json_safe(response),
            },
        )

        return {
            "status": "EXECUTION_UNKNOWN_MANUAL_REVIEW",
            "message": (
                "Unable to safely determine whether the BUY was "
                "submitted. Manual review is required. "
                "No automatic retry will submit another BUY."
            ),
        }

    reference_status = str(
        extract_value(
            response,
            "order_status",
            "orderStatus",
            "status",
        )
        or ""
    ).upper()

    filled_quantity = extract_value(
        response,
        "filled_quantity",
        "filledQuantity",
    )

    claimed = safe_mark_state(
        user_id=user_id,
        expected_status="BUY_SUBMITTING",
        new_status="BUY_SUBMITTED",
        extra={
            "buyOrderId": groww_order_id,
            "buyStatus": reference_status,
            "buyFilledQuantity": filled_quantity,
            "buyRecoveredAt": datetime.now(TZ).isoformat(),
            "buyRecoveryResponse": json_safe(response),
        },
    )

    if not claimed:
        current = load_request(user_id)
        if current:
            return continue_buy_and_gtt(
                user_id=user_id,
                request=current,
                groww=groww,
            )

    request = load_request(user_id)
    if not request:
        return {
            "status": "EXECUTION_UNKNOWN_MANUAL_REVIEW",
            "message": (
                "BUY order was recovered, but the execution state "
                "could not be reloaded safely. Manual review required."
            ),
        }

    return continue_buy_and_gtt(
        user_id=user_id,
        request=request,
        groww=groww,
    )


def find_gtt_with_retries(groww, reference):
    retries = int(os.environ.get("GTT_RECOVERY_RETRIES", str(DEFAULT_RECOVERY_RETRIES)))
    delay_seconds = float(os.environ.get("GTT_RECOVERY_DELAY_SECONDS", str(DEFAULT_RECOVERY_DELAY_SECONDS)))

    for attempt in range(1, retries + 1):
        existing_gtt = groww.find_gtt_by_reference(reference)
        if existing_gtt:
            return existing_gtt

        logger.info(
            "GTT reference not visible yet: reference=%s attempt=%s/%s",
            reference,
            attempt,
            retries,
        )

        if attempt < retries:
            time_module.sleep(delay_seconds)

    return None


def continue_buy_and_gtt(*, user_id, request, groww):
    status = request.get("status")

    if status == "BUY_SUBMITTED":
        groww_order_id = request.get("buyOrderId")

        if not groww_order_id:
            safe_mark_state(
                user_id=user_id,
                expected_status="BUY_SUBMITTED",
                new_status="EXECUTION_UNKNOWN_MANUAL_REVIEW",
                extra={
                    "executionError": (
                        "BUY_SUBMITTED state has no Groww order ID."
                    ),
                    "executionAt": datetime.now(TZ).isoformat(),
                },
            )

            return {
                "status": "EXECUTION_UNKNOWN_MANUAL_REVIEW",
                "message": (
                    "BUY state is incomplete. Manual review required."
                ),
            }

        order_detail = groww.get_order_detail(
            groww_order_id
        )

        buy_status = get_order_status(order_detail)

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

        if buy_status == "EXECUTED":
            if average_fill_price is None:
                safe_mark_state(
                    user_id=user_id,
                    expected_status="BUY_SUBMITTED",
                    new_status="EXECUTION_UNKNOWN_MANUAL_REVIEW",
                    extra={
                        "buyStatus": buy_status,
                        "executionError": (
                            "BUY is EXECUTED but average fill price "
                            "was not returned."
                        ),
                        "executionAt": datetime.now(TZ).isoformat(),
                    },
                )

                return {
                    "status": "EXECUTION_UNKNOWN_MANUAL_REVIEW",
                    "message": (
                        "BUY was executed but its fill price could "
                        "not be confirmed. Manual review required."
                    ),
                }

            average_fill_price = float(average_fill_price)

            stop_loss_percent = float(
                get_parameter(
                    os.environ.get(
                        "STOP_LOSS_PARAMETER_NAME",
                        "/coach-trading/stop-loss-percent",
                    )
                )
            )

            instrument = groww.get_instrument(request["symbol"])

            try:
                tick_size = float(instrument["tick_size"])
            except (KeyError, TypeError, ValueError):
                return {
                    "status": "VALIDATION_FAILED",
                    "message": (
                        f"Could not determine tick size for "
                        f"{request['symbol']}."
                    ),
                }

            if tick_size <= 0:
                return {
                    "status": "VALIDATION_FAILED",
                    "message": (
                        f"Invalid tick size for {request['symbol']}: "
                        f"{tick_size}"
                    ),
                }

            stop_loss_price = calculate_stop_loss(
                average_fill_price,
                stop_loss_percent,
                tick_size,
            )

            stop_loss_limit_price = calculate_stop_loss_limit(
                stop_loss_price,
                tick_size,
            )

            logger.info(
                "Stop-loss calculated: symbol=%s "
                "average_fill=%s stop_percent=%s "
                "stop_trigger=%s stop_limit=%s",
                request["symbol"],
                average_fill_price,
                stop_loss_percent,
                stop_loss_price,
                stop_loss_limit_price,
            )

            mark_state(
                user_id=user_id,
                expected_status="BUY_SUBMITTED",
                new_status="BUY_EXECUTED",
                extra={
                    "buyStatus": buy_status,
                    "buyAveragePrice": average_fill_price,
                    "buyExecutedAt": datetime.now(TZ).isoformat(),
                    "stopLossPercent": stop_loss_percent,
                    "stopLossPrice": stop_loss_price,
                    "stopLossLimitPrice": stop_loss_limit_price,
                    "stopLossStatus": "NOT_SUBMITTED",
                },
            )

            request = load_request(user_id)

        elif buy_status in TERMINAL_BUY_FAILURES:
            mark_state(
                user_id=user_id,
                expected_status="BUY_SUBMITTED",
                new_status="BUY_REJECTED",
                extra={
                    "buyStatus": buy_status,
                    "orderPlaced": True,
                    "gttCreated": False,
                    "executionAt": datetime.now(TZ).isoformat(),
                },
            )

            return {
                "status": "BUY_REJECTED",
                "symbol": request["symbol"],
                "quantity": request["quantity"],
                "buy_status": buy_status,
                "order_placed": True,
                "gtt_created": False,
            }

        else:
            return {
                "status": "BUY_PENDING",
                "symbol": request["symbol"],
                "quantity": request["quantity"],
                "buy_status": buy_status,
                "message": (
                    "BUY has been submitted and is not yet in a "
                    "terminal state. No second BUY will be submitted."
                ),
            }

        # --------------------------------------------------------------
    # BUY_EXECUTED -> create/recover target GTT.
    # --------------------------------------------------------------

    if request.get("status") == "BUY_EXECUTED":
        gtt_reference = request.get("gttReference")
        stop_reference = request.get("stopLossReference")

        # Generate BOTH references before submitting either GTT.
        # They are persisted so a retry never creates a new reference.
        if not gtt_reference:
            gtt_reference = make_order_reference("GTT")

        if not stop_reference:
            stop_reference = make_order_reference("STP")

        claimed = safe_mark_state(
            user_id=user_id,
            expected_status="BUY_EXECUTED",
            new_status="GTT_SUBMITTING",
            extra={
                "gttReference": gtt_reference,
                "stopLossReference": stop_reference,
                "gttSubmittingAt": datetime.now(TZ).isoformat(),
            },
        )

        if not claimed:
            request = load_request(user_id)
        else:
            request = load_request(user_id)

    # --------------------------------------------------------------
    # GTT_SUBMITTING -> recover or create target GTT.
    # --------------------------------------------------------------

    if request.get("status") == "GTT_SUBMITTING":
        gtt_reference = request.get("gttReference")

        if not gtt_reference:
            safe_mark_state(
                user_id=user_id,
                expected_status="GTT_SUBMITTING",
                new_status="GTT_FAILED_MANUAL_ACTION_REQUIRED",
                extra={
                    "executionError": (
                        "GTT_SUBMITTING state has no target GTT reference."
                    ),
                    "executionAt": datetime.now(TZ).isoformat(),
                },
            )

            return {
                "status": "GTT_FAILED_MANUAL_ACTION_REQUIRED",
                "symbol": request["symbol"],
                "quantity": request["quantity"],
                "message": (
                    "Target GTT state is incomplete. "
                    "Manual action is required."
                ),
            }

        existing_gtt = find_gtt_with_retries(
            groww,
            gtt_reference,
        )

        if existing_gtt:
            smart_order_id = extract_value(
                existing_gtt,
                "smart_order_id",
                "smartOrderId",
                "smart_order_internal_id",
                "smartOrderInternalId",
                "id",
            )

            if smart_order_id:
                mark_state(
                    user_id=user_id,
                    expected_status="GTT_SUBMITTING",
                    new_status="GTT_SUBMITTED",
                    extra={
                        "gttId": smart_order_id,
                        "gttRecoveredAt": datetime.now(TZ).isoformat(),
                    },
                )

                request = load_request(user_id)

            else:
                safe_mark_state(
                    user_id=user_id,
                    expected_status="GTT_SUBMITTING",
                    new_status="GTT_FAILED_MANUAL_ACTION_REQUIRED",
                    extra={
                        "executionError": (
                            "Existing target GTT reference was found "
                            "but its internal ID could not be resolved."
                        ),
                        "executionAt": datetime.now(TZ).isoformat(),
                    },
                )

                return {
                    "status": "GTT_FAILED_MANUAL_ACTION_REQUIRED",
                    "symbol": request["symbol"],
                    "quantity": request["quantity"],
                    "message": (
                        "Target GTT was found but could not be safely "
                        "resolved. Manual action is required."
                    ),
                }

        else:
            target_price = float(request["gttTargetPrice"])

            logger.info(
                "Creating SELL GTT: symbol=%s quantity=%s "
                "target=%s reference=%s",
                request["symbol"],
                request["quantity"],
                target_price,
                gtt_reference,
            )

            try:
                gtt_response = groww.create_sell_gtt(
                    trading_symbol=request["symbol"],
                    quantity=int(request["quantity"]),
                    trigger_price=target_price,
                    reference_id=gtt_reference,
                )

            except Exception:
                # Never automatically resubmit using a new reference.
                logger.exception(
                    "Target GTT create call failed; blocking "
                    "automatic retry: reference=%s",
                    gtt_reference,
                )

                safe_mark_state(
                    user_id=user_id,
                    expected_status="GTT_SUBMITTING",
                    new_status="GTT_FAILED_MANUAL_ACTION_REQUIRED",
                    extra={
                        "executionError": (
                            "Target SELL GTT submission could not be "
                            "confirmed. Automatic duplicate GTT creation "
                            "is blocked. Manual review is required."
                        ),
                        "executionAt": datetime.now(TZ).isoformat(),
                        "gttCreateException": True,
                    },
                )

                return {
                    "status": "GTT_FAILED_MANUAL_ACTION_REQUIRED",
                    "symbol": request["symbol"],
                    "quantity": request["quantity"],
                    "message": (
                        "BUY was executed, but target SELL GTT "
                        "submission could not be confirmed. "
                        "Manual review is required."
                    ),
                }

            smart_order_id = extract_value(
                gtt_response,
                "smart_order_id",
                "smartOrderId",
                "smart_order_internal_id",
                "smartOrderInternalId",
                "id",
            )

            if not smart_order_id:
                recovered_gtt = find_gtt_with_retries(
                    groww,
                    gtt_reference,
                )

                smart_order_id = extract_value(
                    recovered_gtt,
                    "smart_order_id",
                    "smartOrderId",
                    "smart_order_internal_id",
                    "smartOrderInternalId",
                    "id",
                )

            if not smart_order_id:
                safe_mark_state(
                    user_id=user_id,
                    expected_status="GTT_SUBMITTING",
                    new_status="GTT_FAILED_MANUAL_ACTION_REQUIRED",
                    extra={
                        "executionError": (
                            "Groww target GTT response did not expose "
                            "a smart-order ID and the saved reference "
                            "could not be resolved."
                        ),
                        "executionAt": datetime.now(TZ).isoformat(),
                        "gttResponse": json_safe(gtt_response),
                    },
                )

                return {
                    "status": "GTT_FAILED_MANUAL_ACTION_REQUIRED",
                    "symbol": request["symbol"],
                    "quantity": request["quantity"],
                    "message": (
                        "Target SELL GTT could not be confirmed safely. "
                        "Manual action is required."
                    ),
                }

            mark_state(
                user_id=user_id,
                expected_status="GTT_SUBMITTING",
                new_status="GTT_SUBMITTED",
                extra={
                    "gttId": smart_order_id,
                    "gttSubmittedAt": datetime.now(TZ).isoformat(),
                    "gttStatus": "SUBMITTED",
                },
            )

            request = load_request(user_id)

    # --------------------------------------------------------------
    # GTT_SUBMITTED -> verify target GTT ACTIVE.
    # --------------------------------------------------------------

    if request.get("status") == "GTT_SUBMITTED":
        smart_order_id = request.get("gttId")

        if not smart_order_id:
            safe_mark_state(
                user_id=user_id,
                expected_status="GTT_SUBMITTED",
                new_status="GTT_FAILED_MANUAL_ACTION_REQUIRED",
                extra={
                    "executionError": (
                        "GTT_SUBMITTED state has no target smart-order ID."
                    ),
                    "executionAt": datetime.now(TZ).isoformat(),
                },
            )

            return {
                "status": "GTT_FAILED_MANUAL_ACTION_REQUIRED",
                "message": (
                    "Target GTT state is incomplete. "
                    "Manual action is required."
                ),
            }

        smart_order_detail = groww.get_smart_order(
            smart_order_id
        )

        gtt_status = get_gtt_status(
            smart_order_detail
        )

        logger.info(
            "Target GTT result: id=%s status=%s",
            smart_order_id,
            gtt_status,
        )

        if gtt_status == "ACTIVE":
            mark_state(
                user_id=user_id,
                expected_status="GTT_SUBMITTED",
                new_status="STOP_SUBMITTING",
                extra={
                    "buyStatus": "EXECUTED",
                    "gttStatus": gtt_status,
                    "gttCreated": True,
                    "orderPlaced": False,
                    "executionStatus": "TARGET_GTT_ACTIVE",
                    "stopSubmittingAt": datetime.now(TZ).isoformat(),
                },
            )

            request = load_request(user_id)

        elif gtt_status in TERMINAL_GTT_FAILURES:
            mark_state(
                user_id=user_id,
                expected_status="GTT_SUBMITTED",
                new_status="GTT_FAILED_MANUAL_ACTION_REQUIRED",
                extra={
                    "gttStatus": gtt_status,
                    "orderPlaced": True,
                    "gttCreated": True,
                    "executionAt": datetime.now(TZ).isoformat(),
                    "executionError": (
                        f"Target SELL GTT entered terminal status "
                        f"{gtt_status}."
                    ),
                },
            )

            return {
                "status": "GTT_FAILED_MANUAL_ACTION_REQUIRED",
                "symbol": request["symbol"],
                "quantity": request["quantity"],
                "buy_average_price": request.get(
                    "buyAveragePrice"
                ),
                "gtt_status": gtt_status,
                "message": (
                    "BUY was executed, but the target SELL GTT is not "
                    "ACTIVE. Manual GTT action is required."
                ),
            }

        else:
            return {
                "status": "GTT_PENDING",
                "symbol": request["symbol"],
                "quantity": request["quantity"],
                "gtt_status": gtt_status,
                "message": (
                    "Target SELL GTT has been submitted but is not yet "
                    "confirmed ACTIVE. No second target GTT will be created."
                ),
            }

    # --------------------------------------------------------------
    # STOP_SUBMITTING -> recover or create stop-loss GTT.
    # --------------------------------------------------------------

    if request.get("status") == "STOP_SUBMITTING":
        stop_reference = request.get("stopLossReference")

        if not stop_reference:
            safe_mark_state(
                user_id=user_id,
                expected_status="STOP_SUBMITTING",
                new_status="GTT_FAILED_MANUAL_ACTION_REQUIRED",
                extra={
                    "executionError": (
                        "STOP_SUBMITTING state has no stop-loss reference."
                    ),
                    "executionAt": datetime.now(TZ).isoformat(),
                },
            )

            return {
                "status": "GTT_FAILED_MANUAL_ACTION_REQUIRED",
                "symbol": request["symbol"],
                "quantity": request["quantity"],
                "message": (
                    "Stop-loss state is incomplete. "
                    "Manual action is required."
                ),
            }

        existing_stop = find_gtt_with_retries(
            groww,
            stop_reference,
        )

        if existing_stop:
            stop_order_id = extract_value(
                existing_stop,
                "smart_order_id",
                "smartOrderId",
                "smart_order_internal_id",
                "smartOrderInternalId",
                "id",
            )

            if stop_order_id:
                mark_state(
                    user_id=user_id,
                    expected_status="STOP_SUBMITTING",
                    new_status="STOP_SUBMITTED",
                    extra={
                        "stopLossId": stop_order_id,
                        "stopLossRecoveredAt": datetime.now(TZ).isoformat(),
                    },
                )

                request = load_request(user_id)

            else:
                safe_mark_state(
                    user_id=user_id,
                    expected_status="STOP_SUBMITTING",
                    new_status="GTT_FAILED_MANUAL_ACTION_REQUIRED",
                    extra={
                        "executionError": (
                            "Existing stop-loss GTT reference was found "
                            "but its internal ID could not be resolved."
                        ),
                        "executionAt": datetime.now(TZ).isoformat(),
                    },
                )

                return {
                    "status": "GTT_FAILED_MANUAL_ACTION_REQUIRED",
                    "symbol": request["symbol"],
                    "quantity": request["quantity"],
                    "message": (
                        "Stop-loss GTT was found but could not be "
                        "safely resolved. Manual action is required."
                    ),
                }

        else:
            stop_trigger_price = float(
                request["stopLossPrice"]
            )

            instrument = groww.get_instrument(request["symbol"])

            try:
                tick_size = float(instrument["tick_size"])
            except (KeyError, TypeError, ValueError):
                return {
                    "status": "VALIDATION_FAILED",
                    "symbol": request["symbol"],
                    "quantity": int(request["quantity"]),
                    "message": (
                        f"Could not determine tick size for "
                        f"{request['symbol']}."
                    ),
                }

            if tick_size <= 0:
                return {
                    "status": "VALIDATION_FAILED",
                    "symbol": request["symbol"],
                    "quantity": int(request["quantity"]),
                    "message": (
                        f"Invalid tick size for {request['symbol']}: "
                        f"{tick_size}"
                    ),
                }

            stop_trigger_price = round_down_to_tick(
                stop_trigger_price,
                tick_size,
            )

            stop_limit_price = calculate_stop_loss_limit(
                stop_trigger_price,
                tick_size,
            )

            # Persist the corrected limit price so subsequent recovery
            # attempts use the same value.
            mark_state(
                user_id=user_id,
                expected_status="STOP_SUBMITTING",
                new_status="STOP_SUBMITTING",
                extra={
                    "stopLossLimitPrice": stop_limit_price,
                },
            )

            request = load_request(user_id)

            if not request:
                return {
                    "status": "EXECUTION_UNKNOWN_MANUAL_REVIEW",
                    "symbol": request.get("symbol") if request else None,
                    "quantity": request.get("quantity") if request else None,
                    "message": (
                        "Execution request disappeared during "
                        "stop-loss recovery."
                    ),
                }

            logger.info(
                "Creating SELL STOP GTT: symbol=%s quantity=%s "
                "trigger=%s limit=%s reference=%s",
                request["symbol"],
                request["quantity"],
                stop_trigger_price,
                stop_limit_price,
                stop_reference,
            )

            try:
                stop_response = groww.create_sell_stop_gtt(
                    trading_symbol=request["symbol"],
                    quantity=int(request["quantity"]),
                    trigger_price=stop_trigger_price,
                    stop_price=stop_limit_price,
                    reference_id=stop_reference,
                )

                mark_state(
                    user_id=user_id,
                    expected_status="STOP_SUBMITTING",
                    new_status="STOP_SUBMITTED",
                    extra={
                        "stopLossStatus": "SUBMITTED",
                        "stopLossCreateException": False,
                        "stopLossResponse": stop_response,
                        "stopSubmittedAt": datetime.now(TZ).isoformat(),
                    },
                )

                request = load_request(user_id)

            except Exception as exc:
                error_text = str(exc)
                groww_error_code = getattr(exc, "code", None)
                groww_error_message = getattr(exc, "msg", None)

                logger.error(
                    "Stop-loss GTT failed: type=%s code=%s msg=%s error=%s",
                    type(exc).__name__,
                    groww_error_code,
                    groww_error_message,
                    error_text,
                )
                # Groww uses reference_id as an idempotency key.
                # A duplicate reference means we must NOT create another
                # stop order with a different reference.
                if "Duplicate smart order" in error_text or "duplicate" in error_text.lower():
                    mark_state(
                        user_id=user_id,
                        expected_status="STOP_SUBMITTING",
                        new_status="STOP_PENDING",
                        extra={
                            "stopLossStatus": "PENDING_VERIFICATION",
                            "stopLossCreateException": True,
                            "stopLossDuplicateReference": True,
                            "stopLossDuplicateError": error_text,
                            "stopPendingAt": datetime.now(TZ).isoformat(),
                        },
                    )

                    return {
                        "status": "STOP_PENDING",
                        "executionStatus": "TARGET_GTT_ACTIVE",
                        "symbol": request["symbol"],
                        "quantity": int(request["quantity"]),
                        "message": (
                            "Target GTT is active. Groww reported the stop-loss "
                            "reference as already existing. No duplicate stop "
                            "was created. Manual verification is required."
                        ),
                    }

                mark_state(
                    user_id=user_id,
                    expected_status="STOP_SUBMITTING",
                    new_status="STOP_PENDING",
                    extra={
                        "stopLossStatus": "PENDING_VERIFICATION",
                        "stopLossCreateException": True,
                        "stopLossCreateError": error_text,
                        "growwErrorCode": str(groww_error_code) if groww_error_code else None,
                        "growwErrorMessage": groww_error_message,
                        "stopPendingAt": datetime.now(TZ).isoformat(),
                    },
                )

                return {
                    "status": "STOP_PENDING",
                    "executionStatus": "TARGET_GTT_ACTIVE",
                    "symbol": request["symbol"],
                    "quantity": int(request["quantity"]),
                    "message": (
                        "Stop-loss creation could not be confirmed. "
                        "Automatic retry is blocked."
                    ),
                }

            stop_order_id = extract_value(
                stop_response,
                "smart_order_id",
                "smartOrderId",
                "smart_order_internal_id",
                "smartOrderInternalId",
                "id",
            )

            if not stop_order_id:
                recovered_stop = find_gtt_with_retries(
                    groww,
                    stop_reference,
                )

                stop_order_id = extract_value(
                    recovered_stop,
                    "smart_order_id",
                    "smartOrderId",
                    "smart_order_internal_id",
                    "smartOrderInternalId",
                    "id",
                )

            if not stop_order_id:
                safe_mark_state(
                    user_id=user_id,
                    expected_status="STOP_SUBMITTING",
                    new_status="GTT_FAILED_MANUAL_ACTION_REQUIRED",
                    extra={
                        "executionError": (
                            "Groww stop-loss GTT response did not expose "
                            "a smart-order ID and the saved reference "
                            "could not be resolved."
                        ),
                        "executionAt": datetime.now(TZ).isoformat(),
                        "stopLossResponse": json_safe(stop_response),
                    },
                )

                return {
                    "status": "GTT_FAILED_MANUAL_ACTION_REQUIRED",
                    "symbol": request["symbol"],
                    "quantity": request["quantity"],
                    "message": (
                        "Stop-loss SELL GTT could not be confirmed safely. "
                        "Manual action is required."
                    ),
                }

            mark_state(
                user_id=user_id,
                expected_status="STOP_SUBMITTING",
                new_status="STOP_SUBMITTED",
                extra={
                    "stopLossId": stop_order_id,
                    "stopLossStatus": "SUBMITTED",
                    "stopLossSubmittedAt": datetime.now(TZ).isoformat(),
                },
            )

            request = load_request(user_id)

    # --------------------------------------------------------------
    # STOP_SUBMITTED -> verify stop-loss GTT ACTIVE.
    # --------------------------------------------------------------

    if request.get("status") == "STOP_SUBMITTED":
        stop_order_id = request.get("stopLossId")

        if not stop_order_id:
            safe_mark_state(
                user_id=user_id,
                expected_status="STOP_SUBMITTED",
                new_status="GTT_FAILED_MANUAL_ACTION_REQUIRED",
                extra={
                    "executionError": (
                        "STOP_SUBMITTED state has no stop-loss "
                        "smart-order ID."
                    ),
                    "executionAt": datetime.now(TZ).isoformat(),
                },
            )

            return {
                "status": "GTT_FAILED_MANUAL_ACTION_REQUIRED",
                "symbol": request["symbol"],
                "quantity": request["quantity"],
                "message": (
                    "Stop-loss state is incomplete. "
                    "Manual action is required."
                ),
            }

        stop_order_detail = groww.get_smart_order(
            stop_order_id
        )

        stop_status = get_gtt_status(
            stop_order_detail
        )

        logger.info(
            "Stop-loss GTT result: id=%s status=%s",
            stop_order_id,
            stop_status,
        )

        if stop_status == "ACTIVE":
            mark_state(
                user_id=user_id,
                expected_status="STOP_SUBMITTED",
                new_status="ORDER_PLACED",
                extra={
                    "buyStatus": "EXECUTED",
                    "gttCreated": True,
                    "gttStatus": "ACTIVE",
                    "stopLossStatus": "ACTIVE",
                    "orderPlaced": True,
                    "executionStatus": "PROTECTED_POSITION",
                    "executionAt": datetime.now(TZ).isoformat(),
                },
            )

            request = load_request(user_id)

        elif stop_status in TERMINAL_GTT_FAILURES:
            safe_mark_state(
                user_id=user_id,
                expected_status="STOP_SUBMITTED",
                new_status="GTT_FAILED_MANUAL_ACTION_REQUIRED",
                extra={
                    "stopLossStatus": stop_status,
                    "executionError": (
                        f"Stop-loss GTT entered terminal status "
                        f"{stop_status}. Position is NOT fully protected."
                    ),
                    "orderPlaced": True,
                    "gttCreated": True,
                    "executionAt": datetime.now(TZ).isoformat(),
                },
            )

            return {
                "status": "GTT_FAILED_MANUAL_ACTION_REQUIRED",
                "symbol": request["symbol"],
                "quantity": request["quantity"],
                "buy_average_price": request.get(
                    "buyAveragePrice"
                ),
                "stop_loss_price": request.get(
                    "stopLossPrice"
                ),
                "stop_loss_status": stop_status,
                "message": (
                    "Target GTT is active, but the stop-loss GTT is "
                    "not ACTIVE. Manual action is required."
                ),
            }

        else:
            return {
                "status": "STOP_PENDING",
                "symbol": request["symbol"],
                "quantity": request["quantity"],
                "stop_loss_status": stop_status,
                "message": (
                    "Stop-loss GTT has been submitted but is not yet "
                    "confirmed ACTIVE. No second stop-loss GTT will be created."
                ),
            }

    # --------------------------------------------------------------
    # ORDER_PLACED -> both target and stop are active.
    # --------------------------------------------------------------

    if request.get("status") == "ORDER_PLACED":
        return {
            "status": "ORDER_PLACED",
            "symbol": request["symbol"],
            "quantity": int(request["quantity"]),
            "coach_entry_price": float(
                request["coachEntryPrice"]
            ),
            "profit_percent": float(
                get_parameter(
                    os.environ.get(
                        "PROFIT_PARAMETER_NAME",
                        "/coach-trading/profit-percent",
                    )
                )
            ),
            "gtt_target_price": float(
                request["gttTargetPrice"]
            ),
            "buy_order_id": request.get("buyOrderId"),
            "buy_average_price": request.get("buyAveragePrice"),
            "gtt_id": request.get("gttId"),
            "gtt_status": request.get("gttStatus"),
            "stop_loss_percent": request.get("stopLossPercent"),
            "stop_loss_price": request.get("stopLossPrice"),
            "stop_loss_limit_price": request.get(
                "stopLossLimitPrice"
            ),
            "stop_loss_reference": request.get(
                "stopLossReference"
            ),
            "stop_loss_id": request.get("stopLossId"),
            "stop_loss_status": request.get(
                "stopLossStatus"
            ),
            "execution_status": request.get(
                "executionStatus",
                "PROTECTED_POSITION",
            ),
            "order_placed": True,
            "gtt_created": True,
        }

    return {
        "status": request.get("status"),
        "symbol": request.get("symbol"),
        "quantity": request.get("quantity"),
        "message": "Execution state requires no additional action.",
    }


def execute_request(*, user_id, request):
    status = request.get("status")

    groww_credentials = get_secret(
        os.environ["GROWW_SECRET_ARN"]
    )

    groww = GrowwClient(groww_credentials)

    if status == "CONFIRMED_BUT_NOT_EXECUTED":
        result = execute_new_buy(
            user_id=user_id,
            request=request,
        )

        if isinstance(result, tuple):
            result = result[1]


        if result["status"] == "DRY_RUN":
            return result

        if result["status"] in {
            "INSUFFICIENT_FUNDS",
            "ALREADY_PROCESSING",
        }:
            return result

        current = load_request(user_id)

        if not current:
            return {
                "status": "EXECUTION_UNKNOWN_MANUAL_REVIEW",
                "message": (
                    "Execution request disappeared from state."
                ),
            }

        return continue_buy_and_gtt(
            user_id=user_id,
            request=current,
            groww=groww,
        )

    if status == "BUY_SUBMITTING":
        recovered = recover_buy_submission(
            user_id=user_id,
            request=request,
            groww=groww,
        )

        if isinstance(recovered, tuple):
            return recovered[1]

        return recovered
    
    if status in {
        "BUY_SUBMITTED",
        "BUY_EXECUTED",
        "GTT_SUBMITTING",
        "GTT_SUBMITTED",
        "STOP_SUBMITTED",
    }:
        return continue_buy_and_gtt(
            user_id=user_id,
            request=request,
            groww=groww,
        )

    if status == "ORDER_PLACED":
        return {
            "status": "ALREADY_PROCESSED",
            "symbol": request["symbol"],
            "quantity": request["quantity"],
            "message": "BUY request has already been completed.",
        }


    if status in {
        "BUY_REJECTED",
        "GTT_FAILED_MANUAL_ACTION_REQUIRED",
        "EXECUTION_UNKNOWN_MANUAL_REVIEW",
        "INSUFFICIENT_FUNDS",
        "VALIDATION_FAILED",
    }:
        return {
            "status": status,
            "symbol": request.get("symbol"),
            "quantity": request.get("quantity"),
            "message": request.get(
                "executionError",
                "This BUY request is in a terminal state.",
            ),
        }

    if status == "DRY_RUN_EXECUTED":
        return {
            "status": "ALREADY_PROCESSED",
            "symbol": request["symbol"],
            "quantity": request["quantity"],
            "message": "BUY request has already been processed.",
        }

    return {
        "status": "INVALID_STATE",
        "message": (
            f"Unsupported BUY execution state: {status}"
        ),
    }


def lambda_handler(event, context):
    user_id = str(
        event.get("telegram_user_id", "")
    )

    if not user_id:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "status": "validation_error",
                "message": "telegram_user_id is required",
            }),
        }

    try:
        request = load_request(user_id)

        if not request:
            return {
                "statusCode": 404,
                "body": json.dumps({
                    "status": "error",
                    "message": "No pending BUY request",
                }),
            }

        status = request.get("status")

        if status == "PENDING_CONFIRMATION":
            return {
                "statusCode": 409,
                "body": json.dumps({
                    "status": "error",
                    "message": "BUY request has not been confirmed",
                }),
            }

        result = execute_request(
            user_id=user_id,
            request=request,
        )

        status = result.get("status")

        if status == "DRY_RUN":
            mark_state(
                user_id=user_id,
                expected_status="CONFIRMED_BUT_NOT_EXECUTED",
                new_status="DRY_RUN_EXECUTED",
                extra={
                    "executionAt": datetime.now(TZ).isoformat(),
                    "executionStatus": "DRY_RUN",
                    "orderPlaced": False,
                    "gttCreated": False,
                    "gttTargetPrice": decimal_value(
                        result["gtt_target_price"]
                    ),
                    "currentLtp": decimal_value(
                        result["current_ltp"]
                    ),
                    "marketOpen": result["market_open"],
                },
            )

            return {
                "statusCode": 200,
                "body": json.dumps(
                    result,
                    default=json_safe,
                ),
            }

        if status in {
            "INSUFFICIENT_FUNDS",
            "VALIDATION_FAILED",
            "BUY_REJECTED",
            "GTT_FAILED_MANUAL_ACTION_REQUIRED",
            "EXECUTION_UNKNOWN_MANUAL_REVIEW",
        }:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    result,
                    default=json_safe,
                ),
            }

        if status in {
            "BUY_PENDING",
            "GTT_PENDING",
            "STOP_PENDING",
            "ALREADY_PROCESSING",
        }:
            return {
                "statusCode": 202,
                "body": json.dumps(
                    result,
                    default=json_safe,
                ),
            }

        if status == "ORDER_PLACED":
            return {
                "statusCode": 200,
                "body": json.dumps(
                    result,
                    default=json_safe,
                ),
            }

        if status == "ALREADY_PROCESSED":
            return {
                "statusCode": 409,
                "body": json.dumps(
                    result,
                    default=json_safe,
                ),
            }

        return {
            "statusCode": 409,
            "body": json.dumps(
                result,
                default=json_safe,
            ),
        }

    except ClientError as exc:
        if exc.response["Error"]["Code"] == (
            "ConditionalCheckFailedException"
        ):
            logger.warning(
                "Execution state changed concurrently for user %s",
                user_id,
            )

            return {
                "statusCode": 409,
                "body": json.dumps({
                    "status": "already_processing",
                    "message": (
                        "BUY request is already being processed."
                    ),
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

        # Don't leave a confirmed request looking executable when
        # validation itself has failed.
        safe_mark_state(
            user_id=user_id,
            expected_status="CONFIRMED_BUT_NOT_EXECUTED",
            new_status="VALIDATION_FAILED",
            extra={
                "executionError": str(exc),
                "executionAt": datetime.now(TZ).isoformat(),
                "orderPlaced": False,
                "gttCreated": False,
            },
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
