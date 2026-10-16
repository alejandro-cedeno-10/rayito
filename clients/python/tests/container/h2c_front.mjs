// Delante del Bedrock falso (HTTP/1.1): atiende HTTP/2 en claro, que es lo que
// el SDK de AWS para JavaScript usa con un endpoint http://, y reenvía cada
// petición por HTTP/1.1 sin bufferizar la respuesta.
import http from "node:http";
import http2 from "node:http2";

const [listenPort, upstreamPort] = process.argv.slice(2).map(Number);

const front = http2.createServer({ allowHTTP1: true }, (req, res) => {
  const headers = { ...req.headers };
  for (const name of Object.keys(headers)) {
    if (name.startsWith(":")) {
      delete headers[name];
    }
  }
  delete headers.connection;
  delete headers["transfer-encoding"];
  const upstream = http.request(
    { host: "127.0.0.1", port: upstreamPort, method: req.method, path: req.url, headers },
    (answer) => {
      const outgoing = { ...answer.headers };
      delete outgoing.connection;
      delete outgoing["transfer-encoding"];
      res.writeHead(answer.statusCode ?? 502, outgoing);
      answer.pipe(res);
    },
  );
  upstream.on("error", () => {
    res.writeHead(502);
    res.end();
  });
  req.pipe(upstream);
});

front.listen(listenPort, "0.0.0.0", () => process.stdout.write("ready\n"));
