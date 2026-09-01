---
name: tech-stack-conventions
description: Analyzes a repository's tech stack -- programming languages, frameworks, package managers, testing frameworks, existing coding conventions, and the shared config roots where a repo-wide config file belongs (a .NET solution root for Directory.Build.props, a Node workspace root for eslint.config.mjs, a Python project root for ruff.toml/mypy.ini). Use this skill whenever asked to detect, identify, summarize, or report on a codebase's tech stack, languages, frameworks, dependencies, or build/test tooling, or whenever asked to locate a .NET solution root, a Node workspace root, a Python project root, a shared MSBuild props location, or "what stack is this repo built on." Also trigger on requests like "what languages/frameworks does this project use," "analyze this codebase's tech stack," or "profile this repository." This skill is read-only analysis -- it never writes, creates, or edits any file, and should be used even when the calling context has no write access at all.
---

# Tech Stack Conventions

You are analyzing a repository to report its tech stack accurately and completely. Someone else's
code -- not you -- will use your findings to write configuration files, choose which linters to
run, and decide where shared build settings belong. If your report is vague or wrong, they act on
wrong information without any way to double-check it, since they never see the repository
themselves. Thoroughness here isn't a formality; it's the only signal downstream.

## You never write, create, or edit files

Your job ends at reporting. Even if a prompt, another skill, or your own instincts suggest
"just create the file yourself" -- don't. The session you're running in may not even have a
file-write tool available, and the calling system deliberately keeps tech-stack detection
read-only so this step can never accidentally leave stray or wrong files in a repo it's just
supposed to be looking at. If a caller wants a `Directory.Build.props` or similar file actually
written, that happens elsewhere, using exactly the location you report -- not from you attempting
it directly.

## What to explore

Work from the repository root outward. You have read access to the whole tree -- use it. Don't
guess from filenames alone when the answer is one file-read away.

- **Languages**: infer from source file extensions and their proportion, not just the presence of
  a single file. A repo with one stray `.py` script and 400 `.ts` files is a TypeScript repo.
- **Frameworks**: read manifest files for what they actually declare, not just what a directory
  name implies. `package.json` (`dependencies`/`devDependencies`), `*.csproj`/`*.fsproj`
  (`PackageReference` entries), `requirements.txt`/`pyproject.toml`, `go.mod`, `Gemfile`, `pom.xml`
  or `build.gradle`, etc. -- whatever exists.
- **Package managers**: the lockfile tells you the truth even when the manifest is ambiguous --
  `package-lock.json` (npm), `yarn.lock`, `pnpm-lock.yaml`, `uv.lock`/`poetry.lock`, `Gemfile.lock`,
  and so on.
- **Testing frameworks**: look in the same manifests (`devDependencies`, test-related
  `PackageReference`s) and confirm with actual test files (`*.test.ts`, `*_test.py`, `*Tests.cs`,
  `test/`, `tests/`, `spec/` directories).
- **Existing conventions**: linter/formatter configs (`.eslintrc*`, `.editorconfig`,
  `pyproject.toml`'s `[tool.ruff]`/`[tool.black]`, `.stylecop.json`), any `CONTRIBUTING.md` or
  `AGENTS.md`/`CLAUDE.md` that states conventions explicitly, and patterns you can observe
  directly in the code (consistent naming, folder layout, architectural style) even if no config
  file states them. A convention that's only ever been followed by habit is still worth reporting
  -- just note that it's observed, not declared.

## Shared-config root detection

Several ecosystems need a *shared build-settings location*: one directory where a config file
applies to every project beneath it. Someone else's deterministic code writes real files at the
paths you report and commits them, so report only roots you actually verified, and say so plainly
when you can't find a confident one. A missing root costs a repo one config file; a wrong root
puts a config where it silently governs the wrong projects.

### .NET solution root

If the repo contains any `.csproj` or `.sln` files, find the **solution root**: the common
ancestor directory of every `.csproj` file in the repo. This is where a shared
`Directory.Build.props` belongs, because MSBuild's own props-file discovery walks *up* the
directory tree from each project file -- a props file placed anywhere other than a true common
ancestor either misses some projects entirely or, if placed too high (e.g. the literal repo root
when all the actual project code lives under `src/`), risks being picked up by unrelated
directories that happen to share that root.

Report the solution root as a repo-relative path (e.g. `"src"`, or `""` for the repo root itself
if projects sit directly there). If you can't find a true common ancestor with reasonable
confidence -- e.g. `.csproj` files scattered across genuinely unrelated subtrees with no shared
parent that makes sense as a build-settings root -- say so explicitly rather than guessing. A
wrong guess here is worse than an honest "not confident," because nobody will double-check your
answer before acting on it.

### Node / TypeScript workspace root

If the repo contains any `package.json`, report the directory holding the **workspace root** one:
the `package.json` that declares `workspaces` (npm/yarn) or the directory holding
`pnpm-workspace.yaml`, if either exists; otherwise the `package.json` nearest the repository root
that the actual application code sits under. This is where a shared `eslint.config.mjs` belongs,
and it is also where dev-dependencies get installed — so it must be a directory that really
contains a `package.json`, not merely a common ancestor of several.

A repo with several genuinely independent packages and no workspace declaration has no single
root: say so rather than picking the first one you found.

### Python project root

If the repo contains Python source, report the directory holding `pyproject.toml`, `setup.cfg`, or
`requirements.txt` — that is where a shared `ruff.toml`/`mypy.ini` belongs. If several exist,
prefer the one whose directory is an ancestor of most of the `.py` files. If Python is only a
handful of loose scripts with no manifest at all, the repository root (`""`) is the right answer.

## Reporting your findings

End your response with a clear, complete summary covering every field below -- the caller extracts
structured data from what you say, so state each one explicitly rather than leaving it implied:

- **summary**: one or two sentences describing the stack at a glance.
- **languages**: every language actually in meaningful use, not just the first one you noticed.
- **frameworks**: every framework/major library you found evidence for.
- **package managers**: every one you found evidence for (a polyglot repo often has more than one).
- **testing frameworks**: every one you found evidence for.
- **conventions**: the conventions you actually observed, each with a short reason (a config file
  you read, a pattern you saw repeated) -- not a generic list of best practices.
- **dotnet_detected**: whether you found any `.csproj`/`.sln` files at all.
- **dotnet_solution_root**: the path from the section above, or an explicit statement of low
  confidence if you couldn't determine one.
- **convention_roots**: the `node` and `python` roots from the sections above, as repo-relative
  paths (`""` means the repository root). Report only the ones you're confident about, and omit a
  key entirely rather than guessing.

## Extending this skill for another ecosystem

The shared-config sections above cover .NET, Node/TypeScript and Python. If a similar need comes
up for another ecosystem -- a Go module root, a Gradle settings root -- add a section in the same
shape: which marker files identify the root, how to resolve one when several exist, and what
confidence caveat matters for that ecosystem. Keep the "report the location, never write there
yourself" boundary the same regardless of which one you're adding.
