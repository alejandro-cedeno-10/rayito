# Excepciones

Todas heredan de `SandboxException`, salvo las que dicen algo de tu entorno
y no del sandbox (`AuthenticationException` y su subclase
`GitAuthException`, `QuotaExceededException`, `CapacityException`) y
`UnimplementedError` (con su subclase `LifecycleUnsupportedException`), que
es un `NotImplementedError`. `RayitoCompatWarning` es un `UserWarning`, no
una excepción: avisa de una opción de E2B que Rayito ignora.
Qué hacer con cada una: [Errores](../errores.md).

::: rayito.exceptions
