// Fixture para `loadOptionalPeer`: simula un peer que SÍ se resuelve pero
// que, al evaluarse, falla por una dependencia anidada suya que falta (no
// por `specifier` mismo). El módulo importado abajo no existe a propósito.
export * from "./this-nested-dependency-does-not-exist.mjs";
