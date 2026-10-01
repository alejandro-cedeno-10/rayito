# Otros lenguajes (gRPC)

Los SDK oficiales son Python y TypeScript. `rayd` es un **servidor** gRPC
dentro del MicroVM y el SDK es un cliente generado desde
[`proto/rayito/v1/`](https://github.com/alejandro-cedeno-10/rayito/tree/main/proto/rayito/v1)
más el ciclo de vida, los tokens y la reconexión. Cualquier lenguaje con un
cliente gRPC y un SDK de AWS puede usar Rayito.

## Comparado con otros sandboxes

| Producto | Agente dentro de la VM | SDK oficiales de cliente |
|---|---|---|
| E2B | `envd` (Go) | Python, JS/TS (Go: forks comunitarios) |
| Daytona | daemon en Go | Python, TypeScript, Ruby, Go, Java + CLI |
| Modal | propietario | Python; JS y Go vía libmodal / modal-client |
| Rayito | `rayd` (Rust) | Python, TypeScript; cualquier otro lenguaje generando un cliente gRPC desde `proto/rayito/v1/` |

## Qué hace falta para un cliente nuevo

1. **Llamar al plano de control** `lambda-microvms` de tu cuenta con el SDK
   de AWS de ese lenguaje: `run-microvm`, `get-microvm`, `suspend` /
   `resume` / `terminate` y `create-microvm-auth-token` para el token del
   proxy (un JWE).
2. **Hablar gRPC sobre HTTPS/2** con `rayd` a través del proxy de AWS, con
   las cuatro cabeceras de metadata en minúsculas: `x-aws-proxy-auth` (el
   JWE), `x-aws-proxy-port` (`8080`), `x-aws-proxy-force-h2` (`true`) y
   `x-access-token` (el secreto del sandbox; `Health` es el único RPC que no
   lo exige).
3. **Implementar la espera de readiness sobre `Health`** (`agent_ready`,
   `kernel_ready`) y, si usas pausa y reanudación, el contrato de
   reconexión (`Connect(from_seq)`, `Pty.Connect`, `WatchDir`, `Reattach`;
   ver [Streams y reconexión](../concepts.md#streams-y-reconexion)).

El access token se fija al crear el sandbox: el SDK genera 32 bytes
aleatorios y envía sólo su sha256 en el `runHookPayload` de `run-microvm`.

## Clientes previstos

- Un cliente **Rust** es trivial de construir (`aws-sdk-lambdamicrovms`
  existe en crates.io y el cliente `tonic` ya está en el workspace) y se
  publicará cuando alguien lo pida.
- Un cliente **Go** no está previsto: genera el tuyo desde el `.proto`.
- Publicar `rayito-proto` en crates.io queda diferido.

??? info "Fuentes y mediciones"
    E2B: [`github.com/e2b-dev/infra`](https://github.com/e2b-dev/infra)
    (`envd` en Go; SDK oficiales sólo Python y JS/TS). Daytona:
    [daytona.io/docs/en/getting-started](https://www.daytona.io/docs/en/getting-started/)
    (Python, TypeScript, Ruby, Go, Java y CLI). Modal:
    [modal.com/docs/guide/sdk-javascript-go](https://modal.com/docs/guide/sdk-javascript-go)
    (JS y Go vía libmodal / modal-client).
