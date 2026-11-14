# Changelog

## [Unreleased]

- TUI: open one file browser directly; Enter opens folders or runs MDA on the highlighted file, without the separate U folder-confirmation step.
- TUI: show primary navigation controls in the footer and put advanced shortcuts behind `?`; show the output-folder field only when relevant.
- TUI: keep processing responsive, prevent overlapping runs, and remember the last browsed folder on exit.
- `--in-place` now shows a diff and asks for confirmation per file (`--yes` skips the prompt; required when stdin is not a terminal). Declined files are reported as `declined` and left untouched.
- In-place writes always back up the original outside the vault: `--backup-dir` → `MDA_BACKUP_DIR` → `~/home/backups-obsidian`, as `<YYYYMMDD>/<vault>/<path>.<sha256[:8]>.bak`. `--backup` is a deprecated no-op; no sibling `.bak` files are created by the CLI any more.
- In-place writes are refused if the source changed since it was read, and the written bytes are verified against the confirmed text.
- TUI: in-place output mode is blocked with a message until it can show the per-file diff.

## [0.2.9]

- TUI: live read-only preview of the highlighted supported file (`.md`, `.txt`, `.pdf`, `.docx`, `.doc` listing).
- Async extraction with 150ms debounce, path+mtime cache, truncation at 10k chars, metadata line (path, ext, size).
- Clear preview on `..`/directories; friendly errors for missing PDF/DOCX extras and legacy `.doc`.
- Toggle preview pane with **I** (`i`); **P** still runs MDA.

## [0.2.8]

- TUI and batch CLI accept `.md`, `.pdf`, `.docx`, `.doc`, and `.txt` (case-insensitive).
- Optional pip extras: `mda-cli[pdf]` (pypdf), `mda-cli[docx]` (python-docx), `mda-cli[documents]` (both).
- `MDA_EXTENSIONS` env overrides the default extension list (comma-separated).
- Text extraction for PDF/DOCX; legacy `.doc` shows a clear convert-to-docx error.
- Folder picker counts all supported types; TUI Kind column shows extension.
- Output: `*.restructured.md` by default; `categorize-vault-notes` uses `*.restructured.txt` for non-Markdown sources.

## [0.2.7]

- Bundle multiple skills under `mda_cli/bundled_skills/` (`markdown-document-architect`, `categorize-vault-notes`).
- Select skill via `--skill`, `MDA_SKILL`, or `MDA_SKILL_DIR` (unchanged override).
- `mda script` subcommand runs helpers from the active skill's `scripts/` directory; `mda --check` lists bundled skills.

## [0.2.5]

- Fix TUI `NoActiveWorker` crash on jump-to-path (`ctrl+j`) and name filter (`f`) by running `push_screen_wait` inside `@work` workers (Textual 8.x).

## [0.2.6]

- Add atomic output writes for batch and TUI processing paths.
- Add optional `--backup` for `--in-place` batch runs.
- Extend JSON-lines/job result reporting with `action`, `changed`, byte counts, and backup-path metadata.

## [0.2.4]

- Bundle Markdown Document Architect skill (`SKILL.md`, `MDA-STANDARD.md`) inside the package so `mda` / TUI work without Claude or Cursor skill installs.
- Skill resolution fallback order ends with packaged `mda_cli/bundled_skill`; `mda --check` reports skill source (`bundled`, `claude`, `cursor`, `env`, `explicit`).

## [0.2.3]

- Fix global `mda` launcher: pin install Python in `~/.config/mda-cli/python`, pipx-aware wrapper (avoids Linuxbrew `python3.14` without `mda_cli`).
- OpenRouter provider: `MDA_PROVIDER`, `MDA_USE_OPENROUTER`, `MDA_OPENROUTER_FALLBACK`, `--provider`, `--check` provider lines.

## [0.2.2]

- TUI-first: `mda` / `mda-cli` with no arguments opens the interactive UI.
- Folder picker before file browser; remembers last folder in user config.
- `mda-cli` console script alias.

## [0.2.1]

- UTF-16 / BOM skill and source reads, prompt cache, retries, TUI improvements, `--check`, `--max-files`, `--json-lines`, vault flatten script.
