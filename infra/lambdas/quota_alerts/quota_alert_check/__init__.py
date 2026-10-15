"""La comprobación programada de cuotas (ADR-026, fase 3): una Lambda que
EventBridge invoca con un `rate(...)`, mide la memoria asignada contra la
cuota de la región con el servicio `Quotas` del SDK (que `scripts/gen_stack_assets.py`
copia al zip) y publica en SNS cuando el uso cruza un umbral. Sólo lecturas de
AWS más tres escrituras propias: SNS, una métrica y una fila de estado.
"""
