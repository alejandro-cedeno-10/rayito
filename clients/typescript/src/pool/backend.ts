/**
 * Backends de estado del pool: dónde viven los registros de plaza (y sus
 * secretos) entre `run-microvm` y `take()`.
 *
 * `PoolBackend` es la costura para un almacén compartido futuro; aquí hay
 * dos: `InMemoryPoolBackend` (por defecto, muere con el proceso) y
 * `JsonFilePoolBackend` (un fichero JSON con esquema `rayito.pool/1`,
 * escrito de forma atómica a través de un temporal exclusivo de nombre
 * aleatorio con modo 0600, y el mismo formato que el SDK Python). El de fichero es **de un solo proceso** —sin bloqueo, sin
 * coordinación entre hosts— y existe para los tests y para recuperar un pool
 * en el mismo host tras un reinicio. Contiene los access tokens de las plazas
 * aparcadas en claro: es tan sensible como `RAYITO_ACCESS_TOKEN`. El pool
 * serializa toda llamada al backend, así que no necesita ser reentrante.
 */

import { randomBytes } from "node:crypto";
import { constants } from "node:fs";
import { open, rename, unlink } from "node:fs/promises";
import { InvalidArgumentError } from "../errors.js";
import { POOL_SCHEMA, recordFromJson, recordToJson, type SlotRecord } from "./core.js";

export const FILE_MODE = 0o600;
export const TEMP_SUFFIX = ".tmp";

const NO_FOLLOW = (constants as Partial<typeof constants>).O_NOFOLLOW ?? 0;
const READ_FLAGS = constants.O_RDONLY | NO_FOLLOW;
const NOT_A_REGULAR_FILE =
  "el fichero de estado del pool debe ser un fichero regular, no un enlace";

/**
 * Nombre impredecible en el mismo directorio: no hay nada que precrear ni que
 * enlazar, y `rename` sigue siendo atómico por estar en el mismo sistema de
 * ficheros.
 */
function temporaryPath(path: string): string {
  return `${path}.${randomBytes(8).toString("hex")}${TEMP_SUFFIX}`;
}

/**
 * El temporal puede no existir (el fallo pudo ser el propio `open`), así que un
 * borrado que falla no vuelve a lanzar: el error que importa es el original.
 */
async function discard(path: string): Promise<void> {
  try {
    await unlink(path);
  } catch {
    return;
  }
}

/** Registros de plaza indexados por `sandboxId`. */
export interface PoolBackend {
  readonly persistent: boolean;
  load(): Promise<readonly SlotRecord[]>;
  /** Upsert por `sandboxId`. */
  save(record: SlotRecord): Promise<void>;
  /** Un id desconocido no es un error. */
  delete(sandboxId: string): Promise<void>;
}

/** Un `Map`: los registros (y sus secretos) mueren con el proceso. */
export class InMemoryPoolBackend implements PoolBackend {
  readonly persistent = false;
  readonly #records = new Map<string, SlotRecord>();

  async load(): Promise<readonly SlotRecord[]> {
    return [...this.#records.values()];
  }

  async save(record: SlotRecord): Promise<void> {
    this.#records.set(record.sandboxId, record);
  }

  async delete(sandboxId: string): Promise<void> {
    this.#records.delete(sandboxId);
  }
}

/**
 * `{"schema": "rayito.pool/1", "slots": [...]}` en `path`: cada escritura
 * vuelca el fichero completo a un temporal de nombre aleatorio en el mismo
 * directorio, creado en exclusiva y con el modo 0600 fijado sobre el propio
 * handle (sin efecto en Windows), y lo sustituye con `rename`; cualquier fallo
 * borra el temporal. La lectura se niega a seguir un enlace en la ruta del
 * estado. Un fichero ausente carga `[]`; otro `schema` es
 * `InvalidArgumentError`.
 */
export class JsonFilePoolBackend implements PoolBackend {
  readonly persistent = true;
  readonly path: string;

  constructor(path: string) {
    this.path = path;
  }

  async load(): Promise<readonly SlotRecord[]> {
    return [...(await this.#read()).values()];
  }

  async save(record: SlotRecord): Promise<void> {
    const records = await this.#read();
    records.set(record.sandboxId, record);
    await this.#write(records);
  }

  async delete(sandboxId: string): Promise<void> {
    const records = await this.#read();
    if (records.delete(sandboxId)) {
      await this.#write(records);
    }
  }

  async #read(): Promise<Map<string, SlotRecord>> {
    let text: string;
    try {
      const handle = await open(this.path, READ_FLAGS);
      try {
        text = await handle.readFile("utf8");
      } finally {
        await handle.close();
      }
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === "ENOENT") {
        return new Map();
      }
      if (code === "ELOOP") {
        throw new InvalidArgumentError(`${this.path}: ${NOT_A_REGULAR_FILE}`, { cause: error });
      }
      throw error;
    }
    return recordsFromDocument(parseDocument(text, this.path));
  }

  async #write(records: Map<string, SlotRecord>): Promise<void> {
    const document = {
      schema: POOL_SCHEMA,
      slots: [...records.values()].map(recordToJson),
    };
    const temp = temporaryPath(this.path);
    try {
      const handle = await open(temp, "wx", FILE_MODE);
      try {
        await handle.chmod(FILE_MODE);
        await handle.writeFile(JSON.stringify(document, null, 2), "utf8");
      } finally {
        await handle.close();
      }
      await rename(temp, this.path);
    } catch (error) {
      await discard(temp);
      throw error;
    }
  }
}

export function parseDocument(text: string, path: string): Record<string, unknown> {
  let document: unknown;
  try {
    document = JSON.parse(text);
  } catch (error) {
    throw new InvalidArgumentError(`${path}: no es JSON válido`, { cause: error });
  }
  if (document === null || typeof document !== "object" || Array.isArray(document)) {
    throw new InvalidArgumentError(`${path}: el documento del pool debe ser un objeto`);
  }
  const schema = (document as Record<string, unknown>).schema;
  if (schema !== POOL_SCHEMA) {
    throw new InvalidArgumentError(
      `${path}: schema ${JSON.stringify(schema)} no reconocido; este SDK lee ${JSON.stringify(POOL_SCHEMA)}`,
    );
  }
  return document as Record<string, unknown>;
}

export function recordsFromDocument(document: Record<string, unknown>): Map<string, SlotRecord> {
  const slots = document.slots ?? [];
  if (!Array.isArray(slots)) {
    throw new InvalidArgumentError("`slots` debe ser una lista");
  }
  const records = new Map<string, SlotRecord>();
  for (const item of slots) {
    const record = recordFromJson(item);
    records.set(record.sandboxId, record);
  }
  return records;
}
