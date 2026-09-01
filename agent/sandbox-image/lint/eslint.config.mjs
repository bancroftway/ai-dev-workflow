// ══════════════════════════════════════════════════════════════════════
// ai-dev-workflow PIPELINE-OWNED lint config -- lives in the sandbox image at /opt/aidw/lint,
// is NEVER written into a target repository, and resolves every plugin from ITS OWN
// node_modules (this directory), never from the repo's. The repo's dependency graph is
// never touched for linting: a root devDependency install once re-resolved a pnpm
// workspace's peer graph, forked drizzle-orm into two incompatible instances, and broke a
// build the pipeline was supposed to protect.
//
// Two consumers:
//   * build gates (a repo that ships its OWN eslint config keeps its own lint contract instead --
//     for BUILDING, this file only applies to repos with no lint setup of their own);
//   * repo_scan.py's `eslint-security` adapter, which runs this config read-only on EVERY repo
//     with a package.json (--no-config-lookup) and keeps only security/* and sonarjs/* findings --
//     a repo's own config governs its style, not whether it gets security-scanned.
// ══════════════════════════════════════════════════════════════════════
import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';
import security from 'eslint-plugin-security';
import sonarjs from 'eslint-plugin-sonarjs';

// All framework plugins ship in this toolchain (image-baked, pinned), so the optional import
// is only a guard against a partial image build -- scoping keeps each plugin's rules on the
// file types it owns.
const optional = async (name) => {
  try {
    return await import(name);
  } catch {
    return null;
  }
};

// Applies a `files` scope to a plugin's own flat-config array. Done by hand rather than with flat
// config's `extends` key, which is only available on newer ESLint 9.x -- this works on every 9.x.
const scoped = (configs, files) => {
  if (!configs) return [];
  const list = Array.isArray(configs) ? configs : [configs];
  return list.map((entry) => ({ ...entry, files }));
};

const reactHooks = await optional('eslint-plugin-react-hooks');
const jsxA11y = await optional('eslint-plugin-jsx-a11y');
const angular = await optional('angular-eslint');
const vue = await optional('eslint-plugin-vue');

const angularPlugin = angular?.default ?? angular;
const jsxA11yFlat = (jsxA11y?.flatConfigs ?? jsxA11y?.default?.flatConfigs)?.recommended;

export default [
  {
    ignores: [
      '**/node_modules/**',
      '**/dist/**',
      '**/build/**',
      '**/out/**',
      '**/.next/**',
      '**/coverage/**',
      '**/*.min.js',
      '**/*.d.ts', // generated declaration files (wrangler, cloudflare-env) are not authored code
      // Non-application paths repo_scan's other tools also skip: pipeline scratch, build caches,
      // vendored payloads, test-run artifacts (mirrors repo_scan._NON_APPLICATION_PATH_RE).
      '**/.angular/**',
      '**/.nuxt/**',
      '**/agent-work/**',
      '**/.ai-dev-workflow/**',
      '**/.venv/**',
      '**/TestResults/**',
      '**/bin/**',
      '**/obj/**',
      '**/.playwright-browsers/**',
      '**/vendor/**',
    ],
  },
  {
    // Without this, flat config declares NO globals and `no-undef` fires on `console`,
    // `process`, `window`, `document` -- every file in a real repo, none of them real defects.
    languageOptions: {
      globals: { ...globals.node, ...globals.browser, ...globals.es2024 },
    },
  },
  js.configs.recommended,
  // `strict` rather than `strictTypeChecked`: the type-aware rulesets need every linted file to
  // resolve inside a tsconfig project graph, and on a brownfield repo one stray file outside it
  // makes ESLint crash instead of report -- a crash is not a quality signal. Type strictness is
  // enforced separately and deterministically by the pipeline's `tsc --noEmit` run.
  ...tseslint.configs.strict,
  security.configs.recommended,
  sonarjs.configs.recommended,

  // ── React ──
  ...(reactHooks
    ? [
        {
          files: ['**/*.{jsx,tsx}'],
          plugins: { 'react-hooks': reactHooks.default ?? reactHooks },
          rules: {
            'react-hooks/rules-of-hooks': 'error',
            'react-hooks/exhaustive-deps': 'error',
          },
        },
      ]
    : []),
  ...(jsxA11yFlat ? scoped([jsxA11yFlat], ['**/*.{jsx,tsx}']) : []),

  // ── Angular ── (ts rules on sources, template rules on component HTML -- angular-eslint ships
  // them as two separate config arrays and expects the caller to supply both file scopes)
  ...(angularPlugin
    ? [
        ...scoped(angularPlugin.configs?.tsRecommended, ['**/*.ts']),
        ...scoped(angularPlugin.configs?.templateRecommended, ['**/*.html']),
      ]
    : []),

  // ── Vue ── (already scoped to .vue by the plugin's own configs)
  ...(vue ? ((vue.default ?? vue).configs?.['flat/recommended'] ?? []) : []),

  {
    // Framework-independent rules worth erroring on: each is a defect class an LLM produces
    // confidently and a human reviewer misses on a large diff.
    rules: {
      eqeqeq: ['error', 'always'],
      'no-console': ['error', { allow: ['warn', 'error'] }],
      'no-eval': 'error',
      'no-implied-eval': 'error',
      'no-new-func': 'error',
      'no-unused-private-class-members': 'error',
      'require-atomic-updates': 'error',
    },
  },
];
