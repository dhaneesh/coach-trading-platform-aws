# DynamoDB schema

Table: `coach-trading`
Primary key: `PK`, `SK`.

Signal: `PK=SIGNAL#<monthly-file>`, `SK=<signal-id>`
Pending BUY: `PK=USER#<telegram-user-id>`, `SK=PENDING_BUY`
Trade: `PK=TRADE#<trade-id>`, `SK=METADATA`
Telegram idempotency: `PK=TELEGRAM_UPDATE#<update-id>`, `SK=PROCESSED`

Use conditional writes for idempotency before any real order. Never blindly retry a BUY after an ambiguous network failure.
