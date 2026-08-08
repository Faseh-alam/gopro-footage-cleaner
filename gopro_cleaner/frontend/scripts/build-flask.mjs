/**
 * Production UI build for Flask: vite build + copy into gopro_cleaner/web.
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(__dirname, "..");

const build = spawnSync("npx", ["vite", "build"], {
  cwd: frontendRoot,
  stdio: "inherit",
  shell: true,
  env: process.env,
});

const hasClient =
  existsSync(join(frontendRoot, "dist", "client")) ||
  existsSync(join(frontendRoot, ".output", "public", "assets"));

if (!hasClient) {
  process.exit(build.status === 0 ? 1 : build.status ?? 1);
}

const copy = spawnSync(process.execPath, [join(__dirname, "copy-web-to-flask.mjs")], {
  cwd: frontendRoot,
  stdio: "inherit",
  env: process.env,
});

if (copy.status !== 0) {
  process.exit(copy.status ?? 1);
}

if (build.status !== 0) {
  console.warn(
    "vite build exited non-zero, but client assets were copied. Check logs above if the UI misbehaves.",
  );
}

process.exit(0);
