# Coach Trading Platform — AWS Serverless Migration

Serverless redesign of the proven coach-sheet monitoring and Telegram/Groww workflow.

## Target architecture
- EventBridge Scheduler -> monitor Lambda every 15 minutes.
- EventBridge Scheduler -> summary Lambda at 15:35 IST weekdays.
- Telegram webhook -> API Gateway HTTP API -> Telegram Lambda.
- DynamoDB stores signal, notification, pending-confirmation, idempotency and trade state.
- Groww execution is isolated to the trading executor.
- Secrets live in AWS Secrets Manager.

The existing Hostinger deployment must remain production until AWS passes parallel and controlled live tests.
