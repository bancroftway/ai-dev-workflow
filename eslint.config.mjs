import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // The Python agent's tree (sandbox-image JS payloads, vendored plugins) is not frontend
    // code -- linting it turned the CI `lint` gate red on files this config was never meant for.
    "agent/**",
  ]),
]);

export default eslintConfig;
