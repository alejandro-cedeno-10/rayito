<!-- Título en formato Conventional Commits: feat: / fix: / refactor: / docs: / chore: / test: / ci: -->

## Resumen

<!-- Qué cambia y por qué, en dos o tres frases. -->

## Checklist

- [ ] Issue enlazado (`Closes #123`), si lo hay
- [ ] Cambio OpenSpec enlazado (`openspec/changes/<nombre>/`) y `tasks.md` marcado
- [ ] Tests nuevos o actualizados que cubren el cambio (`CONTRIBUTING.md` §6), o el motivo de que no los haya
- [ ] `buf breaking` limpio, o el cambio incompatible del `.proto` justificado aquí
- [ ] Gates ejecutados en local (`CONTRIBUTING.md` §7) e indicado el sistema: Linux / WSL2 / VM Linux en macOS / Windows
- [ ] e2e contra AWS ejecutado (imagen y coste aproximado anotados) o "sin cambio de runtime"
- [ ] Sin secretos, tokens, JWE, `runHookPayload` ni contenido de ficheros en logs, tests o fixtures
- [ ] Documentación actualizada (`ARCHITECTURE.md` / `AWS_API_NOTES.md` si hay un hecho nuevo de la plataforma)
- [ ] `CHANGELOG.md` del paquete tocado actualizado
- [ ] Todos los commits con DCO y firmados (`git commit -s -S`)
