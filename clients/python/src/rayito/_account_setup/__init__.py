"""Preparación de una cuenta y región de AWS para Rayito (`rayito setup`,
ADR-030): dominio puro (`_domain`), puertos (`_ports`), servicio de
aplicación (`_service`) y adaptadores (`_aws`, `_release`).

Privado: nada se exporta desde `rayito`. Este paquete no importa nada a
propósito, para que `_agent/_aws_resolve.py` pueda importar `_domain` sin
ciclos.
"""
