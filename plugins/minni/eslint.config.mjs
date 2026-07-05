// Flat ESLint config for minni-multi-plugin.
//
// Scope is deliberately conservative: lint `src/**/*.ts` and `tests/**/*.mjs`
// without type-checked rules (no parserServices/project), so the gate stays
// fast and does not mass-fail on a codebase that predates the lint config.
// Noisy rules from the recommended sets are downgraded to "warn" so they
// surface without failing `npm run lint`; only hard errors gate.
import eslint from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

export default tseslint.config(
  {
    ignores: [
      "dist/**",
      "tests/.compiled/**",
      // frontend/** is the built Vite bundle (minified app.js etc.), a
      // generated artifact — linting it is meaningless. The real frontend
      // SOURCE under frontend-src/** IS linted (see the config block below).
      "frontend/**",
      "node_modules/**",
      "skills/**",
      "**/*.d.ts",
    ],
  },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.ts"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: globals.node,
    },
    rules: {
      // The plugin intentionally widens to `any` at typed boundaries that
      // cross into the untyped daemon RPC surface; warn, don't block.
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      "@typescript-eslint/no-empty-object-type": "off",
      "@typescript-eslint/no-wrapper-object-types": "off",
    },
  },
  {
    // Frontend React/TS source (compiled separately by Vite). Browser globals,
    // and the same conservative warn-not-error posture as src/** so the gate
    // surfaces issues without mass-failing on a pre-existing UI codebase.
    files: ["frontend-src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: globals.browser,
    },
    rules: {
      // Rules of Hooks is a real correctness gate; exhaustive-deps stays a warn
      // (the source already opts out in one place with a disable directive).
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    files: ["tests/**/*.mjs"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.node },
    },
    rules: {
      "no-unused-vars": "off",
      "no-empty-pattern": "off",
      "@typescript-eslint/no-unused-vars": "off",
    },
  },
  {
    // Global downgrades for pre-existing patterns across the codebase.
    // These keep `npm run lint` green (warnings only) without a sweeping
    // refactor: empty catch blocks, intentional useless-assignment guards,
    // ANSI escape regexes, and caught-error rethrow choices are all
    // deliberate here. Prefer-const stays as a warn so it surfaces.
    rules: {
      "no-empty": "off",
      "no-control-regex": "off",
      "no-useless-assignment": "off",
      "no-useless-escape": "off",
      "prefer-const": "warn",
      "preserve-caught-error": "off",
    },
  },
);
