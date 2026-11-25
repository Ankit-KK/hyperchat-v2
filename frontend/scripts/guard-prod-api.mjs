// Fails when any frontend source or non-production environment file names the production API host
// (frontend/README.md: no development or preview build may reference it). Production env files (.env.production*)
// and Markdown docs are exempt. Run: node scripts/guard-prod-api.mjs [root]
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

export const FORBIDDEN = ["api", "hyperchat", "space"].join("."); // assembled, so this file does not trip itself
const SKIP_DIRS = new Set([
  "node_modules",
  ".next",
  ".turbo",
  "coverage",
  "test-results",
  "playwright-report",
]);
const SOURCE = /\.(ts|tsx|js|jsx|mjs|cjs|json|css|ya?ml|html)$/;

export function isChecked(name) {
  if (name.startsWith(".env")) return !name.startsWith(".env.production");
  return SOURCE.test(name);
}

/** Every "path:line" under root that names the forbidden host. */
export function scan(root) {
  const hits = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) {
        if (!SKIP_DIRS.has(entry.name)) walk(path);
      } else if (isChecked(entry.name)) {
        readFileSync(path, "utf8")
          .split("\n")
          .forEach((line, i) => {
            if (line.toLowerCase().includes(FORBIDDEN)) hits.push(`${relative(root, path)}:${i + 1}`);
          });
      }
    }
  };
  walk(root);
  return hits;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const root = process.argv[2] ?? fileURLToPath(new URL("..", import.meta.url));
  const hits = scan(root);
  if (hits.length) {
    console.error(`${FORBIDDEN} must not appear outside production configuration:\n  ${hits.join("\n  ")}`);
    process.exit(1);
  }
  console.log(`guard: no reference to ${FORBIDDEN} in sources or non-production env files`);
}
