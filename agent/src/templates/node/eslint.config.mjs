// ══════════════════════════════════════════════════════════════════════
// DO NOT MODIFY THIS FILE DURING FEATURE WORK
// aidw-template-version: 1
// ══════════════════════════════════════════════════════════════════════
// Written by ai-dev-workflow as this repo's shared quality/security baseline, and intentionally
// immutable during feature development -- the same policy as .NET's Directory.Build.props.
// Any modification requires:
//   1. A dedicated maintenance PR (not part of a feature branch)
//   2. Explicit justification in the PR description
//   3. A full lint + typecheck + test pass before merge
//
// Every rule here is enforced as a build failure: the pipeline runs
// `eslint . --max-warnings=0` alongside a strict `tsc --noEmit`, so a warning is an error and
// generated code that trips one cannot be reported as finished.
// ══════════════════════════════════════════════════════════════════════
import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';
import security from 'eslint-plugin-security';
import sonarjs from 'eslint-plugin-sonarjs';

// Framework plugins are installed only for the frameworks this repo actually uses (ai-dev-workflow
// installs them from its detected tech stack), so each is imported optionally: a missing plugin
// means "this repo is not a React/Angular/Vue repo", not a broken config.
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
