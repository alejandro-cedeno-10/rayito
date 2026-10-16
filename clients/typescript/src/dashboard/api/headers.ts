/**
 * Las cabeceras de seguridad de toda respuesta del panel: CSP sin
 * `unsafe-inline` ni `unsafe-eval` (la SPA son módulos y hojas de estilo del
 * propio origen), `nosniff`, sin caché, sin marcos y sin `Referer`. Las
 * mismas que las de Python, comprobadas con
 * `testdata/dashboard/security-headers.json`. Espejo de
 * `rayito._dashboard._api._headers`.
 */

const SELF = "'self'";
const NONE = "'none'";

/**
 * El logo se sirve con su propia CSP, aún más cerrada: aunque alguien abra
 * `/theme/logo` como documento, un SVG no ejecuta nada ni carga nada.
 */
export const LOGO_HEADERS: Readonly<Record<string, string>> = {
  "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
};

export function contentSecurityPolicy(connectSrc: readonly string[] = []): string {
  const connect = [SELF, ...connectSrc].join(" ");
  return (
    `default-src ${NONE}; script-src ${SELF}; style-src ${SELF}; connect-src ${connect}; ` +
    `img-src ${SELF}; base-uri ${NONE}; form-action ${NONE}; frame-ancestors ${NONE}`
  );
}

export function securityHeaders(connectSrc: readonly string[] = []): Record<string, string> {
  return {
    "Content-Security-Policy": contentSecurityPolicy(connectSrc),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
  };
}
