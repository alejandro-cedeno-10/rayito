# Git

`sbx.git` es la API git del SDK de E2B sobre `commands.run`: cada método
construye la orden `git` con los argumentos de E2B y la ejecuta dentro del
sandbox como el usuario del sandbox. `rayito-base` trae `git-core` desde M9
(`git-core-2.50.1-1.amzn2023.0.1`, fijado en `image/Dockerfile`).

!!! note "E2B marca este módulo como obsoleto"
    Rayito lo ofrece por paridad (`m9-e2b-v2-surface`). Nada impide usar
    `sbx.commands.run("git ...")` directamente.

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        sbx.git.clone("https://github.com/octo/demo.git", path="/home/user/demo", depth=1)
        sbx.git.configure_user("Agente", "agente@example.com")
        sbx.files.write("/home/user/demo/NOTAS.md", "hola\n")
        sbx.git.add("/home/user/demo")
        sbx.git.commit("/home/user/demo", "notas")
        status = sbx.git.status("/home/user/demo")
        print(status.current_branch, status.ahead, status.is_clean)
        print(sbx.git.branches("/home/user/demo").branches)
    ```

=== "Python (async)"

    ```python
    import asyncio
    import os

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            repo = "/home/user/demo"
            await sbx.git.clone("https://github.com/octo/demo.git", path=repo, depth=1)
            await sbx.git.create_branch(repo, "cambios")
            await sbx.files.write(f"{repo}/NOTAS.md", "hola\n")
            await sbx.git.add(repo)
            await sbx.git.commit(repo, "notas", author_name="Agente", author_email="agente@example.com")
            # token de vida corta y ámbito mínimo; sólo existe mientras dura el push
            await sbx.git.push(repo, username="x-access-token", password=os.environ["GIT_TOKEN"])
            print((await sbx.git.status(repo)).ahead)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    const repo = "/home/user/demo";
    await sbx.git.clone("https://github.com/octo/demo.git", { path: repo, depth: 1 });
    await sbx.git.configureUser("Agente", "agente@example.com");
    await sbx.git.createBranch(repo, "cambios");
    await sbx.files.write(`${repo}/NOTAS.md`, "hola\n");
    await sbx.git.add(repo);
    await sbx.git.commit(repo, "notas");
    const status = await sbx.git.status(repo);
    console.log(status.currentBranch, status.isClean, status.ahead);
    await sbx.git.push(repo, { username: "x-access-token", password: process.env.GIT_TOKEN ?? "" });
    console.log((await sbx.git.branches(repo)).branches);
    ```

Métodos (los mismos en `Git` y `AsyncGit`; en TypeScript en camelCase):
`clone`, `init`, `remote_add`, `remote_get`, `status`, `branches`,
`create_branch`, `checkout_branch`, `delete_branch`, `add`, `commit`,
`reset`, `restore`, `push`, `pull`, `set_config`, `get_config`,
`dangerously_authenticate` y `configure_user`. Todos aceptan además `envs`,
`user`, `cwd`, `timeout` y `request_timeout`, y fijan `GIT_TERMINAL_PROMPT=0`
(un git que pediría una contraseña falla en vez de colgarse).

## Credenciales

- **`clone`, `push` y `pull` con `username`/`password`**: la URL con
  credenciales sólo existe mientras dura la orden. Tras un `clone` el SDK
  deja el remoto `origin` con la URL sin credenciales (salvo
  `dangerously_store_credentials=True`); en `push`/`pull` cambia la URL del
  remoto, ejecuta la orden y la restaura siempre, también si falló. Un
  `password` sin `username` lanza `InvalidArgumentException` antes de ejecutar
  nada.
- **Las credenciales viajan en la orden** (`StartRequest.cmd`) hasta `rayd`,
  que nunca registra órdenes; el SDK tampoco las registra y las redacta
  (`***`) de `stdout`, `stderr` y del mensaje de un `CommandExitException`.
  Mientras la orden corre, un proceso del propio sandbox podría verlas en
  `/proc`.
- **`dangerously_authenticate(username, password, host="github.com")`**
  guarda las credenciales con `git credential approve` en
  `~/.git-credentials` del usuario del sandbox: **quedan visibles para el
  código del sandbox**, igual que en E2B. Úsalo sólo con un token de ámbito
  mínimo y de vida corta (`SECURITY.md` T9).

## Errores

| Situación | Python | TypeScript |
|---|---|---|
| el remoto pide credenciales | `GitAuthException` (nunca incluye la URL) | `GitAuthError` |
| `push`/`pull` sin rama de seguimiento | `GitUpstreamException` con la orden que falta | `GitUpstreamError` |
| cualquier otro fallo de git | `CommandExitException` (salida redactada si había credenciales) | `CommandExitError` |
| argumentos inválidos | `InvalidArgumentException`, antes de ejecutar nada | `InvalidArgumentError` |

En el shim de E2B, `sbx.git` es el mismo objeto y las excepciones tienen los
nombres de `e2b.exceptions` (`GitAuthException`, `GitUpstreamException`).
