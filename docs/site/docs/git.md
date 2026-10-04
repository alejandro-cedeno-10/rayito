# Git

`sbx.git` es la API git del SDK de E2B sobre `commands.run`: cada método
construye la orden `git` con los argumentos de E2B y la ejecuta dentro del
sandbox como el usuario del sandbox. `rayito-base` trae `git-core` desde 0.3.0
(`git-core-2.50.1-1.amzn2023.0.1`, fijado en `image/Dockerfile`).

!!! note "E2B marca este módulo como obsoleto"
    Rayito lo ofrece por paridad. Nada impide usar
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
            # token de vida corta y de un solo repo: el código del sandbox puede verlo
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

!!! warning "Trata el token como revelado al código del sandbox"
    Un `username`/`password` que pasas a `clone`, `push` o `pull` está al
    alcance de cualquier código que ya haya corrido en ese sandbox con el
    mismo usuario: puede haber dejado preparado su `~/.bash_profile` (las
    órdenes corren con un shell de login), su configuración de git o un
    proceso que mire `/proc`. Usa sólo tokens de vida corta, de un solo
    repositorio y con el mínimo permiso (por ejemplo, un token de
    instalación de una GitHub App), nunca un token personal de larga
    duración en un sandbox que ejecuta código no confiable. Es la misma
    regla que para los [secretos inyectados](secrets.md).

- **`clone`, `push` y `pull` con `username`/`password`**: la URL con
  credenciales está en `.git/config` mientras dura la orden. Tras un `clone`
  el SDK deja el remoto `origin` con la URL sin credenciales (salvo
  `dangerously_store_credentials=True`); en `push`/`pull` cambia la URL del
  remoto, ejecuta la orden y la restaura siempre, también si la orden falla
  o vence. Si la restauración no se puede hacer (el sandbox se pausa, se
  corta el stream), el logger `rayito.git` (en TypeScript, el `logger` del
  sandbox) avisa sin la URL de que el token puede seguir en `.git/config`,
  y viajaría en un snapshot o en `persist=`. Un `password` sin `username`
  lanza `InvalidArgumentException` antes de ejecutar nada.
- **Endurecimiento**: con credenciales, la orden de git corre sin hooks
  (`-c core.hooksPath=/dev/null`) y sin credential helpers
  (`-c credential.helper=`, para que un helper configurado no guarde el
  token en disco), y antes de enviar nada el SDK comprueba que la
  configuración de git no reescribe URLs (`url.*.insteadOf`); si lo hace,
  lanza `GitAuthException` sin haber enviado las credenciales.
- **Las credenciales viajan en la orden** (`StartRequest.cmd`) hasta `rayd`,
  que nunca registra órdenes; el SDK tampoco las registra y las redacta
  (`***`) de `stdout`, `stderr` y del mensaje de un `CommandExitException`.
- **`dangerously_authenticate(username, password, host="github.com")`**
  guarda las credenciales con `git credential approve` en
  `~/.git-credentials` del usuario del sandbox: **quedan visibles para el
  código del sandbox**, igual que en E2B. Úsalo sólo con un token de ámbito
  mínimo y de vida corta ([Seguridad](security.md)).

## Errores

| Situación | Python | TypeScript |
|---|---|---|
| el remoto pide credenciales | `GitAuthException` (nunca incluye la URL) | `GitAuthError` |
| la configuración de git reescribe URLs (`url.*.insteadOf`) y la llamada lleva credenciales | `GitAuthException`, sin enviarlas | `GitAuthError` |
| `push`/`pull` sin rama de seguimiento | `GitUpstreamException` con la orden que falta | `GitUpstreamError` |
| cualquier otro fallo de git | `CommandExitException` (salida redactada si había credenciales) | `CommandExitError` |
| argumentos inválidos | `InvalidArgumentException`, antes de ejecutar nada | `InvalidArgumentError` |

En el shim de E2B, `sbx.git` es el mismo objeto y las excepciones tienen los
nombres de `e2b.exceptions` (`GitAuthException`, `GitUpstreamException`).
