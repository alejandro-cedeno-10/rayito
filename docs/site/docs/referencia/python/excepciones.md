# Excepciones

Todas heredan de `SandboxException`, salvo las que dicen algo de tu entorno
y no del sandbox (`AuthenticationException`, `QuotaExceededException`,
`CapacityException`) y `UnimplementedError`, que es un `NotImplementedError`.
Qué hacer con cada una: [Errores](../errores.md).

::: rayito.exceptions
