/**
 * `KeyValueStoreWriter` falso en memoria (m15-custom-domain): espejo de
 * `tests/unit/fake_custom_domain.py`. Un `ETag` por almacén que avanza en
 * cada escritura (optimistic concurrency) y `ResourceNotFoundException`
 * sobre un almacén o una clave que no existen.
 */

import type { KeyValueStoreWriter } from "../../src/custom-domain/kvs.js";
import { RESOURCE_NOT_FOUND_CODE } from "../../src/custom-domain/kvs.js";
import { CustomDomainError } from "../../src/errors.js";

export class FakeKeyValueStoreWriter implements KeyValueStoreWriter {
  readonly stores = new Map<string, Map<string, string>>();
  readonly etags = new Map<string, number>();
  readonly calls: Array<readonly [string, ...string[]]> = [];
  /** `key -> awsCode`: el próximo `put()` sobre esa clave falla una vez con
   * ese código (se consume al fallar), para probar el rollback/retry de
   * `CustomDomain#writeRoute`. */
  readonly failPutOnce = new Map<string, string>();

  #requireStore(kvsArn: string): Map<string, string> {
    const store = this.stores.get(kvsArn);
    if (store === undefined) {
      throw new CustomDomainError("almacén no encontrado", { awsCode: RESOURCE_NOT_FOUND_CODE });
    }
    return store;
  }

  #bump(kvsArn: string): string {
    const next = (this.etags.get(kvsArn) ?? 0) + 1;
    this.etags.set(kvsArn, next);
    return String(next);
  }

  /** Crea el almacén vacío (como si `deploy()` ya hubiera corrido). */
  seed(kvsArn: string): void {
    if (!this.stores.has(kvsArn)) {
      this.stores.set(kvsArn, new Map());
      this.etags.set(kvsArn, 0);
    }
  }

  async describe(kvsArn: string): Promise<string> {
    this.calls.push(["describe", kvsArn]);
    this.#requireStore(kvsArn);
    return String(this.etags.get(kvsArn) ?? 0);
  }

  async put(kvsArn: string, key: string, value: string, ifMatch: string): Promise<string> {
    this.calls.push(["put", kvsArn, key]);
    const store = this.#requireStore(kvsArn);
    const injected = this.failPutOnce.get(key);
    if (injected !== undefined) {
      this.failPutOnce.delete(key);
      throw new CustomDomainError("fallo inyectado para pruebas", { awsCode: injected });
    }
    if (String(this.etags.get(kvsArn) ?? 0) !== ifMatch) {
      throw new CustomDomainError("ETag no coincide", { awsCode: "ConflictException" });
    }
    store.set(key, value);
    return this.#bump(kvsArn);
  }

  async delete(kvsArn: string, key: string, ifMatch: string): Promise<string> {
    this.calls.push(["delete", kvsArn, key]);
    const store = this.#requireStore(kvsArn);
    if (String(this.etags.get(kvsArn) ?? 0) !== ifMatch) {
      throw new CustomDomainError("ETag no coincide", { awsCode: "ConflictException" });
    }
    if (!store.has(key)) {
      throw new CustomDomainError("clave no encontrada", { awsCode: RESOURCE_NOT_FOUND_CODE });
    }
    store.delete(key);
    return this.#bump(kvsArn);
  }
}
