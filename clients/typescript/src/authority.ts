/**
 * Autoridades HTTP (`host:puerto`) tal y como las escribe un cliente en
 * `Host` y `Origin` (espejo de `rayito._authority`). Sin dependencias: las
 * comparte `rayito-mcp --http` y, cuando llegue, el proxy de la CLI.
 */

import { isIP } from "node:net";

const IPV4_VERSION = 4;
const IPV6_VERSION = 6;
const IPV4_LOOPBACK_FIRST_OCTET = 127;
const LOOPBACK_NAME = "localhost";
const BITS_PER_OCTET = 8;
const HEX_RADIX = 16;
const IPV6_UNSPECIFIED = "[::]";
const IPV6_LOOPBACK = "[::1]";
const IPV4_UNSPECIFIED = "0.0.0.0";
const IPV4_MAPPED_PREFIX = /^\[::ffff:([0-9a-f]{1,4}):[0-9a-f]{1,4}\]$/;

/** `Host` y `Origin` como los escribe un cliente: un literal IPv6 va entre corchetes (RFC 3986 §3.2.2). */
export function httpAuthority(host: string, port: number): string {
  return isIP(host) === IPV6_VERSION ? `[${host}]:${port}` : `${host}:${port}`;
}

function normalizedIpv6(host: string): string | undefined {
  if (isIP(host) !== IPV6_VERSION) {
    return undefined;
  }
  return new URL(`http://[${host}]`).hostname;
}

/** `0.0.0.0` y `::` ligan todas las interfaces: son una interfaz válida y una autoridad imposible. */
export function isWildcard(host: string): boolean {
  return host === IPV4_UNSPECIFIED || normalizedIpv6(host) === IPV6_UNSPECIFIED;
}

function isIpv4Loopback(host: string): boolean {
  return (
    isIP(host) === IPV4_VERSION &&
    Number.parseInt(host.split(".")[0] ?? "", 10) === IPV4_LOOPBACK_FIRST_OCTET
  );
}

function isMappedIpv4Loopback(normalized: string): boolean {
  const high = IPV4_MAPPED_PREFIX.exec(normalized)?.[1];
  return (
    high !== undefined &&
    Number.parseInt(high, HEX_RADIX) >> BITS_PER_OCTET === IPV4_LOOPBACK_FIRST_OCTET
  );
}

export function isLoopback(host: string): boolean {
  if (host === LOOPBACK_NAME || isIpv4Loopback(host)) {
    return true;
  }
  const normalized = normalizedIpv6(host);
  return (
    normalized !== undefined && (normalized === IPV6_LOOPBACK || isMappedIpv4Loopback(normalized))
  );
}
