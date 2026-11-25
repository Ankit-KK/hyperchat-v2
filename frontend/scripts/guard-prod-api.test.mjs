import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { FORBIDDEN, isChecked, scan } from "./guard-prod-api.mjs";

test("a planted reference in a source file or a non-production env file is caught", () => {
  const root = mkdtempSync(join(tmpdir(), "guard-"));
  mkdirSync(join(root, "apps", "web", "lib"), { recursive: true });
  mkdirSync(join(root, "node_modules", "x"), { recursive: true });
  writeFileSync(
    join(root, "apps", "web", "lib", "api.ts"),
    `ok\nconst u = "https://${FORBIDDEN.toUpperCase()}";\n`,
  );
  writeFileSync(join(root, "apps", "web", ".env.preview"), `NEXT_PUBLIC_API_BASE_URL=https://${FORBIDDEN}\n`);
  writeFileSync(
    join(root, "apps", "web", ".env.production"),
    `NEXT_PUBLIC_API_BASE_URL=https://${FORBIDDEN}\n`,
  );
  writeFileSync(join(root, "README.md"), `production uses ${FORBIDDEN}\n`);
  writeFileSync(join(root, "node_modules", "x", "i.js"), FORBIDDEN);
  assert.deepEqual(scan(root).sort(), [
    join("apps", "web", ".env.preview") + ":1",
    join("apps", "web", "lib", "api.ts") + ":2",
  ]);
});

test("which files are checked", () => {
  for (const name of [".env", ".env.local", ".env.development", ".env.example", "a.tsx", "b.json", "c.css"]) {
    assert.equal(isChecked(name), true, name);
  }
  for (const name of [".env.production", ".env.production.local", "README.md", "logo.png"]) {
    assert.equal(isChecked(name), false, name);
  }
});
