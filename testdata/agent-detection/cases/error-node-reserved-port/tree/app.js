const app = express();
app.get("/health", (_q, r) => r.send("ok"));
app.listen(8080);
