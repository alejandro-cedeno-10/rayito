"""Eco de WebSocket mínimo para los e2e de dominio propio (Python y TypeScript).

Sólo usa `socket` (no instala nada en el sandbox): lo justo para comprobar de
punta a punta que la distribución deja pasar un upgrade, no un servidor de
WebSocket real. Uso: `python3 websocket_echo_server.py <puerto> <dirección>`;
la dirección de escucha la decide quien lo lanza (los e2e, la de
`python3 -m http.server`; el test unitario, loopback).

Atiende una conexión tras otra (un apretón de manos que llegó por un servidor
del edge sin la ruta se reintenta) y lee cabeceras y trama hasta tenerlas
enteras. El script se sube al sandbox con la API de ficheros, nunca citado en
la shell. Una conexión que cierra antes de mandar la trama (`IndexError` o
`OSError`) no mata el bucle.
"""

import base64
import hashlib
import socket
import sys

#: GUID fijo de RFC 6455 §1.3, para calcular `Sec-WebSocket-Accept`.
WEBSOCKET_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
READ_CHUNK_BYTES = 4096
LISTEN_BACKLOG = 8
#: Cabecera de una trama cliente->servidor de menos de 126 bytes: 2 bytes más la máscara de 4.
MASKED_FRAME_HEADER_BYTES = 6
MASK_BYTES = 4
LENGTH_MASK = 0x7F
FINAL_TEXT_FRAME = 0x81


def read_until(conn, done):
    data = b""
    while not done(data):
        chunk = conn.recv(READ_CHUNK_BYTES)
        if not chunk:
            break
        data += chunk
    return data


def frame_done(data):
    return len(data) >= MASKED_FRAME_HEADER_BYTES and len(data) >= (
        MASKED_FRAME_HEADER_BYTES + (data[1] & LENGTH_MASK)
    )


def serve(conn):
    data = read_until(conn, lambda received: b"\r\n\r\n" in received)
    head, _, rest = data.partition(b"\r\n\r\n")
    key = None
    for line in head.decode("latin1").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    if key is None:
        return
    digest = hashlib.sha1((key + WEBSOCKET_ACCEPT_GUID).encode()).digest()
    accept = base64.b64encode(digest).decode()
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    frame = rest + read_until(conn, lambda received: frame_done(rest + received))
    length = frame[1] & LENGTH_MASK
    mask = frame[2 : 2 + MASK_BYTES]
    body = frame[MASKED_FRAME_HEADER_BYTES : MASKED_FRAME_HEADER_BYTES + length]
    payload = bytes(byte ^ mask[index % MASK_BYTES] for index, byte in enumerate(body))
    conn.sendall(bytes([FINAL_TEXT_FRAME, len(payload)]) + payload)


def main(port, address):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((address, port))
    server.listen(LISTEN_BACKLOG)
    while True:
        conn, _ = server.accept()
        try:
            serve(conn)
        except (OSError, IndexError):
            pass
        finally:
            conn.close()


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2])
