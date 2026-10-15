"""La API JSON versionada del panel de gestión (`/api/v1`, ADR-026): un
núcleo sin transporte (`DashboardApi.handle`) sobre los servicios `Fleet`,
`Quotas` y `UsageCost`. La cáscara local (`rayito dashboard`) y, en la fase
2, el handler Lambda lo envuelven y pasan los mismos tests de contrato."""
