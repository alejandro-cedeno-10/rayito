const express = require("express");
const app = express();
app.get("/health", (_q, r) => r.send("ok"));
