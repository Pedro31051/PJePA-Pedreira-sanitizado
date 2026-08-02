"use strict";

const fs = require("fs");
const path = require("path");

const configured = (process.env.PJE_BROWSER_CONFIG || "").trim();
const localConfig = path.join(__dirname, "browser_config.local.js");
const target = configured || (fs.existsSync(localConfig) ? localConfig : "");

if (!target) {
  throw new Error(
    "Configuração de navegador ausente. Defina PJE_BROWSER_CONFIG com um " +
      "caminho absoluto ou crie automacoes-pje/browser_config.local.js a " +
      "partir de browser_config.local.example.js."
  );
}

const resolved = path.resolve(target);
if (!fs.existsSync(resolved) || resolved === __filename) {
  throw new Error(`PJE_BROWSER_CONFIG inválido: ${resolved}`);
}

module.exports = require(resolved);
