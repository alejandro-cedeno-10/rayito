# Ficheros y transferencias

`sbx.files` lee, escribe, lista y vigila ficheros del sandbox por gRPC a
través del proxy de Lambda MicroVMs. Desde M9 (`m9-file-transfer`, ADR-010)
suma URLs de S3 prefirmadas (`upload_url`/`download_url`), un camino por S3
para los ficheros grandes, gzip, metadatos por fichero y un plazo entre trozos
de lectura. Todo lo de M9 exige una imagen construida con el `rayd` de M9: en
una anterior el SDK lanza `UnimplementedError("actualiza la imagen")` antes de
mover un byte.

## La superficie de ficheros

| Método | Qué hace |
|---|---|
| `files.read(path, format=...)` | lee un fichero regular como `text`, `bytes` o `stream` (TS: además `blob`) |
| `files.write(path, data)` / `write_files([WriteEntry, ...])` | escritura atómica (temporal + rename), padres creados; varios ficheros en un stream |
| `files.list(path, depth=)`, `exists`, `get_info`, `remove`, `rename`, `make_dir` | como en E2B |
| `files.watch_dir(path, on_event=...)` | eventos de inotify hasta `stop()`; un `files.write` en el directorio llega como un único `WRITE` del destino (con `entry` si `include_entry=True`), como el ejemplo de E2B |
| `files.upload_url(path)` / `download_url(path)` (M9) | URLs de S3 firmadas con tus credenciales |

Por el proxy, un fichero sube a ≈ 0,6 MB/s (la ventana HTTP/2 del proxy,
`AWS_API_NOTES.md` Q32) y baja a 4–5 MB/s (Q27). Entre el VM y S3 se midieron
55,7–106,2 MB/s de subida y 84,2–99,0 MB/s de bajada (Q59). Por eso los
ficheros grandes van por S3.

## `S3Staging`: el bucket de transferencias

Las URLs y los ficheros grandes necesitan un bucket tuyo. No hay bucket por
defecto: pásalo en `create`/`connect` o en el entorno.

=== "Python"

    ```python
    from rayito import S3Staging, Sandbox

    staging = S3Staging(
        "amzn-s3-demo-bucket",
        prefix="rayito-transfer",           # por defecto
        region=None,                        # la del sandbox; debe ser la del bucket
        max_expires_in=86400,               # tope de las URLs de usuario
        threshold_bytes=8 * 1024 * 1024,    # desde aquí files.write/read van por S3
    )
    sbx = Sandbox.create(transfer=staging)
    # o: RAYITO_TRANSFER_BUCKET=amzn-s3-demo-bucket (y RAYITO_TRANSFER_PREFIX, RAYITO_TRANSFER_REGION)
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    const sbx = await Sandbox.create({
      transfer: { bucket: "amzn-s3-demo-bucket", prefix: "rayito-transfer" },
    });
    ```

Las claves quedan bajo `<prefix>/<sandbox_id>/<up|down>/<id>`: una clave por
URL, ligada al sandbox, que `rayd` comprueba antes de hacer nada. El SDK firma
con SigV4 y el host virtual **regional** del bucket (`<bucket>.s3.<region>.amazonaws.com`);
`rayd` rechaza cualquier otra forma. El prefijo nunca puede ser `rayito` (los
artefactos de imagen) ni el de persistencia: la regla de ciclo de vida de 1
día los borraría.

## `upload_url` y `download_url`

`upload_url` arma una importación en el sandbox y devuelve una URL de subida;
lo que se suba a ella aterriza en `path`.

=== "Python (`requests`)"

    ```python
    import requests
    from rayito import S3Staging, Sandbox

    with Sandbox.create(transfer=S3Staging("amzn-s3-demo-bucket")) as sbx:
        ticket = sbx.files.upload_url("/home/user/datos.csv", expires_in=900, max_bytes=50_000_000)
        with open("datos.csv", "rb") as source:
            requests.put(ticket, data=source, headers=ticket.headers).raise_for_status()
        entry = ticket.wait()          # la importación terminó: EntryInfo del fichero

        link = sbx.files.download_url("/home/user/datos.csv", filename="datos.csv")
        print(entry.size, link.size, link.sha256)
        body = requests.get(link).content
    ```

=== "curl"

    ```bash
    curl -T datos.csv -H "Content-Type: application/octet-stream" "$UPLOAD_URL"
    curl -o datos.csv "$DOWNLOAD_URL"
    curl -H "Range: bytes=0-1023" "$DOWNLOAD_URL"
    ```

=== "Python (async)"

    ```python
    import asyncio

    import httpx
    from rayito import AsyncSandbox, S3Staging


    async def main() -> None:
        staging = S3Staging("amzn-s3-demo-bucket")
        async with await AsyncSandbox.create(transfer=staging) as sbx:
            ticket = await sbx.files.upload_url("/home/user/datos.csv", expires_in=900)
            async with httpx.AsyncClient() as http:
                with open("datos.csv", "rb") as source:
                    response = await http.put(ticket.url, content=source.read(), headers=ticket.headers)
                response.raise_for_status()
                entry = await ticket.wait()
                print(entry.size, (await ticket.status()).phase)

                link = await sbx.files.download_url("/home/user/datos.csv")
                body = (await http.get(link.url)).content
                print(len(body) == link.size)


    asyncio.run(main())
    ```

=== "TypeScript (`fetch`)"

    ```ts
    import { readFile } from "node:fs/promises";
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({ transfer: { bucket: "amzn-s3-demo-bucket" } });
    const ticket = await sbx.files.uploadUrl("/home/user/datos.csv", {
      expiresIn: 900,
      maxBytes: 50_000_000,
    });
    const put = await fetch(ticket.url, {
      method: "PUT",
      body: await readFile("datos.csv"),
      headers: ticket.headers,
    });
    if (!put.ok) throw new Error(`PUT ${put.status}`);
    const entry = await ticket.wait({ timeoutMs: 120_000 });
    console.log(entry.size);

    const link = await sbx.files.downloadUrl("/home/user/datos.csv", { filename: "datos.csv" });
    console.log(link.size, link.sha256, link.expiresAt);
    const bytes = new Uint8Array(await (await fetch(link.url)).arrayBuffer());
    console.log(bytes.length === link.size);
    ```

- **El ticket es un `str`** (Python) con la URL; `ticket.headers` son las
  cabeceras que el `PUT` debe llevar (`Content-Type:
  application/octet-stream`). En TypeScript la URL es `ticket.url` (o
  `String(ticket)`).
- **Formulario `POST`** para navegadores: `upload_url(path, form=True)` da
  `ticket.method == "POST"` y `ticket.fields`; se envían los campos más el
  fichero en el campo `file`. S3 limita el tamaño a `max_bytes` (5 GiB si no).
- **De un solo uso.** La primera subida que aterriza cierra el ticket; una
  segunda no se importa. `ticket.status()` y `ticket.cancel()` consultan y
  cancelan la importación.
- **Caducidad** siempre fijada: `expires_in` (3600 s por defecto), topada por
  `S3Staging.max_expires_in` y por los 7 días de SigV4. Si firmas con
  credenciales temporales (SSO, un rol), la URL deja de valer cuando caducan
  ellas aunque diga más (Q62).
- **`download_url` es una foto** del fichero tal como está al llamar (la
  exportación termina antes de devolver la URL); un fichero que no existe
  lanza `FileNotFoundException` al momento, un directorio o un symlink
  `InvalidArgumentException`. Desde `S3Staging.multipart_threshold_bytes`
  (5 GiB) la exportación es multiparte. Una exportación que una suspensión
  reencola vuelve a leer el fichero tras reanudar.
- Los errores de una importación (`ticket.wait()`) son
  `FileUploadException(code, reason)`: `too_large`, `expired`,
  `checksum_mismatch`, `disk_reserve`, `wrong_region`...; los de una
  exportación, `TransferException`.

### La barrera de lectura tras subida

Tras tu `PUT`, `rayd` ve el objeto en el siguiente sondeo del `GET` (cada 1 s
durante los primeros 10 minutos, luego cada 5 s) y lo escribe. Para que "subo
y luego leo o ejecuto" funcione sin `wait()`, un `files.read`, `get_info`,
`list` del directorio padre, `commands.run`, `run_code` o `pty.create`
posterior espera hasta 2 s a las importaciones armadas: si el objeto ya está
en S3 la importación se completa antes de la operación. Si no llega en ese
margen la operación sigue (verá el fichero viejo o `NotFound`); `ticket.wait()`
es la forma determinista. Medido (Q71): entre el `200` del `PUT` y el
fichero visible pasan 0,34 s de mediana y 0,83 s de p95 con 1 MiB; con
50 MB, `ticket.wait()` vuelve 0,7 s después del `200`.

## Ficheros grandes por S3

Con `transfer=` configurado, `files.write`/`write_files` de un fichero de
`threshold_bytes` o más (8 MiB por defecto, y todo stream binario no buscable)
lo sube el SDK directamente a S3 con tus credenciales mientras calcula su
sha256, y `rayd` lo importa comprobando ese sha256. `files.read` de un fichero
así lo exporta y el SDK lo descarga verificando el sha256; el objeto temporal
se borra al terminar. Sin `transfer` el camino gRPC no cambia. `gzip` no se
aplica a lo que va por S3. Medido (Q75): dentro del VM `rayd` importa a
60-74 MB/s y exporta a 28-86 MB/s (10 MiB a 200 MB), así que el límite lo
pone tu enlace con S3. Desde una conexión doméstica de ~3,5 MB/s de subida,
200 MB por S3 van 6 veces más rápido que por gRPC al escribir; al leer, gRPC
ya iguala ese enlace. No se ha medido desde un cliente en la misma región.

=== "Python"

    ```python
    from rayito import S3Staging, Sandbox

    staging = S3Staging("amzn-s3-demo-bucket", threshold_bytes=8 * 1024 * 1024)
    with Sandbox.create(transfer=staging) as sbx:
        with open("modelo.bin", "rb") as source:          # ≥ 8 MiB: por S3, sha256 comprobado
            info = sbx.files.write("/home/user/modelo.bin", source)
        data = sbx.files.read("/home/user/modelo.bin", format="bytes")   # vuelve por S3
        print(info.size == len(data))
    ```

=== "TypeScript"

    ```ts
    import { createReadStream } from "node:fs";
    import { Readable } from "node:stream";
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create({
      transfer: { bucket: "amzn-s3-demo-bucket", thresholdBytes: 8 * 1024 * 1024 },
    });
    // Un ReadableStream siempre va por S3 (subida en streaming con @aws-sdk/lib-storage).
    const body = Readable.toWeb(createReadStream("modelo.bin")) as ReadableStream<Uint8Array>;
    const info = await sbx.files.write("/home/user/modelo.bin", body);
    const data = await sbx.files.read("/home/user/modelo.bin", { format: "bytes" });
    console.log(info.size === data.length);
    ```

## gzip, metadatos y `stream_idle_timeout`

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        texto_grande = "línea\n" * 100_000
        sbx.files.write("/home/user/log.txt", texto_grande, gzip=True)       # compresión gRPC
        texto = sbx.files.read("/home/user/log.txt", gzip=True)              # respuesta comprimida
        sbx.files.write("/home/user/a.bin", b"\x00" * 16, metadata={"origen": "ci", "lote": "7"})
        print(sbx.files.get_info("/home/user/a.bin").metadata)               # {'origen': 'ci', 'lote': '7'}
        for chunk in sbx.files.read("/home/user/log.txt", format="stream", stream_idle_timeout=30):
            print(len(chunk))
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    await sbx.files.write("/home/user/log.txt", "línea\n".repeat(100_000), { gzip: true });
    const texto = await sbx.files.read("/home/user/log.txt", { gzip: true });
    await sbx.files.write("/home/user/a.bin", new Uint8Array(16), {
      metadata: { origen: "ci", lote: "7" },
    });
    console.log((await sbx.files.getInfo("/home/user/a.bin")).metadata);
    const blob = await sbx.files.read("/home/user/log.txt", { format: "blob", streamIdleTimeoutMs: 30_000 });
    console.log(texto.length, blob.size);
    ```

- **gzip** es la compresión estándar de gRPC: las respuestas sólo llegan
  comprimidas si la petición lleva la cabecera `rayito-compress: gzip`, que
  el SDK pone con `gzip=True`. Cruza el proxy en los dos sentidos: 20 MB de
  texto compresible pasan de 0,80 a 52,34 MB/s de escritura + lectura (Q74).
- **Metadatos**: se guardan como xattrs `user.rayito.<clave>` del fichero
  (claves de caracteres de token HTTP, en minúsculas; valores ASCII
  imprimible; ≤ 64 claves y ≤ 4 000 B). Sobrescribir un fichero reemplaza el
  conjunto entero, y sobrescribirlo sin `metadata` lo vacía (semántica de
  envd). No son secretos ni se registran en logs.
- **`stream_idle_timeout`** (TS `streamIdleTimeoutMs`) corta una lectura que
  lleva ese tiempo sin recibir un trozo (`TimeoutException`).
- **`use_octet_stream`** se acepta sin efecto (gRPC no tiene formulario
  multipart).

## IAM y el bucket

Las credenciales que firman son las de **tu** proceso (el llamante), nunca un
execution role: `rayd` no guarda ninguna (T16). La política mínima del
llamante está parametrizada en `spike/m0/iam.yaml` (`TransferBucket`,
`TransferPrefix`): `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` y
`s3:AbortMultipartUpload` sobre `<bucket>/<prefix>/*` y `s3:ListBucket`
acotado a ese prefijo (sin él, una clave que falta da 403 en vez de 404). La
receta del bucket (regla de ciclo de vida de 1 día, política que exige SigV4 y
TLS, CORS para navegadores, SSE-KMS) está en `infra/README.md`,
"Transferencias de ficheros".

## En el shim de E2B

`rayito.e2b.Sandbox.create()` no acepta `transfer=`: el bucket sale de
`RAYITO_TRANSFER_BUCKET` (y `RAYITO_TRANSFER_PREFIX`, `RAYITO_TRANSFER_REGION`).

=== "Python"

    ```python
    import os

    import requests
    from rayito.e2b import Sandbox

    os.environ.setdefault("RAYITO_TRANSFER_BUCKET", "amzn-s3-demo-bucket")
    with Sandbox.create() as sbx:
        url = sbx.upload_url("/home/user/in.csv", use_signature_expiration=600)
        requests.put(url, data=b"a,b\n1,2\n", headers=url.headers).raise_for_status()
        url.wait()                                   # sólo Rayito: espera la importación
        print(requests.get(sbx.download_url("/home/user/in.csv")).text)
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito/e2b";

    // RAYITO_TRANSFER_BUCKET=amzn-s3-demo-bucket en el entorno
    await using sbx = await Sandbox.create();
    const url = await sbx.uploadUrl("/home/user/in.csv", { useSignatureExpiration: 600 });
    await fetch(url, {
      method: "PUT",
      body: "a,b\n1,2\n",
      headers: { "Content-Type": "application/octet-stream" },
    });
    // la barrera de rayd hace que la lectura siguiente espere la importación (hasta 2 s)
    console.log(await sbx.files.read("/home/user/in.csv"));
    const download = await sbx.downloadUrl("/home/user/in.csv");
    console.log(await (await fetch(download)).text());
    ```

En TypeScript el shim devuelve la URL como `string`, igual que E2B; para el
ticket con `wait()` usa `sbx.native.files.uploadUrl()`.

## Diferencias con E2B

- Subida por `PUT` con el cuerpo en crudo en vez del `POST` multipart de
  envd; `form=True` da los campos de un `POST`.
- La subida aterriza de forma asíncrona: la cubre la barrera de 2 s y
  `ticket.wait()`.
- Tickets de un solo uso.
- La descarga es una foto tomada al llamar.
- Caducidad siempre fijada (≤ 7 días y ≤ la vida de tus credenciales);
  `use_signature_expiration <= 0` lanza.
- Hace falta un bucket de transferencias; sin él, `UnimplementedError`.
- Un fichero que no existe lanza en `download_url`, no al descargar.
- `upload_url(path=None)` lanza: una URL de S3 no lleva nombre de fichero.
- En el shim async, `upload_url`/`download_url` son corrutinas.
