"""La cáscara de la pila `dashboard` (ADR-026, fase 2): una Lambda con
Function URL que sirve la misma SPA y la misma API `/api/v1` que
`rayito dashboard`, con autenticación de Cognito, un rol de lectura y otro de
acciones, y un registro de auditoría append-only en DynamoDB. Los servicios
(`Fleet`, `Quotas`, `UsageCost`) y el contrato son los del SDK, que
`scripts/gen_stack_assets.py` copia al zip; aquí sólo vive lo propio de la
cáscara.
"""
