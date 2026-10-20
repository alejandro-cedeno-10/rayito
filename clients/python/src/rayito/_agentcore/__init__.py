"""Despliegue de agentes en Amazon Bedrock AgentCore Runtime (ADR-031):
`_domain.py` es el dominio puro (nombres, validación, plan, decisiones),
`_models.py` los valores, `_ports.py` los puertos, `_service.py` y
`_service_async.py` la aplicación, `_aws.py` y `_stack.py` los adaptadores y
`_fleet.py` y `_fleet_async.py` la superficie pública. Importar este paquete no
carga ningún cliente de AWS.
"""
