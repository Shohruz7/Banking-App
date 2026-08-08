import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";

export default tseslint.config(
  { ignores: ["dist", "src/api/schema.d.ts", "coverage"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],

      // ADR-0009, enforced rather than remembered. `Number(balance)` is the one-character mistake
      // that silently discards the guarantee the whole ledger is built on, and it will not fail a
      // test that is not looking for it. `toChartNumber` in money.ts is the sanctioned exception,
      // and it is exempted by file below.
      "no-restricted-globals": [
        "error",
        { name: "parseFloat", message: "Money is a decimal string — see src/money.ts (ADR-0009)." },
        { name: "parseInt", message: "Money is a decimal string — see src/money.ts (ADR-0009)." },
      ],
      "no-restricted-syntax": [
        "error",
        {
          selector: "CallExpression[callee.name='Number']",
          message:
            "Do not convert money to a number. Use the helpers in src/money.ts; the only float " +
            "conversion allowed is toChartNumber, at the charting boundary (ADR-0009).",
        },
      ],
    },
  },
  {
    // The module that owns the rule is allowed to be the one place that breaks it.
    files: ["src/money.ts", "src/money.test.ts"],
    rules: { "no-restricted-syntax": "off" },
  },
  {
    // A context provider and the hook that reads it belong in one file — splitting them to satisfy
    // a fast-refresh heuristic would scatter each context across two modules for a dev-server
    // convenience. Same for `ui.tsx`, whose `cx` helper lives with the components that use it.
    files: [
      "src/auth/AuthProvider.tsx",
      "src/components/Toaster.tsx",
      "src/components/ui.tsx",
      "src/realtime/useStream.tsx",
    ],
    rules: { "react-refresh/only-export-components": "off" },
  },
);
