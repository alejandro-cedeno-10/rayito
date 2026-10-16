// Delante del Bedrock falso (`fake_upstream.py`): un servidor HTTP/2 en texto
// plano (h2c) que reenvía cada petición por HTTP/1.1 al falso. Los clientes de
// AWS de JavaScript (@langchain/aws, Strands) hablan h2c con un endpoint
// `http://`, que el servidor de Python no entiende.
//
//   node h2c_front.mjs <puerto-de-escucha|0> <puerto-del-falso>
//
// Escribe en la salida estándar el puerto en el que escucha.
import http from "node:http";
import http2 from "node:http2";

const [listenArg, backendArg] = process.argv.slice(2);
const BACKEND_PORT = Number(backendArg);
const BACKEND_HOST = "127.0.0.1";
const HOP_BY_HOP = new Set(["connection", "transfer-encoding", "keep-alive", "upgrade"]);

function plainHeaders(headers) {
  return Object.fromEntries(
    Object.entries(headers).filter(([name]) => !name.startsWith(":") && !HOP_BY_HOP.has(name)),
  );
}

const front = http2.createServer();
front.on("stream", (stream, headers) => {
  const upstream = http.request(
    {
      host: BACKEND_HOST,
      port: BACKEND_PORT,
      method: headers[":method"],
      path: headers[":path"],
      headers: plainHeaders(headers),
    },
    (response) => {
      stream.respond({ ":status": response.statusCode ?? 502, ...plainHeaders(response.headers) });
      response.pipe(stream);
    },
  );
  upstream.on("error", (error) => {
    process.stderr.write(`h2c_front: ${error.message}\n`);
    stream.close(http2.constants.NGHTTP2_INTERNAL_ERROR);
  });
  stream.pipe(upstream);
});

front.listen(Number(listenArg), "0.0.0.0", () => {
  process.stdout.write(`${front.address().port}\n`);
});
