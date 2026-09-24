/**
 * Resumen seguro de un error del SDK de AWS v3 para el `cause` de los errores
 * propios. El error crudo de smithy lleva `$response` y, colgando de él, la
 * petición HTTP firmada y los buffers del socket (`authorization:
 * AWS4-HMAC-SHA256 Credential=…`, `x-amz-security-token`); S3 además copia en
 * el error `AWSAccessKeyId`, `StringToSign` y `CanonicalRequest`, y los
 * servicios JSON meten la cadena canónica (con el token de sesión) en el
 * `message` de `InvalidSignatureException`. `util.inspect`, `console.error` o
 * el "Serialized Error" de vitest lo imprimen todo. Aquí sólo sobrevive una
 * lista cerrada de campos, y el texto pasa por `redactAwsText`.
 */

const METADATA_KEY = "$metadata";
const FAULT_KEY = "$fault";
const REDACTED = "<redacted>";
const MAX_MESSAGE_CHARS = 2048;

/** Lo que queda de `$metadata`: sin cabeceras, sin cuerpo, sin credenciales. */
export interface AwsErrorMetadata {
  readonly httpStatusCode?: number;
  readonly requestId?: string;
  readonly extendedRequestId?: string;
  readonly attempts?: number;
}

export interface SanitizeAwsErrorOptions {
  /**
   * `false` deja el mensaje fuera (sólo el nombre): las transferencias por S3
   * nunca nombran bucket, clave ni host, y un error de red de Node sí los nombra.
   */
  readonly includeMessage?: boolean | undefined;
}

/**
 * La tabla de campos seguros de un error del SDK, como `Error` para que
 * `String(cause)` y `cause.name` sigan diciendo `ThrottlingException: …`, y
 * con `$fault`/`$metadata` con la misma forma que en el SDK (sólo sus campos
 * seguros) para que `cause.$metadata.requestId` siga funcionando.
 */
export class AwsErrorSummary extends Error {
  readonly code: string | undefined;
  declare readonly $fault: string | undefined;
  declare readonly $metadata: AwsErrorMetadata;

  constructor(
    name: string,
    message: string,
    fields: {
      readonly code?: string | undefined;
      readonly fault?: string | undefined;
      readonly metadata: AwsErrorMetadata;
      readonly frames?: string | undefined;
    },
  ) {
    super(message);
    Object.setPrototypeOf(this, new.target.prototype);
    this.name = name;
    this.code = fields.code;
    Object.defineProperty(this, FAULT_KEY, { value: fields.fault, enumerable: true });
    Object.defineProperty(this, METADATA_KEY, {
      value: Object.freeze({ ...fields.metadata }),
      enumerable: true,
    });
    this.stack = `${name}${message ? `: ${message}` : ""}${fields.frames ?? ""}`;
  }
}

const TEXT_RULES: ReadonlyArray<readonly [RegExp, string]> = [
  // La cadena canónica y la cadena a firmar de un `SignatureDoesNotMatch`/`InvalidSignatureException`.
  [/\s*The Canonical String for this request should have been[\s\S]*$/i, ` ${REDACTED}`],
  [/\s*The String-to-Sign should have been[\s\S]*$/i, ` ${REDACTED}`],
  // La query entera de una URL prefirmada, y cualquier parámetro de firma suelto.
  [/\?[^\s'"<>]*X-Amz-[^\s'"<>]*/gi, `?${REDACTED}`],
  [/X-Amz-(?:Security-Token|Credential|Signature)(?:=[^\s&'"<>]*)?/gi, REDACTED],
  [/(authorization\s*[:=]\s*)[^\r\n'"<>]+/gi, `$1${REDACTED}`],
  [/(AWS4-HMAC-SHA256\s+)Credential=[^\r\n'"<>]+/gi, `$1${REDACTED}`],
  [/(x-amz-security-token\s*[:=]\s*)[^\s&'"<>]+/gi, `$1${REDACTED}`],
  [/((?:^|[^A-Za-z-])Signature\s*[:=]\s*)[0-9a-f]{16,}/gi, `$1${REDACTED}`],
  [/\b(?:AKIA|ASIA|AROA|AIDA)[A-Z0-9]{12,}\b/g, REDACTED],
];

/**
 * Quita de un texto de AWS la cadena canónica, las cabeceras de firma, los
 * parámetros de una URL prefirmada y los ids de clave de acceso; lo acota a
 * 2048 caracteres. Pura y total: nunca lanza.
 */
export function redactAwsText(text: string): string {
  let result = text;
  for (const [pattern, replacement] of TEXT_RULES) {
    result = result.replace(pattern, replacement);
  }
  return result.length > MAX_MESSAGE_CHARS ? `${result.slice(0, MAX_MESSAGE_CHARS)}…` : result;
}

function field(source: object, key: string): unknown {
  try {
    return (source as Record<string, unknown>)[key];
  } catch {
    return undefined;
  }
}

function stringField(source: object, key: string): string | undefined {
  const value = field(source, key);
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function safeMetadata(error: object): AwsErrorMetadata {
  const metadata = field(error, METADATA_KEY);
  if (typeof metadata !== "object" || metadata === null) {
    return {};
  }
  const status = field(metadata, "httpStatusCode");
  const attempts = field(metadata, "attempts");
  const requestId = stringField(metadata, "requestId");
  const extendedRequestId = stringField(metadata, "extendedRequestId");
  return {
    ...(typeof status === "number" ? { httpStatusCode: status } : {}),
    ...(requestId === undefined ? {} : { requestId: redactAwsText(requestId) }),
    ...(extendedRequestId === undefined
      ? {}
      : { extendedRequestId: redactAwsText(extendedRequestId) }),
    ...(typeof attempts === "number" ? { attempts } : {}),
  };
}

/** Sólo las líneas `at …` de la pila original: rutas y funciones, nunca el mensaje. */
function stackFrames(error: object): string | undefined {
  const stack = field(error, "stack");
  if (typeof stack !== "string") {
    return undefined;
  }
  const frames = stack.split("\n").filter((line) => /^\s+at\s/.test(line));
  return frames.length === 0 ? undefined : `\n${frames.map(redactAwsText).join("\n")}`;
}

function errorName(error: object): string {
  const name = stringField(error, "name");
  return name === undefined ? "Error" : redactAwsText(name);
}

/**
 * El resumen seguro de cualquier valor lanzado por el SDK de AWS: nombre,
 * `code` (el `Code` de S3 o el `code` de un error de sistema de Node),
 * mensaje redactado, `$fault` y `$metadata.{httpStatusCode, requestId,
 * extendedRequestId, attempts}`. Nunca `$response`, cabeceras, cuerpo ni
 * credenciales, ni ninguna otra propiedad del error original.
 */
export function sanitizeAwsError(
  error: unknown,
  options: SanitizeAwsErrorOptions = {},
): AwsErrorSummary {
  const includeMessage = options.includeMessage ?? true;
  if (typeof error !== "object" || error === null) {
    const text = includeMessage ? redactAwsText(String(error)) : "";
    return new AwsErrorSummary(typeof error === "string" ? "Error" : typeof error, text, {
      metadata: {},
    });
  }
  const name = errorName(error);
  const rawCode = stringField(error, "Code") ?? stringField(error, "code");
  const rawMessage = stringField(error, "message");
  const fault = stringField(error, FAULT_KEY);
  return new AwsErrorSummary(
    name,
    includeMessage && rawMessage !== undefined ? redactAwsText(rawMessage) : "",
    {
      code: rawCode === undefined ? undefined : redactAwsText(rawCode),
      fault: fault === "client" || fault === "server" ? fault : undefined,
      metadata: safeMetadata(error),
      frames: stackFrames(error),
    },
  );
}
