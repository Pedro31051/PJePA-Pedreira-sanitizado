"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

function artifactPath(filename) {
  const root = process.env.PJE_AUTOMATION_ARTIFACTS_DIR || path.join(os.tmpdir(), "pjepa-automation-artifacts");
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  return path.join(root, filename);
}

module.exports = { artifactPath };
