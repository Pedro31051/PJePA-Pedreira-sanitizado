"use strict";

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const ignored = new Set(["browser_config.local.js"]);

function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "node_modules" ? [] : walk(full);
    return entry.name.endsWith(".js") && !ignored.has(entry.name) ? [full] : [];
  });
}

for (const file of walk(root)) {
  execFileSync(process.execPath, ["--check", file], { stdio: "inherit" });
}

console.log("Sintaxe JavaScript validada.");
