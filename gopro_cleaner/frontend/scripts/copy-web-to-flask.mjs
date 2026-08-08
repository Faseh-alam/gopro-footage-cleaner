/**
 * Copy TanStack Start SPA public output into gopro_cleaner/web for Flask.
 */
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(__dirname, "..");
const repoWeb = join(frontendRoot, "..", "web");

const candidates = [
  join(frontendRoot, "dist", "client"),
  join(frontendRoot, ".output", "public"),
  join(frontendRoot, "dist"),
];

const src = candidates.find((p) => existsSync(p));
if (!src) {
  console.error(
    "No SPA build output found. Tried:\n" + candidates.map((p) => `  - ${p}`).join("\n"),
  );
  process.exit(1);
}

rmSync(repoWeb, { recursive: true, force: true });
mkdirSync(repoWeb, { recursive: true });
cpSync(src, repoWeb, { recursive: true });

function ensureIndexHtml() {
  const indexHtml = join(repoWeb, "index.html");
  const shellHtml = join(repoWeb, "_shell.html");
  if (existsSync(indexHtml)) return "index.html";
  if (existsSync(shellHtml)) {
    cpSync(shellHtml, indexHtml);
    return "_shell.html → index.html";
  }

  // Fallback if prerender did not emit a shell (should be rare with nitro: false).
  const assetsDir = join(repoWeb, "assets");
  if (!existsSync(assetsDir)) {
    console.error("No index.html/_shell.html and no assets/ directory to synthesize from.");
    process.exit(1);
  }
  const files = readdirSync(assetsDir);
  const js = files.find((f) => /^index-.*\.js$/.test(f));
  const css = files.find((f) => /^styles-.*\.css$/.test(f));
  if (!js) {
    console.error("Could not find assets/index-*.js to synthesize index.html.");
    process.exit(1);
  }
  const cssLink = css ? `    <link rel="stylesheet" href="/assets/${css}" />\n` : "";
  writeFileSync(
    indexHtml,
    `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>World Context — Footage Pipeline</title>
${cssLink}    <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  </head>
  <body>
    <script type="module" src="/assets/${js}"></script>
  </body>
</html>
`,
  );
  return "synthesized index.html";
}

const indexNote = ensureIndexHtml();

writeFileSync(
  join(repoWeb, ".build-info.json"),
  JSON.stringify({ source: src, index: indexNote, builtAt: new Date().toISOString() }, null, 2) +
    "\n",
);

console.log(`Copied SPA assets:\n  ${src}\n→ ${repoWeb}\n  index: ${indexNote}`);
