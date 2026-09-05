import os
import pyotp
import growwapi.groww.client as groww_client_module

from growwapi import GrowwAPI
from growwapi.groww.exceptions import GrowwAPIException


class GrowwClient:
    def __init__(self, credentials=None):
        self._configure_instrument_cache()

        credentials = credentials or {}

        self.totp_token = (
            credentials.get("totp_token")
            or os.environ["GROWW_TOTP_TOKEN"]
        )
        self.totp_secret = (
            credentials.get("totp_secret")
            or os.environ["GROWW_TOTP_SECRET"]
        )

        self.client = None
        self.authenticate()

    @staticmethod
    def _configure_instrument_cache():
        cache_dir = "/tmp/groww/common"
        os.makedirs(cache_dir, exist_ok=True)

        if getattr(groww_client_module, "_coach_original_get_cwd", None) is None:
            groww_client_module._coach_original_get_cwd = groww_client_module.get_cwd
            groww_client_module.get_cwd = (
                lambda file=__file__: cache_dir
            )

    def authenticate(self):
        token = GrowwAPI.get_access_token(
            api_key=self.totp_token,
            totp=pyotp.TOTP(self.totp_secret).now(),
        )
        self.client = GrowwAPI(token)

    @staticmethod
    def is_auth_error(exc):
        code = str(getattr(exc, "code", "")).strip().upper()
        msg = str(getattr(exc, "msg", "")).strip().lower()

        return (
            code
            in {
                "UNAUTHORIZED",
                "AUTHENTICATION_FAILED",
                "INVALID_TOKEN",
                "TOKEN_EXPIRED",
                "TOKEN_INVALID",
                "401",
            }
            or any(
                x in msg
                for x in (
                    "authentication failed",
                    "api token",
                    "invalid token",
                    "token has expired",
                    "unauthorized",
                    "access token",
                )
            )
        )

    def call_with_reauth(self, operation):
        try:
            return operation()
        except GrowwAPIException as exc:
            if not self.is_auth_error(exc):
                raise

            self.authenticate()
            return operation()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_profile(self):
        return self.call_with_reauth(
            lambda: self.client.get_user_profile()
        )

    # ------------------------------------------------------------------
    # Instrument / market data
    # ------------------------------------------------------------------

    def get_instrument(self, trading_symbol):
        return self.call_with_reauth(
            lambda: self.client.get_instrument_by_exchange_and_trading_symbol(
                exchange="NSE",
                trading_symbol=trading_symbol,
            )
        )

    def get_ltp(self, trading_symbol):
        response = self.call_with_reauth(
            lambda: self.client.get_ltp(
                exchange_trading_symbols=(f"NSE_{trading_symbol}",),
                segment=self.client.SEGMENT_CASH,
            )
        )

        return response[f"NSE_{trading_symbol}"]

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_market_buy(
        self,
        *,
        trading_symbol,
        quantity,
        order_reference_id,
    ):
        return self.call_with_reauth(
            lambda: self.client.place_order(
                validity=self.client.VALIDITY_DAY,
                exchange="NSE",
                order_type=self.client.ORDER_TYPE_MARKET,
                product="CNC",
                quantity=quantity,
                segment=self.client.SEGMENT_CASH,
                trading_symbol=trading_symbol,
                transaction_type="BUY",
                order_reference_id=order_reference_id,
                price=0.0,
            )
        )

    def get_order_detail(self, groww_order_id):
        return self.call_with_reauth(
            lambda: self.client.get_order_detail(
                segment=self.client.SEGMENT_CASH,
                groww_order_id=groww_order_id,
            )
        )

    def get_order_status_by_reference(self, order_reference_id):
        return self.call_with_reauth(
            lambda: self.client.get_order_status_by_reference(
                segment=self.client.SEGMENT_CASH,
                order_reference_id=order_reference_id,
            )
        )

    # ------------------------------------------------------------------
    # GTT
    # ------------------------------------------------------------------

    def create_sell_gtt(
        self,
        *,
        trading_symbol,
        quantity,
        trigger_price,
        reference_id,
    ):
        return self.call_with_reauth(
            lambda: self.client.create_smart_order(
                smart_order_type="GTT",
                segment=self.client.SEGMENT_CASH,
                trading_symbol=trading_symbol,
                quantity=quantity,
                product_type="CNC",
                exchange="NSE",
                duration=self.client.VALIDITY_DAY,
                reference_id=reference_id,
                trigger_price=str(trigger_price),
                trigger_direction="UP",
                order={
                    "order_type": self.client.ORDER_TYPE_LIMIT,
                    "price": float(trigger_price),
                    "transaction_type": "SELL",
                },
            )
        )

    def get_smart_order(self, smart_order_id):
        return self.call_with_reauth(
            lambda: self.client.get_smart_order(
                segment=self.client.SEGMENT_CASH,
                smart_order_type="GTT",
                smart_order_id=smart_order_id,
            )
        )

    def get_smart_order_list(
        self,
        *,
        status=None,
        page=0,
        page_size=50,
    ):
        return self.call_with_reauth(
            lambda: self.client.get_smart_order_list(
                smart_order_type="GTT",
                segment=self.client.SEGMENT_CASH,
                status=status,
                page=page,
                page_size=page_size,
            )
        )

    def find_gtt_by_reference(self, reference_id):
        for page in range(10):
            response = self.get_smart_order_list(
                page=page,
                page_size=50,
            )

            orders = response.get("orders", [])

            for order in orders:
                candidate = (
                    order.get("reference_id")
                    or order.get("referenceId")
                    or order.get("smart_order_reference_id")
                    or order.get("smartOrderReferenceId")
                )

                if candidate == reference_id:
                    return order

            if len(orders) < 50:
                break

        return None

    def get_available_margin_details(self):
        return self.call_with_reauth(
            lambda: self.client.get_available_margin_details()
        )

    def get_order_margin_details(self, orders):
        return self.call_with_reauth(
            lambda: self.client.get_order_margin_details(
                segment=self.client.SEGMENT_CASH,
                orders=orders,
            )
        )

    def check_cnc_funds(
        self,
        *,
        trading_symbol,
        quantity,
        order_type,
        price,
    ):
        available = self.get_available_margin_details()

        equity = available.get("equity_margin_details", {})
        cnc_available = float(
            equity.get("cnc_balance_available", 0.0)
        )

        margin_response = self.get_order_margin_details(
            [
                {
                    "trading_symbol": trading_symbol,
                    "transaction_type": "BUY",
                    "quantity": quantity,
                    "price": price,
                    "order_type": order_type,
                    "product": "CNC",
                    "exchange": "NSE",
                }
            ]
        )

        total_requirement = float(
            margin_response.get("total_requirement", 0.0)
        )

        return {
            "cnc_balance_available": cnc_available,
            "total_requirement": total_requirement,
            "sufficient": cnc_available >= total_requirement,
            "margin_details": margin_response,
        }
