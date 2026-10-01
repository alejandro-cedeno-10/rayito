"""Componente `custom-domain` (m15-custom-domain, ADR-024):
`infra/custom-domain.yaml` — una distribución CloudFront con alias
comodín, su CloudFront Function de enrutado (`cf.updateRequestOrigin`) y
un KeyValueStore. Ver `_custom_domain/_service.py` (`CustomDomain`) para la
fachada que despliega esta pila.

El refresher Lambda opcional que describe la arquitectura de M15 (§7.8,
`EnableRefresher`) queda como seguimiento no bloqueante: necesitaría
empaquetar y subir un artefacto en cada `deploy()` (el mecanismo genérico
de `OptionalStacks` no soporta un artefacto condicional) sólo para una
Lambda que, sin D3 (dominio y certificado del mantenedor), tampoco se
puede probar contra AWS real en este cambio. Mientras tanto,
`CustomDomain.refresh()` cubre el mismo caso desde el lado del SDK: el
llamante lo invoca antes de que caduque el JWE de una ruta (DOM-7)."""

from __future__ import annotations

from rayito._stacks._model import CostStatement, StackComponent, StackParameter

COMPONENT: StackComponent = StackComponent(
    name="custom-domain",
    description=(
        "Distribución CloudFront con alias comodín, CloudFront Function de enrutado y "
        "KeyValueStore, para domain=/CustomDomain."
    ),
    parameters=(
        StackParameter(
            "PublicDomain",
            "Dominio del mantenedor (D3); el alias de la distribución es *.<PublicDomain>.",
            required=True,
        ),
        StackParameter(
            "CertificateArn",
            "ARN de un certificado ACM en us-east-1 que cubra *.<PublicDomain> (D3).",
            required=True,
        ),
    ),
    cost=CostStatement(
        creates=(
            "AWS::CloudFront::Distribution",
            "AWS::CloudFront::Function",
            "AWS::CloudFront::KeyValueStore",
            "AWS::CloudFront::KeyValueStoreAssociation (en la Function)",
        ),
        idle_monthly="~$0 en reposo (CloudFront sin tráfico no factura; el KVS tampoco)",
        per_use=(
            "CloudFront: ~$0,085/GB + $0,0075/10 000 peticiones HTTPS (salida, us-east-1)",
            "KeyValueStore: $0,0000004 por lectura/escritura "
            "(PutKey/DeleteKey/consulta de la Function)",
        ),
        removal=(
            "destroy() borra la distribución (tarda ~15 min en deshabilitarse primero), la "
            "Function y el KVS; ninguna ruta sobrevive"
        ),
        source="AWS_API_NOTES.md §29 (verificado contra botocore 1.43.103, 2026-09-30)",
    ),
)
