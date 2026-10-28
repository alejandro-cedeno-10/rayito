# Checklist de revisión

Se aplica a toda revisión de un PR, sea de una persona o de un agente, y
siempre antes de fusionar a `main`. Un hallazgo es **bloqueante** si filtra
un dato o rompe el comportamiento; **mayor** si incumple una regla de esta
skill; **menor** si puede ir en un PR posterior. Solo se informan problemas
reales.

## 1. Barrido seguro del diff

Va primero: si algo se filtró, la revisión para ahí y avisa al mantenedor
(no se reescribe la historia por cuenta propia).

- `python3 scripts/check_hygiene.py`, con la lista privada en
  `.git/info/hygiene-denylist`.
- `gitleaks` sobre los commits del PR, no solo sobre el último: un dato
  borrado en un commit posterior sigue en la historia.
- Las líneas añadidas (`git diff origin/main...HEAD`), buscadas a mano:
  - números de 12 dígitos y ARNs con cuenta;
  - IDs `vpc-`, `subnet-` y `sg-`, y claves `AKIA`/`ASIA`;
  - portales SSO, perfiles y rutas locales absolutas;
  - correos que no sean noreply, tokens o JWT;
  - nombres de empresa o de dominio.
- Cada imagen añadida se abre y se mira: no debe verse ninguna cuenta,
  nombre, correo, ID de recurso ni URL interna.
- El título y el cuerpo del PR pasan por `python3 scripts/check_hygiene.py -`.
- Nada de borradores (mensajes, comentarios, correos) en ficheros
  versionados.

## 2. Arquitectura: hexagonal y DDD

- El dominio es puro: no importa `boto3`/`botocore`, `@aws-sdk/*`, `grpc`,
  `tonic` ni el código generado del proto.
- Los puertos son `Protocol` (Python), `interface` (TypeScript) o `trait`
  (Rust). Los adaptadores traducen los errores técnicos a excepciones
  propias.
- Las dependencias llegan por constructor, y los tests usan fakes.
- El lenguaje del dominio es el de `SPEC.md` y `ARCHITECTURE.md`: sandbox,
  template, pool, snapshot, etc. Un mismo concepto no tiene dos nombres.
- Las invariantes se validan en el modelo del dominio (value objects,
  constructores), no repartidas por los adaptadores.
- Nada especulativo: no hay módulos, clases ni excepciones "para más
  adelante".

## 3. Código limpio

- No hay valores mágicos: cada límite, TTL, TPS, puerto, tamaño o umbral es
  una constante con nombre. Si es un límite, va en `limits.json` y
  `gen_limits.py`.
- No hay duplicación entre sync y async, ni entre módulos: lo compartido
  vive en `_*_base.py` o en un helper.
- Las funciones son cortas y con un solo propósito. No hay comentarios en
  línea: el código se extrae a un helper con nombre o se explica en el
  docstring/TSDoc.
- **Todo el código va en inglés**: identificadores, clases, funciones,
  variables, constantes, nombres de fichero y de carpeta del código y de los
  tests. Docstrings, TSDoc, comentarios y mensajes al usuario van en
  español; la documentación de `docs/site`, en español e inglés (cada
  `pagina.md` con su `pagina.en.md`, sincronizadas).
- Los tests se nombran por el comportamiento, no por el hito.

## 4. Paridad y compatibilidad

- [parity-checklist.md](parity-checklist.md) completa: Python sync y async,
  TypeScript y el shim E2B.
- `scripts/check_sdk_parity.py` pasa. Una excepción nueva lleva su motivo.
- Retrocompatibilidad: nada público cambia sin `feat!:` o sin un aviso de
  obsolescencia previo.
- Las regresiones tienen test, y los tests existentes no se han debilitado
  (ni skips nuevos sin motivo ni asserts relajados).

## 5. Coste y seguridad

- Lo que cuesta va apagado por defecto, se activa desde el SDK y lleva la
  caja "Coste y activación".
- IAM con el mínimo privilegio y limitado a los recursos de Rayito.
- Los logs solo llevan ids, estados, duraciones, bytes y códigos, nunca
  secretos ni contenido.

## 6. Gates y docs

- Pasan los gates de [gates.md](gates.md) del lenguaje tocado, y el CI está
  en verde.
- Hay entrada en el CHANGELOG del paquete tocado.
- Un e2e nuevo cumple la viñeta de e2e de «Reglas para cualquier ejecución en
  AWS» de [release-and-aws.md](release-and-aws.md) (etiquetas, limpieza solo de
  lo propio, servidores sin timeout, scripts por la API de ficheros).
- Las docs están en español e inglés (`pagina.md` y `pagina.en.md`, mismos
  ejemplos), con pestañas Python/TypeScript, `check_docs_i18n.py` y
  `mkdocs --strict` pasan.
