# Exceptions

All of them inherit from `SandboxException`, except those that say something
about your environment rather than the sandbox (`AuthenticationException` and
its subclass `GitAuthException`, `QuotaExceededException`,
`CapacityException`), `AgentCoreException` (the AgentCore deploy) and
`UnimplementedError` (with its subclass
`LifecycleUnsupportedException`), which is a `NotImplementedError`.
`RayitoCompatWarning` is a `UserWarning`, not an exception: it warns about an
E2B option that Rayito ignores.
What to do about each one: [Errors](../errores.md).

::: rayito.exceptions
