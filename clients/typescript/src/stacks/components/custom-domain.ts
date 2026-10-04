/**
 * Componente `custom-domain` (m15-custom-domain, ADR-024):
 * `infra/custom-domain.yaml` — una distribución CloudFront con alias
 * comodín, su CloudFront Function de enrutado (`cf.updateRequestOrigin`) y
 * un KeyValueStore. Ver `../../custom-domain/service.ts` (`CustomDomain`)
 * para la fachada que despliega esta pila.
 *
 * El refresher Lambda opcional que describía la arquitectura de M15 queda
 * como seguimiento no bloqueante (ver la contraparte Python de este
 * fichero para el razonamiento); `CustomDomain.refresh()` cubre el mismo
 * caso desde el lado del SDK.
 */

import type { StackComponent } from "../model.js";

export const COMPONENT: StackComponent = {
  name: "custom-domain",
  description:
    "Distribución CloudFront con alias comodín, CloudFront Function de enrutado y " +
    "KeyValueStore, para domain/CustomDomain.",
  parameters: [
    {
      name: "PublicDomain",
      description:
        "Tu dominio; el alias de la distribución es *.<PublicDomain> salvo " +
        "AlternateDomainNames.",
      required: true,
    },
    {
      name: "CertificateArn",
      description:
        "ARN de un certificado ACM en us-east-1 que cubra los nombres de la distribución " +
        "(*.<PublicDomain> por defecto).",
      required: true,
    },
    {
      name: "AlternateDomainNames",
      description:
        "Opcional: nombres explícitos <etiqueta>.<PublicDomain>, separados por comas, en " +
        "lugar del comodín (p. ej. si otra distribución ya tiene *.<PublicDomain>).",
    },
  ],
  cost: {
    creates: [
      "AWS::CloudFront::Distribution",
      "AWS::CloudFront::Function",
      "AWS::CloudFront::KeyValueStore",
      "AWS::CloudFront::KeyValueStoreAssociation (en la Function)",
    ],
    idleMonthly: "~$0 en reposo (CloudFront sin tráfico no factura; el KVS tampoco)",
    // Mismas líneas que la contraparte Python (tres conceptos que AWS
    // factura por separado, no una cifra combinada).
    perUse: [
      "CloudFront: ~$0,085/GB + $0,0075/10 000 peticiones HTTPS (salida, us-east-1)",
      "CloudFront Functions: ~$0,10 por 1 000 000 de invocaciones (una por petición, " +
        "cifra de lista desde su lanzamiento, no reconfirmada en vivo esta sesión)",
      "KeyValueStore, lectura (la que hace la Function en cada petición): " +
        "~$0,50 por 1 000 000 de lecturas",
      "KeyValueStore, llamada de gestión (PutKey/DeleteKey de " +
        "register()/unregister()/refresh()): ~$5 por 1 000 000 de llamadas",
    ],
    removal:
      "destroy() borra la distribución (tarda ~15 min en deshabilitarse primero), la " +
      "Function y el KVS; ninguna ruta sobrevive",
    source:
      "AWS_API_NOTES.md §29 (verificado contra botocore 1.43.103, 2026-09-30); cifras de " +
      "lanzamiento de CloudFront Functions/KeyValueStore, reconfirmar contra " +
      "https://aws.amazon.com/cloudfront/pricing/ en la etapa de aceptación AWS",
  },
};
