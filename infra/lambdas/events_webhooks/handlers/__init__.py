"""The three Lambda entry points `infra/events-webhooks.yaml` deploys:
`forwarder` (CloudWatch Logs subscription), `deliverer` (DynamoDB Streams on
the events table) and `reconciler` (EventBridge Scheduler, every
`ReconcilerIntervalMinutes`).
Each handler only wires its port implementations together and calls into
`domain/`; the actual decisions live there, not here.
"""
