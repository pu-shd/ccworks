import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default [
  { ignores: ["node_modules/**", ".venv/**"] },
  {
    files: ["tests/*.mjs"],
    ...js.configs.recommended,
    languageOptions: { globals: globals.node },
  },
  {
    files: [".pi/extensions/*.ts"],
    languageOptions: { parser: tseslint.parser, globals: globals.node },
    rules: {
      ...js.configs.recommended.rules,
      "no-unused-vars": ["error", { args: "none", caughtErrors: "none" }],
    },
  },
];
