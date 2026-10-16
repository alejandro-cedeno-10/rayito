/** Códigos de estado HTTP que usan el panel y el servidor MCP. Una sola definición. */
export const HTTP_OK = 200 as const;
export const HTTP_ACCEPTED = 202 as const;
export const HTTP_SEE_OTHER = 303 as const;
export const HTTP_BAD_REQUEST = 400 as const;
export const HTTP_UNAUTHORIZED = 401 as const;
export const HTTP_FORBIDDEN = 403 as const;
export const HTTP_NOT_FOUND = 404 as const;
export const HTTP_METHOD_NOT_ALLOWED = 405 as const;
export const HTTP_PAYLOAD_TOO_LARGE = 413 as const;
export const HTTP_UNSUPPORTED_MEDIA_TYPE = 415 as const;
export const HTTP_MISDIRECTED = 421 as const;
export const HTTP_TOO_MANY_REQUESTS = 429 as const;
export const HTTP_INTERNAL_ERROR = 500 as const;
export const HTTP_BAD_GATEWAY = 502 as const;
export const HTTP_SERVICE_UNAVAILABLE = 503 as const;
