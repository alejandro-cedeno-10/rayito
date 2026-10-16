/**
 * Códigos de salida de la CLI, los mismos que los de `rayito.cli` en Python:
 * 0 éxito; 1 la operación falló; 2 error de uso o de entorno (argumentos,
 * sin región, sin credenciales, sin token); 124 el agente cortó un comando
 * por su plazo (el de `timeout(1)`); 130 Ctrl-C (128 más SIGINT).
 */

export const EXIT_OK = 0;
export const EXIT_FAILURE = 1;
export const EXIT_USAGE = 2;
export const EXIT_TIMEOUT = 124;
export const EXIT_INTERRUPTED = 130;
