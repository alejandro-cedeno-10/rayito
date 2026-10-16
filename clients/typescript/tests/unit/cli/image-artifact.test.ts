/**
 * El zip de la imagen más allá de los casos compartidos: el archivo es
 * idéntico byte a byte al repetirlo, un enlace simbólico se rechaza en vez de
 * seguirse, un entorno virtual enlazado se salta, `copySidecar` aplica las
 * mismas exclusiones y exige los dos ficheros de pines, y el escritor de zip
 * comprime y deja los modos Unix. Espejo de `test_artifact.py`.
 */

import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { comparePaths, ImageArtifactError } from "../../../src/images/artifact.js";
import { artifactSha256, copySidecar, shippedFiles } from "../../../src/images/artifact-node.js";
import { readZipEntries, writeZipEntries } from "../../../src/templates/zip-node.js";
import { ImageWorkspace } from "./image-fakes.js";

const folders: string[] = [];
const workspaces: ImageWorkspace[] = [];

afterEach(() => {
  for (const folder of folders.splice(0)) {
    rmSync(folder, { recursive: true, force: true });
  }
  for (const workspace of workspaces.splice(0)) {
    workspace.dispose();
  }
});

function workspace(): ImageWorkspace {
  const created = new ImageWorkspace();
  workspaces.push(created);
  return created;
}

function scratch(): string {
  const folder = mkdtempSync(join(tmpdir(), "rayito-sidecar-"));
  folders.push(folder);
  return folder;
}

function sidecar(): string {
  const root = join(scratch(), "sidecar");
  for (const [relative, text] of Object.entries({
    "requirements.txt": "ipykernel==6\n",
    "requirements-poly.txt": "bash_kernel==0.9\n",
    "sidecar.py": "print()\n",
    "tests/test_x.py": "",
    ".venv/lib/x.py": "",
    "__pycache__/x.pyc": "",
    "uv.lock": "",
  })) {
    const path = join(root, relative);
    mkdirSync(join(path, ".."), { recursive: true });
    writeFileSync(path, text, "utf8");
  }
  return root;
}

describe("el zip", () => {
  it("es idéntico byte a byte al repetirlo", () => {
    const files = workspace();
    files.write("kernel-sidecar/a.py", "x = 1\n");
    const first = files.zip("slim", false, "one.zip");
    const second = files.zip("slim", false, "two.zip");
    expect(readFileSync(first).equals(readFileSync(second))).toBe(true);
    expect(artifactSha256(first)).toBe(artifactSha256(second));
    expect(artifactSha256(first)).toMatch(/^[0-9a-f]{64}$/);
  });

  it("comprime con DEFLATE y se lee de vuelta", () => {
    const files = workspace();
    files.write("big.txt", "linea repetida\n".repeat(4000));
    const destination = files.zip();
    const bytes = new Uint8Array(readFileSync(destination));
    const content = readZipEntries(bytes).get("big.txt");
    expect(new TextDecoder().decode(content)).toBe("linea repetida\n".repeat(4000));
    expect(bytes.length).toBeLessThan(10_000);
  });

  it("ordena como `sorted(Path)` de Python, componente a componente", () => {
    expect(["a-b", "a/b", "a", "a/c", "b"].sort(comparePaths)).toEqual([
      "a",
      "a/b",
      "a/c",
      "a-b",
      "b",
    ]);
  });

  it("un enlace simbólico a un fichero se rechaza", () => {
    const files = workspace();
    const outside = join(files.root, "secret.txt");
    writeFileSync(outside, "credenciales\n");
    symlinkSync(outside, join(files.imageDir, "linked.txt"));
    expect(() => files.zip()).toThrow(ImageArtifactError);
    expect(() => files.zip()).toThrow(/linked\.txt es un enlace simbólico/);
  });

  it("un directorio enlazado también se rechaza, dentro de cualquier subárbol", () => {
    const files = workspace();
    mkdirSync(join(files.imageDir, "sub"));
    symlinkSync(files.root, join(files.imageDir, "sub", "linked"));
    expect(() => shippedFiles(files.imageDir)).toThrow(/sub\/linked es un enlace simbólico/);
  });

  it("un enlace dentro de un directorio excluido se ignora y un venv enlazado se salta", () => {
    const files = workspace();
    mkdirSync(join(files.imageDir, "tests"));
    symlinkSync(join(files.root, "nada"), join(files.imageDir, "tests", "dangling"));
    symlinkSync(files.root, join(files.imageDir, ".venv"));
    expect(shippedFiles(files.imageDir)).toEqual([
      "Dockerfile",
      "licenses/LICENSE",
      "licenses/NOTICE",
      "licenses/THIRD_PARTY_LICENSES.md",
      "rayd",
    ]);
  });

  it("un nombre no ASCII lleva la bandera UTF-8 y el modo de rayd es ejecutable", () => {
    const entries = writeZipEntries(
      [
        { name: "ñ.txt", content: new TextEncoder().encode("x") },
        { name: "rayd", content: new TextEncoder().encode("y"), mode: 0o755 },
      ],
      { compress: true, unixCreator: true },
    );
    const buffer = Buffer.from(entries);
    const central = buffer.readUInt32LE(
      buffer.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06])) + 16,
    );
    expect(buffer.readUInt16LE(central + 8) & 0x0800).toBe(0x0800);
    expect(buffer.readUInt16LE(central + 4)).toBe((3 << 8) | 20);
  });
});

describe("copySidecar", () => {
  it("aplica las mismas exclusiones y reemplaza el destino entero", () => {
    const files = workspace();
    const source = sidecar();
    const destination = join(files.imageDir, "kernel-sidecar");
    mkdirSync(destination, { recursive: true });
    writeFileSync(join(destination, "stale.py"), "viejo\n");
    expect(copySidecar(source, destination)).toBe(3);
    expect(existsSync(join(destination, "stale.py"))).toBe(false);
    expect(shippedFiles(destination)).toEqual([
      "requirements-poly.txt",
      "requirements.txt",
      "sidecar.py",
    ]);
  });

  it("falla si falta alguno de los dos ficheros de pines", () => {
    const files = workspace();
    const source = sidecar();
    rmSync(join(source, "requirements-poly.txt"));
    expect(() => copySidecar(source, join(files.imageDir, "kernel-sidecar"))).toThrow(
      /falta requirements-poly\.txt en el sidecar/,
    );
  });

  it("un enlace simbólico en el sidecar se rechaza", () => {
    const files = workspace();
    const source = sidecar();
    symlinkSync(files.root, join(source, "linked"));
    expect(() => copySidecar(source, join(files.imageDir, "kernel-sidecar"))).toThrow(
      ImageArtifactError,
    );
  });
});
