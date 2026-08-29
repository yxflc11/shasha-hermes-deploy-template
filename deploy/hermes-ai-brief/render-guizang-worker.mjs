#!/usr/bin/env node
/** No-network queue worker for the fixed Playwright renderer sidecar. */

import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const queue = process.env.ACT_BRIEF_RENDER_QUEUE || "/queue";
const jobPattern = /^[0-9]{8}T[0-9]{4}Z-(?:morning|evening)-[a-f0-9]{12}$/;

function writeJsonAtomic(target, payload) {
  const temporary = `${target}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(payload), { mode: 0o600 });
  fs.renameSync(temporary, target);
}

function processJob(jobDir) {
  const request = path.join(jobDir, "request.json");
  const processing = path.join(jobDir, "processing.json");
  const resultPath = path.join(jobDir, "result.json");
  if (fs.existsSync(resultPath)) return;
  if (fs.existsSync(request)) {
    try {
      fs.renameSync(request, processing);
    } catch {
      return;
    }
  }
  if (!fs.existsSync(processing)) return;
  const outputDir = path.join(jobDir, "output");
  fs.mkdirSync(outputDir, { recursive: true, mode: 0o700 });
  const result = spawnSync("node", [
    "/app/render_guizang_brief.mjs",
    path.join(jobDir, "package.json"),
    "/app/template-swiss-card.html",
    outputDir,
  ], {
    encoding: "utf8",
    timeout: 180_000,
    maxBuffer: 1_000_000,
    env: { ...process.env, HOME: "/tmp" },
  });
  if (result.status === 0) {
    try {
      const rendered = JSON.parse(result.stdout);
      writeJsonAtomic(resultPath, { ok: true, files: rendered.files.map((file) => path.basename(file)) });
    } catch (error) {
      writeJsonAtomic(resultPath, { ok: false, error: `invalid renderer output: ${error.message}` });
    }
  } else {
    const detail = String(result.stderr || result.stdout || result.error || "render failed")
      .replace(/\s+/g, " ").slice(0, 300);
    writeJsonAtomic(resultPath, { ok: false, error: detail });
  }
}

function scan() {
  let entries = [];
  try {
    entries = fs.readdirSync(queue, { withFileTypes: true });
  } catch (error) {
    process.stderr.write(`queue unavailable: ${error.message}\n`);
    return;
  }
  for (const entry of entries) {
    if (entry.isDirectory() && jobPattern.test(entry.name)) {
      processJob(path.join(queue, entry.name));
    }
  }
}

scan();
setInterval(scan, 500);
