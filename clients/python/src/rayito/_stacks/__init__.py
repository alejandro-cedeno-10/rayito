"""Convenio `OptionalStack` (M15 foundations, ADR-016): componentes de
infraestructura opcional en la cuenta del cliente, desplegados sólo con una
llamada explícita del SDK o de `rayito stack` — nunca automáticamente.
`_model.py` es el dominio puro, `_port.py` el puerto, `_cloudformation.py`
el adaptador boto3, `_service.py`/`_service_async.py` el servicio
`OptionalStacks`, `_registry.py` el catálogo y `components/` la definición
de cada uno.
"""
