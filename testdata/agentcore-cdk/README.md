# testdata/agentcore-cdk

Contrato compartido de los constructos CDK de Rayito (`agentcore-cdk`,
ADR-032): `RayitoAgent` (un agente en Amazon Bedrock AgentCore Runtime) y
`RayitoMicrovmImage` (una imagen de Lambda MicroVMs con `rayd`). Python
(`rayito.cdk`) y TypeScript (`rayito/cdk`) son dos implementaciones nativas;
este directorio es lo que las mantiene iguales. Ningún SDK es la referencia:
los ficheros de aquí lo son.

## Ficheros

| Fichero | Qué fija |
|---|---|
| `contract.json` | Superficie pública de los dos SDK (nombres, props, atributos, `propNames` camelCase → snake_case), ids de constructo (de ellos salen los logical ids), marcas de propiedad y etiquetas, errores con sus mensajes, rechazos de la CLI, reglas de normalización y formato de los casos |
| `cases/<caso>/input.json` | Entrada de un caso: constructo, entorno del stack, id, props, proyecto o artefacto, y lo esperado (`"template"` o un error) |
| `cases/<caso>/expected.template.json` | Plantilla sintetizada y normalizada (`{"root", "nested"}`); la genera `scripts/gen_cdk_vectors.py` y la comprueban los dos SDK |
| `fixtures/image-*/` | Árboles mínimos de artefacto de imagen (un `Dockerfile` y, en `image-slim`, el marcador de la variante); nunca se construyen |

## Cómo se usa un caso

1. Crear una `App` con el contexto de `contract.caseFormat.appContext` y un
   `Stack` llamado `RayitoCdkCase` con `env` = `stack` del caso (sin `env` si
   `region` es `null`).
2. `RayitoAgent`: `project` es una ruta relativa a `testdata/`; `build` es
   `"codeBuild"`, `{"codeBuild": {"timeoutMinutes": N}}` o `"localDocker"`.
   `RayitoMicrovmImage`: comprimir `fixtures/<dir>` con la regla de
   `caseFormat.fields.artifact` y pasarlo a `MicrovmImageArtifact.fromFile`
   (con `sha256` si el caso lo trae).
3. Traducir las props con `sdk.propNames` en Python.
4. Si `expected` es `"template"`: sintetizar, normalizar la plantilla raíz y
   cada nested stack con las reglas de abajo y comparar con
   `expected.template.json`. Si es `{"error": …}`: el constructor lanza la
   excepción nombrada con ese `code` y esos campos, y ningún recurso queda
   en el árbol.

La síntesis no llama a AWS: la cuenta es la ficticia `123456789012` y nada
usa `fromLookup`.

## Normalización

En este orden (referencia: `normalise_template` de
`scripts/gen_cdk_vectors.py`; el test de TypeScript la reimplementa y un
caso de prueba de cada SDK comprueba que coinciden):

1. Quitar `Resources.CDKMetadata`, `Conditions.CDKMetadataAvailable`,
   `Parameters.BootstrapVersion` y `Rules.CheckBootstrapVersion` (y la
   sección si queda vacía).
2. Quitar `Metadata` de cada recurso.
3. Cada secuencia de 64 hex en minúsculas → `<ASSET_HASH>`.
4. Cada etiqueta de imagen por contenido `src-<16 hex>` → `src-<SOURCE_HASH>`.
   Hashea el contexto de build, que incluye los ficheros del host del agente
   del SDK: sin esta regla, cualquier cambio del host rompería los goldens. La
   paridad de la etiqueta entre los dos SDK la cubren los vectores de
   `testdata/agentcore-deploy/`.
5. `hnb659fds` → `<QUALIFIER>`.
6. El valor de `rayito:sdk-version` (lista `Key`/`Value` o mapa) →
   `<SDK_VERSION>`.
7. JSON con claves ordenadas, sangría de 2, sin escapar no-ASCII y salto de
   línea final.

## Regenerar

```bash
uv run --project clients/python python scripts/gen_cdk_vectors.py          # escribe expected.template.json
uv run --project clients/python python scripts/gen_cdk_vectors.py --check  # sale 1 si algo deriva
```

El arnés de Python es `clients/python/tests/cdk_contract_harness.py`
(`synthesize_case`). Las plantillas dependen de la versión de `aws-cdk-lib`
fijada en `clients/python/uv.lock` (de ella salen las funciones del
`Provider`): TypeScript fija la misma en su lockfile.
