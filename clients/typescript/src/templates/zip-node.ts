/**
 * Lector/escritor mínimo de ZIP (m15-templates), sin dependencias nuevas:
 * `readZipEntries` descomprime con `node:zlib` (DEFLATE puro, método 8, lo
 * que escribe `zipfile.ZIP_DEFLATED` de Python); `writeZip` escribe sin
 * comprimir (método 0, "stored") — un zip válido no necesita compresión, y
 * evita añadir una librería nueva de escritura de zip (investigación §3.6
 * señala que un "escritor de zip" para TS necesitaría auditoría de
 * licencia; esto la evita del todo).
 *
 * Sólo entiende lo que un zip de `codeArtifact`/`gen_stack_assets.py`
 * produce: sin cifrado, sin zip64, sin comentario global largo.
 */

import { inflateRawSync } from "node:zlib";

const EOCD_SIGNATURE = 0x06054b50;
const CENTRAL_DIRECTORY_SIGNATURE = 0x02014b50;
const LOCAL_FILE_HEADER_SIGNATURE = 0x04034b50;
const STORED_METHOD = 0;
const DEFLATE_METHOD = 8;
const DOS_FIXED_DATE_TIME = 0x0021; // 1980-01-01, igual que `scripts/gen_stack_assets.py`.
const EOCD_MIN_SIZE = 22;

/** Tabla CRC-32 (polinomio reflejado 0xEDB88320, IEEE 802.3 / PKZIP APPNOTE). */
const CRC_TABLE: Uint32Array = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) {
      c = (c & 1) !== 0 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(data: Uint8Array): number {
  let crc = 0xffffffff;
  for (const byte of data) {
    crc = (CRC_TABLE[(crc ^ byte) & 0xff] ?? 0) ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function findEndOfCentralDirectory(buffer: Buffer): number {
  for (let offset = buffer.length - EOCD_MIN_SIZE; offset >= 0; offset -= 1) {
    if (buffer.readUInt32LE(offset) === EOCD_SIGNATURE) {
      return offset;
    }
  }
  throw new Error("zip inválido: no se encontró el End Of Central Directory");
}

/** Cada entrada de un zip ya existente, como `nombre -> contenido`;
 * ignora los directorios (entradas terminadas en `/`, sin contenido propio). */
export function readZipEntries(bytes: Uint8Array): Map<string, Uint8Array> {
  const buffer = Buffer.from(bytes);
  const eocdOffset = findEndOfCentralDirectory(buffer);
  const entryCount = buffer.readUInt16LE(eocdOffset + 10);
  const centralDirectoryOffset = buffer.readUInt32LE(eocdOffset + 16);

  const entries = new Map<string, Uint8Array>();
  let cursor = centralDirectoryOffset;
  for (let i = 0; i < entryCount; i += 1) {
    if (buffer.readUInt32LE(cursor) !== CENTRAL_DIRECTORY_SIGNATURE) {
      throw new Error("zip inválido: entrada del directorio central corrupta");
    }
    const compressionMethod = buffer.readUInt16LE(cursor + 10);
    const compressedSize = buffer.readUInt32LE(cursor + 20);
    const nameLength = buffer.readUInt16LE(cursor + 28);
    const extraLength = buffer.readUInt16LE(cursor + 30);
    const commentLength = buffer.readUInt16LE(cursor + 32);
    const localHeaderOffset = buffer.readUInt32LE(cursor + 42);
    const name = buffer.toString("utf8", cursor + 46, cursor + 46 + nameLength);
    cursor += 46 + nameLength + extraLength + commentLength;

    if (!name.endsWith("/")) {
      entries.set(
        name,
        readLocalFile(buffer, localHeaderOffset, compressionMethod, compressedSize),
      );
    }
  }
  return entries;
}

function readLocalFile(
  buffer: Buffer,
  offset: number,
  compressionMethod: number,
  compressedSize: number,
): Uint8Array {
  if (buffer.readUInt32LE(offset) !== LOCAL_FILE_HEADER_SIGNATURE) {
    throw new Error("zip inválido: cabecera local corrupta");
  }
  const nameLength = buffer.readUInt16LE(offset + 26);
  const extraLength = buffer.readUInt16LE(offset + 28);
  const dataStart = offset + 30 + nameLength + extraLength;
  const raw = buffer.subarray(dataStart, dataStart + compressedSize);
  if (compressionMethod === STORED_METHOD) {
    return new Uint8Array(raw);
  }
  if (compressionMethod === DEFLATE_METHOD) {
    return new Uint8Array(inflateRawSync(raw));
  }
  throw new Error(`zip inválido: método de compresión no soportado (${compressionMethod})`);
}

/** Un zip determinista (orden de `entries`, método "stored", fecha fija)
 * con cada `[nombre, contenido]` de `entries`. */
export function writeZip(entries: ReadonlyArray<readonly [string, Uint8Array]>): Uint8Array {
  const localParts: Buffer[] = [];
  const centralParts: Buffer[] = [];
  let offset = 0;

  for (const [name, content] of entries) {
    const nameBytes = Buffer.from(name, "utf8");
    const checksum = crc32(content);
    const localHeader = Buffer.alloc(30);
    localHeader.writeUInt32LE(LOCAL_FILE_HEADER_SIGNATURE, 0);
    localHeader.writeUInt16LE(20, 4); // version needed to extract
    localHeader.writeUInt16LE(0, 6); // flags
    localHeader.writeUInt16LE(STORED_METHOD, 8);
    localHeader.writeUInt16LE(0, 10); // mod time
    localHeader.writeUInt16LE(DOS_FIXED_DATE_TIME, 12); // mod date
    localHeader.writeUInt32LE(checksum, 14);
    localHeader.writeUInt32LE(content.length, 18); // compressed size
    localHeader.writeUInt32LE(content.length, 22); // uncompressed size
    localHeader.writeUInt16LE(nameBytes.length, 26);
    localHeader.writeUInt16LE(0, 28); // extra length
    localParts.push(localHeader, nameBytes, Buffer.from(content));

    const centralHeader = Buffer.alloc(46);
    centralHeader.writeUInt32LE(CENTRAL_DIRECTORY_SIGNATURE, 0);
    centralHeader.writeUInt16LE(20, 4); // version made by
    centralHeader.writeUInt16LE(20, 6); // version needed to extract
    centralHeader.writeUInt16LE(0, 8); // flags
    centralHeader.writeUInt16LE(STORED_METHOD, 10);
    centralHeader.writeUInt16LE(0, 12); // mod time
    centralHeader.writeUInt16LE(DOS_FIXED_DATE_TIME, 14); // mod date
    centralHeader.writeUInt32LE(checksum, 16);
    centralHeader.writeUInt32LE(content.length, 20); // compressed size
    centralHeader.writeUInt32LE(content.length, 24); // uncompressed size
    centralHeader.writeUInt16LE(nameBytes.length, 28);
    centralHeader.writeUInt16LE(0, 30); // extra length
    centralHeader.writeUInt16LE(0, 32); // comment length
    centralHeader.writeUInt16LE(0, 34); // disk number
    centralHeader.writeUInt16LE(0, 36); // internal attrs
    centralHeader.writeUInt32LE(0o644 << 16, 38); // external attrs (unix perms)
    centralHeader.writeUInt32LE(offset, 42);
    centralParts.push(centralHeader, nameBytes);

    offset += localHeader.length + nameBytes.length + content.length;
  }

  const centralDirectoryOffset = offset;
  const centralDirectory = Buffer.concat(centralParts);
  const eocd = Buffer.alloc(EOCD_MIN_SIZE);
  eocd.writeUInt32LE(EOCD_SIGNATURE, 0);
  eocd.writeUInt16LE(0, 4);
  eocd.writeUInt16LE(0, 6);
  eocd.writeUInt16LE(entries.length, 8);
  eocd.writeUInt16LE(entries.length, 10);
  eocd.writeUInt32LE(centralDirectory.length, 12);
  eocd.writeUInt32LE(centralDirectoryOffset, 16);
  eocd.writeUInt16LE(0, 20);

  return new Uint8Array(Buffer.concat([...localParts, centralDirectory, eocd]));
}
