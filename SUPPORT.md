# Soporte

Rayito es un proyecto de código abierto mantenido por voluntarios
(`GOVERNANCE.md`): no hay soporte comercial ni SLA. Estas son las vías, por
orden.

1. **Documentación**: <https://alejandro-cedeno-10.github.io/rayito/>, en
   especial [Solución de problemas](https://alejandro-cedeno-10.github.io/rayito/operacion/solucion-de-problemas/)
   y las [FAQ](https://alejandro-cedeno-10.github.io/rayito/operacion/faq/).
2. **Diagnóstico de tu cuenta**: `pip install "rayito[cli]" && rayito doctor`
   comprueba credenciales, cuotas, IAM, bucket, imagen y la compatibilidad
   entre tu SDK y el `rayd` de la imagen.
3. **Issues**: <https://github.com/alejandro-cedeno-10/rayito/issues>, con la
   plantilla de bug o de propuesta. Incluye SDK y versión, `agent_version`
   (`get_health()` / `getHealth()`), imagen y versión, región y una
   reproducción mínima; **nunca** tokens, JWE ni `runHookPayload`. Busca
   antes entre los issues abiertos y cerrados.
4. **Vulnerabilidades**: nunca en un issue público; sigue `SECURITY.md`
   ("Reportar una vulnerabilidad").

Sólo la última línea `MAJOR.MINOR` recibe correcciones (`SECURITY.md`,
"Versiones soportadas"); si usas una anterior, actualiza antes de reportar.
Para contribuir un arreglo, `CONTRIBUTING.md`.

Issues and questions in English are welcome too.
