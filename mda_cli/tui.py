"""Textual TUI: open folders and run MDA on a file with Enter."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, TextArea
from textual.widgets.data_table import RowKey

from mda_cli.config import resolve_initial_folder, set_last_folder
from mda_cli.core import Job, find_skill_dir, jobs_from_document_files, load_system_prompt
from mda_cli.document_io import (
    DocumentReadError,
    is_supported_extension,
    read_document_text,
    supported_extensions,
)
from mda_cli.preview import (
    DEFAULT_PREVIEW_MAX_CHARS,
    format_preview_metadata,
    truncate_preview_text,
)
from mda_cli.providers import (
    build_restructure_client,
    ensure_api_credentials,
    process_job_with_provider,
    resolve_provider_name,
)


def _is_windows_hidden(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(os.fspath(path))
        if attrs == -1:
            return False
        return bool(attrs & 2)
    except Exception:
        return False


class JumpPathScreen(ModalScreen[Path | None]):
    """Enter a filesystem path to jump the listing to."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def compose(self) -> ComposeResult:
        yield Label("Directory path (Enter = go, Esc = cancel):")
        yield Input(placeholder="C:\\path or ~/projects/foo", id="jump-in")

    def on_mount(self) -> None:
        self.query_one("#jump-in", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "jump-in":
            return
        raw = event.value.strip()
        if not raw:
            self.dismiss(None)
            return
        p = Path(raw).expanduser()
        if not p.exists():
            self.dismiss(None)
            return
        if p.is_file():
            p = p.parent
        self.dismiss(p.resolve())


class FilterScreen(ModalScreen[str | None]):
    """Filter listing by substring (case-insensitive); empty clears filter."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, initial: str) -> None:
        super().__init__()
        self.initial = initial

    def compose(self) -> ComposeResult:
        yield Label("Name filter (Enter = apply, Esc = cancel):")
        yield Input(placeholder="substring…", id="filter-in")

    def on_mount(self) -> None:
        inp = self.query_one("#filter-in", Input)
        inp.value = self.initial
        inp.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "filter-in":
            return
        self.dismiss(event.value.strip().lower())


class DocumentTable(DataTable):
    """Keep Enter activation separate from mouse selection and preview."""

    BINDINGS = [
        Binding("enter", "open_or_run", "Open/run"),
        Binding("backspace", "app.go_up", "Up"),
        Binding("space", "app.toggle_select", "Select", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("j", "cursor_down", "Down", show=False),
    ]

    class Activated(Message):
        """Request activation of the row on which Enter was pressed."""

        def __init__(self, table: DocumentTable, row_key: RowKey) -> None:
            self.table = table
            self.row_key = row_key
            super().__init__()

    def action_open_or_run(self) -> None:
        if self.row_count:
            row_key = self.coordinate_to_cell_key(self.cursor_coordinate).row_key
            self.post_message(self.Activated(self, row_key))


class NavigationHelpScreen(ModalScreen[None]):
    """Keep less common controls out of the normal browsing flow."""

    DEFAULT_CSS = """
    NavigationHelpScreen { align: center middle; }
    NavigationHelpScreen > Vertical {
        width: 74; max-width: 95%; height: auto; max-height: 95%;
        border: heavy $primary; background: $surface; padding: 1 2;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("escape,enter,question_mark,q", "close", "Back", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[bold]Navigation and more options[/]")
            yield Label(
                "Enter       Open a folder or run the highlighted file\n"
                "Backspace   Go to the parent folder\n"
                "Space       Add/remove a file from the batch selection\n"
                "P           Run the selected batch\n"
                "A           Select supported files recursively\n"
                "F           Filter names\n"
                "V           Show/hide hidden files\n"
                "H           Go home\n"
                "Ctrl+J      Jump to a path\n"
                "I           Show/hide the preview\n"
                "O           Change output mode\n"
                "G           Choose an output folder\n\n"
                "Enter always runs only the highlighted file.\n"
                "Default output: a separate file next to the source.\n"
                "In-place changes require diff approval in the terminal.\n\n"
                "[dim]Enter or Esc to return[/]"
            )

    def action_close(self) -> None:
        self.dismiss(None)


class MdaNavigatorApp(App[None]):
    """One file browser: Enter opens folders or processes the highlighted file."""

    CSS = """
    #main { height: 100%; }
    #left { width: 38%; height: 100%; border: heavy $primary; padding: 0 1; }
    #preview-col { width: 32%; height: 100%; border: heavy $primary; padding: 0 1; }
    #preview-col.hidden { display: none; }
    #right { width: 1fr; height: 100%; border: heavy $primary; padding: 0 1; }
    #listing { height: 1fr; min-height: 10; }
    #preview-meta { height: auto; max-height: 3; text-style: bold; }
    #preview-body { height: 1fr; min-height: 8; }
    #log { height: 1fr; min-height: 10; background: $surface; }
    Label { margin: 0 0 1 0; }
    """

    BINDINGS = [
        Binding("question_mark", "show_help", "More", key_display="?"),
        Binding("q", "quit", "Quit", show=True),
        Binding("escape", "quit", "Quit", show=False),
        Binding("o", "cycle_output", "Output mode", show=False),
        Binding("i", "toggle_preview", "Preview", show=False),
        Binding("p", "run_mda", "Run selected", show=False),
        Binding("g", "focus_out_dir", "Out folder", show=False),
        Binding("h", "go_home", "Home", show=False),
        Binding("ctrl+j", "jump_path", "Jump", show=False),
        Binding("f", "filter_listing", "Filter", show=False),
        Binding("v", "toggle_hidden", "Hidden", show=False),
        Binding("a", "select_recursive_supported", "Recursive all", show=False),
    ]

    def __init__(
        self,
        *,
        start: Path | None,
        skill_dir: Path | None,
        skill_id: str | None = None,
        model: str,
        max_tokens: int,
        max_attempts: int = 3,
        provider: str | None = None,
    ) -> None:
        super().__init__()
        base = (start or Path.cwd()).expanduser()
        if base.is_file():
            self.cwd = base.resolve().parent
        else:
            self.cwd = base.resolve()
        self.skill_dir_arg = skill_dir
        self.skill_id = skill_id
        self.model = model
        self.max_tokens = max_tokens
        self.max_attempts = max_attempts
        self.provider = provider
        self.selected: set[Path] = set()
        self.output_mode: str = "sibling"  # sibling | in_place | out_dir
        self._rows: list[tuple[Path | None, str]] = []
        self._selectable_row_keys: dict[Path, object] = {}
        self._row_key_seq = 0
        self._col_name_key: object | None = None
        self.name_filter: str = ""
        self.show_hidden: bool = False
        self.show_preview_pane: bool = True
        self._preview_cache: tuple[Path, float, str, str | None] | None = None
        self._preview_timer = None
        self._preview_load_id: int = 0
        self._mda_running = False
        self._run_worker = None

    def _alloc_row_key(self) -> str:
        self._row_key_seq += 1
        return f"lr{self._row_key_seq}"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Label(id="path-label")
                yield DocumentTable(id="listing", zebra_stripes=True, cursor_type="row")
                yield Label("", id="run-status")
                yield Label(id="selection-label")
                yield Label(id="mode-label")
                yield Label(id="filter-label")
                yield Input(
                    placeholder="Output folder (absolute path or ~)",
                    id="out-dir",
                )
            with Vertical(id="preview-col"):
                yield Label("Preview", id="preview-title")
                yield Label("", id="preview-meta")
                yield TextArea("", id="preview-body", read_only=True)
            with Vertical(id="right"):
                yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#listing", DataTable)
        name_k, _kind_k = table.add_columns(("Name", "name"), ("Kind", "kind"))
        self._col_name_key = name_k
        self.refresh_listing()
        self.log_msg(
            "[dim]Enter[/] opens a folder or runs the highlighted file.\n"
            "Results are saved next to the source. [dim]?[/] shows more options."
        )
        self._apply_preview_pane_visibility()

    def on_document_table_activated(self, event: DocumentTable.Activated) -> None:
        if event.table.id != "listing":
            return
        event.stop()
        # A navigation refresh may have replaced this row before its message was handled.
        if event.row_key in event.table.rows:
            self.action_activate(event.table.get_row_index(event.row_key))

    def on_unmount(self) -> None:
        if self._preview_timer is not None:
            self._preview_timer.stop()
            self._preview_timer = None
        self._preview_load_id += 1

    def action_show_help(self) -> None:
        self.push_screen(NavigationHelpScreen())

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "listing":
            return
        self._schedule_preview_update()

    def log_msg(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)

    def _mode_label(self) -> str:
        if self.output_mode == "sibling":
            return "Save a new copy next to the source"
        if self.output_mode == "in_place":
            return "Overwrite source: use terminal confirmation"
        return "Output folder"

    def _selection_label(self) -> str:
        if not self.selected:
            return ""
        n = len(self.selected)
        if n <= 3:
            names = ", ".join(p.name for p in sorted(self.selected))
            return f"Selected ({n}): {names} · P to run batch"
        return f"Selected: {n} file(s) · P to run batch"

    def _sync_secondary_controls(self) -> None:
        self.query_one("#selection-label", Label).display = bool(self.selected)
        self.query_one("#filter-label", Label).display = bool(self.name_filter or self.show_hidden)
        self.query_one("#out-dir", Input).display = self.output_mode == "out_dir"
        self.query_one("#run-status", Label).display = self._mda_running

    def _filter_status(self) -> str:
        base = f"Filter: {self.name_filter!r}" if self.name_filter else "Filter: (none)"
        hid = "show hidden" if self.show_hidden else "hide hidden/dotfiles"
        return f"{base}  |  {hid}"

    def _entry_name_ok(self, name: str) -> bool:
        if not self.name_filter:
            return True
        return self.name_filter in name.lower()

    def _path_visible(self, path: Path, *, is_dir: bool) -> bool:
        del is_dir  # reserved for future use
        if not self.show_hidden:
            if path.name.startswith("."):
                return False
            if _is_windows_hidden(path):
                return False
        return self._entry_name_ok(path.name)

    def refresh_listing(self) -> None:
        prev_focus_id: str | None = None
        w = self.focused
        if w is not None and hasattr(w, "id") and w.id is not None:
            prev_focus_id = str(w.id)

        table = self.query_one("#listing", DataTable)
        previous_path: Path | None = None
        previous_row = self._cursor_row_index()
        if previous_row is not None:
            previous_path = self._rows[previous_row][0]
        table.clear()
        self._rows.clear()
        self._selectable_row_keys.clear()

        self.query_one("#path-label", Label).update(str(self.cwd))
        self.query_one("#mode-label", Label).update(self._mode_label())
        self.query_one("#selection-label", Label).update(self._selection_label())
        self.query_one("#filter-label", Label).update(self._filter_status())
        self._sync_secondary_controls()

        if self._col_name_key is None:
            return

        if not self.cwd.is_dir():
            self.log_msg(f"[red]Not a directory:[/] {self.cwd}")
            return

        try:
            entries = list(self.cwd.iterdir())
        except OSError as e:
            self.log_msg(f"[red]Cannot read directory:[/] {e}")
            return

        dirs = sorted([p for p in entries if p.is_dir()], key=lambda p: p.name.lower())
        files = sorted([p for p in entries if p.is_file()], key=lambda p: p.name.lower())

        dirs = [p for p in dirs if self._path_visible(p, is_dir=True)]
        files = [p for p in files if self._path_visible(p, is_dir=False)]

        table.add_row("..", "parent", key=self._alloc_row_key())
        self._rows.append((self.cwd.parent, "up"))

        for p in dirs:
            table.add_row(f"{p.name}/", "dir", key=self._alloc_row_key())
            self._rows.append((p, "dir"))

        for p in files:
            selectable = is_supported_extension(p)
            kind = p.suffix.lower().lstrip(".") or "file"
            if not selectable:
                kind = "file"
            pr = p.resolve()
            mark = "* " if pr in self.selected else ""
            rk = table.add_row(f"{mark}{p.name}", kind, key=self._alloc_row_key())
            row_kind = kind if selectable else "file"
            self._rows.append((pr, row_kind))
            if selectable:
                self._selectable_row_keys[pr] = rk

        if previous_path is not None:
            for row_index, (path, _kind) in enumerate(self._rows):
                if path == previous_path:
                    table.move_cursor(row=row_index)
                    break

        if prev_focus_id == "out-dir" and self.output_mode == "out_dir":
            self.query_one("#out-dir", Input).focus()
        else:
            self.query_one("#listing", DataTable).focus()
        self._schedule_preview_update()

    def _apply_preview_pane_visibility(self) -> None:
        col = self.query_one("#preview-col")
        if self.show_preview_pane:
            col.remove_class("hidden")
        else:
            col.add_class("hidden")

    def action_toggle_preview(self) -> None:
        self.show_preview_pane = not self.show_preview_pane
        self._apply_preview_pane_visibility()
        if self.show_preview_pane:
            self._schedule_preview_update()
        else:
            self._clear_preview_pane()

    def _clear_preview_pane(self) -> None:
        self.query_one("#preview-meta", Label).update("")
        self.query_one("#preview-body", TextArea).load_text("")

    def _highlighted_preview_path(self) -> Path | None:
        idx = self._cursor_row_index()
        if idx is None:
            return None
        path, kind = self._rows[idx]
        if path is None or kind in ("up", "dir"):
            return None
        if not is_supported_extension(path):
            return None
        return path

    def _schedule_preview_update(self) -> None:
        if not self.show_preview_pane:
            return
        if self._preview_timer is not None:
            self._preview_timer.stop()
        self._preview_timer = self.set_timer(0.15, self._on_preview_debounce)

    def _on_preview_debounce(self) -> None:
        self._preview_timer = None
        if not self.is_running or not self.query("#listing"):
            return
        path = self._highlighted_preview_path()
        if path is None:
            self._preview_load_id += 1
            self._clear_preview_pane()
            return
        self._start_preview_load(path)

    def _start_preview_load(self, path: Path) -> None:
        self._preview_load_id += 1
        load_id = self._preview_load_id
        try:
            mtime = path.stat().st_mtime
        except OSError as e:
            self._render_preview(path, "", f"Cannot read file: {e}", truncated=False)
            return
        cached = self._preview_cache
        if cached is not None and cached[0] == path and cached[1] == mtime:
            body, truncated = truncate_preview_text(
                cached[2], max_chars=DEFAULT_PREVIEW_MAX_CHARS
            )
            self._render_preview(path, body, cached[3], truncated=truncated)
            return
        self.query_one("#preview-meta", Label).update(format_preview_metadata(path))
        self.query_one("#preview-body", TextArea).load_text("Loading…")
        self._load_preview_worker(path, load_id, mtime)

    @work(group="preview", exclusive=True)
    async def _load_preview_worker(self, path: Path, load_id: int, mtime: float) -> None:
        try:
            raw = await asyncio.to_thread(read_document_text, path)
            err: str | None = None
        except DocumentReadError as e:
            raw, err = "", str(e)
        except OSError as e:
            raw, err = "", f"Cannot read file: {e}"
        if load_id != self._preview_load_id:
            return
        self._preview_cache = (path, mtime, raw, err)
        truncated = False
        body = raw
        if err is None:
            body, truncated = truncate_preview_text(raw, max_chars=DEFAULT_PREVIEW_MAX_CHARS)
        self._render_preview(path, body, err, truncated=truncated)

    def _render_preview(
        self,
        path: Path,
        body: str,
        error: str | None,
        *,
        truncated: bool,
    ) -> None:
        meta = format_preview_metadata(path, error=error)
        self.query_one("#preview-meta", Label).update(meta)
        if error:
            text = error if not body else f"{error}\n\n{body}"
        elif body == "":
            text = "(empty)"
        else:
            text = body
        if truncated and not error:
            text = f"{text}\n\n… truncated"
        elif truncated:
            text = f"{text}\n\n… truncated"
        self.query_one("#preview-body", TextArea).load_text(text)

    def _sync_selectable_row_star(self, path: Path) -> None:
        if self._col_name_key is None:
            return
        rk = self._selectable_row_keys.get(path)
        if rk is None:
            return
        table = self.query_one("#listing", DataTable)
        mark = "* " if path in self.selected else ""
        table.update_cell(rk, self._col_name_key, f"{mark}{path.name}")

    def _cursor_row_index(self) -> int | None:
        table = self.query_one("#listing", DataTable)
        coord = table.cursor_coordinate
        if coord is None:
            return None
        row = coord.row
        if row < 0 or row >= len(self._rows):
            return None
        return row

    def action_go_up(self) -> None:
        parent = self.cwd.parent
        if parent == self.cwd:
            return
        self.cwd = parent.resolve()
        self.refresh_listing()

    def action_go_home(self) -> None:
        self.cwd = Path.home().resolve()
        self.refresh_listing()

    @work
    async def action_jump_path(self) -> None:
        path = await self.push_screen_wait(JumpPathScreen())
        if path is None:
            return
        if not path.is_dir():
            self.log_msg(f"[yellow]Not a directory:[/] {path}")
            return
        self.cwd = path
        self.refresh_listing()

    @work
    async def action_filter_listing(self) -> None:
        res = await self.push_screen_wait(FilterScreen(self.name_filter))
        if res is None:
            return
        self.name_filter = res
        self.refresh_listing()

    def action_toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self.refresh_listing()

    def action_select_recursive_supported(self) -> None:
        from mda_cli.document_io import is_restructured_output_name

        cap = 500
        found: list[Path] = []
        for ext in supported_extensions():
            for p in sorted(self.cwd.rglob(f"*{ext}")):
                if not p.is_file():
                    continue
                if is_restructured_output_name(p.name):
                    continue
                found.append(p.resolve())
        found = sorted(set(found))
        if len(found) > cap:
            self.log_msg(
                f"[yellow]Recursive select:[/] {len(found)} files found; "
                f"only first {cap} added."
            )
            found = found[:cap]
        else:
            self.log_msg(f"[cyan]Recursive select:[/] {len(found)} file(s) under {self.cwd}")
        self.selected.update(found)
        self.refresh_listing()

    def action_activate(self, row_index: int | None = None) -> None:
        idx = self._cursor_row_index() if row_index is None else row_index
        if idx is None:
            return
        path, kind = self._rows[idx]
        if path is None:
            return
        if kind in ("up", "dir"):
            self.cwd = path.resolve()
            self.refresh_listing()
        elif is_supported_extension(path):
            self._start_mda([path])
        else:
            self.log_msg(f"[yellow]Unsupported file type:[/] {path.name}")

    def action_toggle_select(self) -> None:
        idx = self._cursor_row_index()
        if idx is None:
            return
        path, kind = self._rows[idx]
        if path is None or kind in ("up", "dir") or not is_supported_extension(path):
            return
        if path in self.selected:
            self.selected.remove(path)
        else:
            self.selected.add(path)
        self._sync_selectable_row_star(path)
        self.query_one("#selection-label", Label).update(self._selection_label())
        self._sync_secondary_controls()

    def action_cycle_output(self) -> None:
        order = ("sibling", "in_place", "out_dir")
        i = order.index(self.output_mode)
        self.output_mode = order[(i + 1) % len(order)]
        self.query_one("#mode-label", Label).update(self._mode_label())
        self._sync_secondary_controls()

    def action_focus_out_dir(self) -> None:
        self.output_mode = "out_dir"
        self.query_one("#mode-label", Label).update(self._mode_label())
        self._sync_secondary_controls()
        self.query_one("#out-dir", Input).focus()

    def _show_run_progress(self, index: int, total: int, name: str) -> None:
        self.query_one("#run-status", Label).update(f"Running MDA · {index}/{total} · {name}")

    def _run_batch_sync(self, skill_dir: Path, jobs: list[Job]) -> None:
        def thread_log(msg: str) -> None:
            self.call_from_thread(self.log_msg, msg)

        system = load_system_prompt(skill_dir)
        client, ctx = build_restructure_client(
            cli_provider=self.provider,
            model=self.model,
            max_tokens=self.max_tokens,
        )
        ok = 0
        for index, job in enumerate(jobs, start=1):
            self.call_from_thread(self._show_run_progress, index, len(jobs), job.src.name)
            try:
                result = process_job_with_provider(
                    job,
                    client,
                    ctx,
                    max_tokens=self.max_tokens,
                    system=system,
                    progress=False,
                    log=thread_log,
                    max_attempts=self.max_attempts,
                )
                if result.ok:
                    ok += 1
            except Exception as e:
                thread_log(f"[red]FAILED[/] {job.src}: {e}")
        thread_log(f"[bold]Done.[/] {ok}/{len(jobs)} succeeded.")

    async def action_run_mda(self) -> None:
        if not self.selected:
            self.log_msg(
                "[yellow]No batch selected.[/] Use Enter to run the highlighted file, "
                "or Space to select files and P to run the batch."
            )
            return
        self._start_mda(sorted(self.selected))

    def _start_mda(self, sources: list[Path]) -> None:
        if self._mda_running:
            self.log_msg("[yellow]MDA is already running.[/] Wait for this run to finish.")
            return
        # Set the guard before scheduling so repeated Enter presses cannot create duplicate runs.
        self._mda_running = True
        self.query_one("#run-status", Label).update("Starting MDA…")
        self._sync_secondary_controls()
        self._run_worker = self.run_worker(
            self._run_mda(tuple(sources)),
            name="Run MDA",
            group="mda-run",
            exit_on_error=False,
        )

    async def _run_mda(self, sources: tuple[Path, ...]) -> None:
        try:
            provider = resolve_provider_name(cli_provider=self.provider)
            ensure_api_credentials(provider)

            skill_res, err = find_skill_dir(self.skill_dir_arg, skill_id=self.skill_id)
            if err or skill_res is None:
                self.log_msg(f"[red]{err or 'Could not resolve skill dir'}[/]")
                return
            skill_dir = skill_res.path

            out_dir: Path | None = None
            if self.output_mode == "in_place":
                # Preserve the existing per-file diff-approval requirement for source changes.
                self.log_msg(
                    "[yellow]In-place needs a diff confirmation per file. Run "
                    "`mda --in-place <file-or-folder>` in a terminal, "
                    "or pick another output mode.[/]"
                )
                return
            if self.output_mode == "out_dir":
                raw = self.query_one("#out-dir", Input).value.strip()
                if not raw:
                    self.log_msg(
                        "[yellow]Type an output folder in the bottom-left field (or press G).[/]"
                    )
                    return
                out_dir = Path(raw).expanduser().resolve()
                out_dir.mkdir(parents=True, exist_ok=True)

            jobs = jobs_from_document_files(
                sources,
                out_dir=out_dir,
                in_place=False,
                skill_id=skill_res.skill_id,
            )

            self.log_msg(f"[cyan]Skill:[/] {skill_res.skill_id} ({skill_dir})")
            for job in jobs:
                self.log_msg(f"[dim]{job.src} → {job.dst}[/]")

            await asyncio.to_thread(self._run_batch_sync, skill_dir, jobs)
            self._preview_cache = None
            self.refresh_listing()
        except (Exception, SystemExit) as e:
            self.log_msg(f"[red]MDA could not run:[/] {e}")
        finally:
            self._mda_running = False
            self.query_one("#run-status", Label).update("")
            self._sync_secondary_controls()


def run_tui(
    *,
    start: Path | None,
    skill_dir: Path | None,
    skill_id: str | None = None,
    model: str,
    max_tokens: int,
    max_attempts: int = 3,
    provider: str | None = None,
) -> int:
    app = MdaNavigatorApp(
        start=resolve_initial_folder(start),
        skill_dir=skill_dir,
        skill_id=skill_id,
        model=model,
        max_tokens=max_tokens,
        max_attempts=max_attempts,
        provider=provider,
    )
    app.run()
    try:
        set_last_folder(app.cwd)
    except OSError as e:
        print(f"Could not remember the last folder: {e}", file=sys.stderr)
    return 0
