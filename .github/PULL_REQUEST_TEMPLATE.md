<!-- Título en formato Conventional Commits: feat: / fix: / refactor: / docs: / chore: / test: / ci: -->

## Resumen

<!-- Qué cambia y por qué, en dos o tres frases. -->

## Checklist

- [ ] Cambio OpenSpec enlazado (`openspec/changes/<nombre>/`) y `tasks.md` marcado
- [ ] `buf breaking` limpio, o el cambio incompatible del `.proto` justificado aquí
- [ ] Gates ejecutados en local (`CONTRIBUTING.md` §3) e indicado el sistema: Linux / WSL2 / Windows
- [ ] e2e contra AWS ejecutado (imagen y coste aproximado anotados) o "sin cambio de runtime"
- [ ] Sin secretos, tokens, JWE, `runHookPayload` ni contenido de ficheros en logs, tests o fixtures
- [ ] Documentación actualizada (`ARCHITECTURE.md` / `AWS_API_NOTES.md` si hay un hecho nuevo de la plataforma)
- [ ] `CHANGELOG.md` del paquete tocado actualizado
- [ ] Todos los commits firmados (`git commit -s`, DCO)
