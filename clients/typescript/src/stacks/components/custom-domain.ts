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
      description: "Dominio del mantenedor (D3); el alias de la distribución es *.<PublicDomain>.",
      required: true,
    },
    {
      name: "CertificateArn",
      description: "ARN de un certificado ACM en us-east-1 que cubra *.<PublicDomain> (D3).",
      required: true,
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
    perUse: [
      "CloudFront: ~$0,085/GB + $0,0075/10 000 peticiones HTTPS (salida, us-east-1)",
      "KeyValueStore: $0,0000004 por lectura/escritura (PutKey/DeleteKey/consulta de la Function)",
    ],
    removal:
      "destroy() borra la distribución (tarda ~15 min en deshabilitarse primero), la Function y el KVS",
    source: "AWS_API_NOTES.md §29 (verificado contra botocore 1.43.103, 2026-09-30)",
  },
};
