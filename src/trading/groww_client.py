import os
import pyotp

from growwapi import GrowwAPI
from growwapi.groww.exceptions import GrowwAPIException


class GrowwClient:
    def __init__(self, credentials=None):
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
