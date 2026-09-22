# Changelog

Rayito se publica por componente, cada uno con su propio changelog en formato
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y su propio
prefijo de tag:

| Componente | Changelog | Tag |
|---|---|---|
| SDK Python (`rayito` en PyPI) | [`clients/python/CHANGELOG.md`](clients/python/CHANGELOG.md) | `python-v<versión>` |
| SDK TypeScript (`rayito` en npm) | [`clients/typescript/CHANGELOG.md`](clients/typescript/CHANGELOG.md) | `typescript-v<versión>` |
| Agente `rayd` (binario en la imagen y asset de la GitHub Release) | [`crates/rayd/CHANGELOG.md`](crates/rayd/CHANGELOG.md) | `rayd-v<versión>` |

Las versiones de la imagen `rayito-base` no llevan tag: son números de build
opacos que asigna AWS (`create-microvm-image` / `update-microvm-image`) y se
anotan en `MILESTONES.md` junto a la aceptación de cada hito. Los pasos de
publicación están en [`docs/RELEASING.md`](docs/RELEASING.md).
