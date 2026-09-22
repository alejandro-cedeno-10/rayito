"""`rayito`, la herramienta de línea de comandos del SDK (extra `rayito[cli]`).

Este paquete no importa nada: `rayito.cli.app` es lo único que necesita
`typer`, y `_artifact`, `_publish`, `_prune`, `_logs`, `_checks`, `_compat`,
`_session` y `_console` se importan sin el extra (los shims de `scripts/` y
los tests de la lógica los usan así).
"""
