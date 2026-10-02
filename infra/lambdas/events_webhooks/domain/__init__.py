"""Pure domain of the `events-webhooks` Lambdas (m15-events-webhooks):
nothing here imports `boto3` or does network/file I/O. The three handlers
(`forwarder`, `deliverer`, `reconciler`) are the only adapters that touch
AWS; everything they decide comes from this package.
"""
