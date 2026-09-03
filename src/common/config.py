import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    table_name: str
    authorized_telegram_user_id: str
    profit_parameter_name: str

def settings() -> Settings:
    return Settings(
        table_name=os.environ["DYNAMODB_TABLE"],
        authorized_telegram_user_id=os.environ["TELEGRAM_AUTHORIZED_USER_ID"],
        profit_parameter_name=os.environ.get("PROFIT_PARAMETER_NAME", "/coach-trading/profit-percent"),
    )
