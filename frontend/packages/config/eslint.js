import js from "@eslint/js";
import nextPlugin from "@next/eslint-plugin-next";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

/**
 * The workspace ESLint preset: JavaScript and TypeScript recommended rules, React hooks, jsx-a11y (recommended),
 * and the Next.js rules for the given app directories.
 * @param {{ nextApps?: string[] }} options
 */
export function hyperchat({ nextApps = [] } = {}) {
  return tseslint.config(
    {
      ignores: [
        "**/node_modules/**",
        "**/.next/**",
        "**/next-env.d.ts",
        "**/schema.d.ts",
        "openapi/**",
        "e2e/test-results/**",
        "e2e/playwright-report/**",
      ],
    },
    js.configs.recommended,
    ...tseslint.configs.recommended,
    {
      files: ["**/*.{ts,tsx,js,mjs}"],
      languageOptions: { globals: { ...globals.browser, ...globals.node } },
    },
    {
      files: ["**/*.{tsx,jsx}"],
      plugins: { "jsx-a11y": jsxA11y, "react-hooks": reactHooks },
      languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
      rules: { ...jsxA11y.flatConfigs.recommended.rules, ...reactHooks.configs.recommended.rules },
    },
    ...nextApps.map((dir) => ({
      files: [`${dir}/**/*.{ts,tsx}`],
      plugins: { "@next/next": nextPlugin },
      settings: { next: { rootDir: dir } },
      rules: { ...nextPlugin.configs.recommended.rules, ...nextPlugin.configs["core-web-vitals"].rules },
    })),
  );
}
