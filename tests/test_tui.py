from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from textual.widgets import DataTable, Input, RichLog

from mda_cli import config
from mda_cli.tui import FilterScreen, JumpPathScreen, MdaNavigatorApp


@pytest.fixture(autouse=True)
def isolated_preferences(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "mda-config.json")


@pytest.fixture(autouse=True)
def process_job(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Exercise job setup and UI logging without credentials or external requests."""
    process = Mock(return_value=SimpleNamespace(ok=True))
    monkeypatch.setattr("mda_cli.tui.ensure_api_credentials", Mock())
    monkeypatch.setattr(
        "mda_cli.tui.build_restructure_client", Mock(return_value=(object(), object()))
    )
    monkeypatch.setattr("mda_cli.tui.process_job_with_provider", process)
    return process


def _navigator(start: Path, skill_dir: Path) -> MdaNavigatorApp:
    return MdaNavigatorApp(
        start=start,
        skill_dir=skill_dir,
        model="test-model",
        max_tokens=100,
        provider="anthropic",
    )


def _listed_names(app: MdaNavigatorApp) -> list[str]:
    table = app.query_one("#listing", DataTable)
    return [str(table.get_row_at(row)[0]) for row in range(table.row_count)]


def _log_text(app: MdaNavigatorApp) -> str:
    return "\n".join(line.text for line in app.query_one("#log", RichLog).lines)


async def _finish_workers(app: MdaNavigatorApp) -> None:
    await asyncio.wait_for(app.workers.wait_for_complete(), timeout=5)


@pytest.mark.asyncio
async def test_tui_mount_stylesheet_and_select_supported(skill_dir: Path, tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "one.md").write_text("# One\n", encoding="utf-8")
    note = docs / "note.txt"
    note.write_text("x", encoding="utf-8")

    app = MdaNavigatorApp(
        start=docs,
        skill_dir=skill_dir,
        model="claude-sonnet-4-20250514",
        max_tokens=100,
    )
    async with app.run_test() as pilot:
        assert app.cwd.resolve() == docs.resolve()
        await pilot.press("down")  # .. -> note.txt
        await pilot.press("space")
        assert app.selected == {note.resolve()}
        await pilot.press("down")  # one.md
        await pilot.press("space")
        assert app.selected == {note.resolve(), (docs / "one.md").resolve()}
        await pilot.press("o")
        assert app.output_mode == "in_place"
        await pilot.press("q")


@pytest.mark.asyncio
async def test_tui_enter_opens_subdirectory(
    skill_dir: Path, tmp_path: Path, process_job: Mock
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    sub = root / "inside"
    sub.mkdir()
    (sub / "a.md").write_text("# A", encoding="utf-8")
    app = MdaNavigatorApp(
        start=root,
        skill_dir=skill_dir,
        model="claude-sonnet-4-20250514",
        max_tokens=100,
    )
    async with app.run_test() as pilot:
        await pilot.press("down")  # .. -> inside/
        await pilot.press("enter")
        assert app.cwd.resolve() == sub.resolve()
        assert _listed_names(app) == ["..", "a.md"]
        assert app.focused is app.query_one("#listing", DataTable)
        process_job.assert_not_called()
        await pilot.press("q")


@pytest.mark.parametrize("start_at_file", [False, True])
def test_run_tui_opens_one_navigator_without_folder_picker(
    monkeypatch: pytest.MonkeyPatch,
    skill_dir: Path,
    tmp_path: Path,
    start_at_file: bool,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "a.md"
    source.write_text("# A", encoding="utf-8")
    navigator_starts: list[Path] = []

    def fake_pick_folder(*, start: Path | None) -> Path | None:
        pytest.fail("run_tui must open the navigator without a folder-picker/U step")

    def capture_navigator(app: MdaNavigatorApp) -> None:
        navigator_starts.append(app.cwd)

    monkeypatch.setattr("mda_cli.folder_picker.pick_folder", fake_pick_folder)
    monkeypatch.setattr(MdaNavigatorApp, "run", capture_navigator)

    from mda_cli.tui import run_tui

    start = source if start_at_file else vault
    assert run_tui(start=start, skill_dir=skill_dir, model="x", max_tokens=100) == 0
    assert navigator_starts == [vault.resolve()]


@pytest.mark.asyncio
async def test_tui_jump_path_worker(skill_dir: Path, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "doc.md").write_text("# Doc", encoding="utf-8")

    app = MdaNavigatorApp(
        start=root,
        skill_dir=skill_dir,
        model="claude-sonnet-4-20250514",
        max_tokens=100,
    )

    async def fake_push_screen_wait(screen, *, mode=None):  # noqa: ARG001
        return target.resolve()

    app.push_screen_wait = fake_push_screen_wait  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        worker = app.action_jump_path()
        await worker.wait()
        assert app.cwd.resolve() == target.resolve()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_tui_jump_path_via_binding(skill_dir: Path, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "other"
    target.mkdir()

    app = MdaNavigatorApp(
        start=root,
        skill_dir=skill_dir,
        model="claude-sonnet-4-20250514",
        max_tokens=100,
    )
    async with app.run_test() as pilot:
        await pilot.press("ctrl+j")
        assert isinstance(app.screen, JumpPathScreen)
        jump_in = app.screen.query_one("#jump-in")
        jump_in.value = str(target)
        await pilot.press("enter")
        await pilot.pause()
        assert app.cwd.resolve() == target.resolve()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_tui_filter_listing_worker(skill_dir: Path, tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "alpha.md").write_text("# A", encoding="utf-8")
    (docs / "beta.md").write_text("# B", encoding="utf-8")

    app = MdaNavigatorApp(
        start=docs,
        skill_dir=skill_dir,
        model="claude-sonnet-4-20250514",
        max_tokens=100,
    )

    async def fake_push_screen_wait(screen, *, mode=None):  # noqa: ARG001
        return "alpha"

    app.push_screen_wait = fake_push_screen_wait  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        worker = app.action_filter_listing()
        await worker.wait()
        assert app.name_filter == "alpha"
        await pilot.press("q")


@pytest.mark.asyncio
async def test_tui_filter_via_binding(skill_dir: Path, tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "alpha.md").write_text("# A", encoding="utf-8")
    (docs / "keep.txt").write_text("x", encoding="utf-8")

    app = MdaNavigatorApp(
        start=docs,
        skill_dir=skill_dir,
        model="claude-sonnet-4-20250514",
        max_tokens=100,
    )
    async with app.run_test() as pilot:
        await pilot.press("f")
        assert isinstance(app.screen, FilterScreen)
        filt_in = app.screen.query_one("#filter-in")
        filt_in.value = "alpha"
        await pilot.press("enter")
        await pilot.pause()
        assert app.name_filter == "alpha"
        names = {p.name for p, _ in app._rows if p is not None}
        assert "alpha.md" in names
        assert "keep.txt" not in names
        await pilot.press("q")


@pytest.mark.asyncio
async def test_tui_start_file_uses_parent(skill_dir: Path, tmp_path: Path) -> None:
    f = tmp_path / "x.md"
    f.write_text("# X", encoding="utf-8")
    app = MdaNavigatorApp(
        start=f,
        skill_dir=skill_dir,
        model="claude-sonnet-4-20250514",
        max_tokens=100,
    )
    async with app.run_test() as pilot:
        assert app.cwd == f.parent.resolve()
        await pilot.press("q")


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".md", ".txt"])
async def test_enter_processes_highlighted_file_without_marking_or_p(
    skill_dir: Path, tmp_path: Path, process_job: Mock, suffix: str
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / f"source{suffix}"
    source.write_text("Source content\n", encoding="utf-8")
    app = _navigator(docs, skill_dir)

    async with app.run_test() as pilot:
        assert app.focused is app.query_one("#listing", DataTable)
        assert app.selected == set()
        await pilot.press("down", "enter")
        await _finish_workers(app)

        process_job.assert_called_once()
        job = process_job.call_args.args[0]
        assert job.src == source.resolve()
        assert job.dst == docs / "source.restructured.md"
        assert app.selected == set()
        assert "1/1 succeeded" in _log_text(app)


@pytest.mark.asyncio
async def test_enter_only_processes_highlighted_file_and_p_keeps_batch_selection(
    skill_dir: Path, tmp_path: Path, process_job: Mock
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    sources = [docs / name for name in ("a.md", "b.md", "c.md")]
    for source in sources:
        source.write_text("# Source\n", encoding="utf-8")
    app = _navigator(docs, skill_dir)

    async with app.run_test() as pilot:
        await pilot.press("down", "space", "down", "space", "down", "enter")
        await _finish_workers(app)

        assert [call.args[0].src for call in process_job.call_args_list] == [sources[2]]
        assert app.selected == set(sources[:2])

        await pilot.press("p")
        await _finish_workers(app)
        assert [call.args[0].src for call in process_job.call_args_list] == [
            sources[2], sources[0], sources[1]
        ]


@pytest.mark.asyncio
async def test_enter_on_unsupported_file_never_processes_marked_files(
    skill_dir: Path, tmp_path: Path, process_job: Mock
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "a.md"
    source.write_text("# Source\n", encoding="utf-8")
    (docs / "z.png").write_bytes(b"not a document")
    app = _navigator(docs, skill_dir)

    async with app.run_test() as pilot:
        await pilot.press("down", "space", "down", "enter")
        await _finish_workers(app)
        process_job.assert_not_called()
        assert app.selected == {source.resolve()}
        assert app.cwd == docs.resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding", "input_id"),
    [("f", "filter-in"), ("ctrl+j", "jump-in"), ("g", "out-dir")],
)
async def test_enter_in_text_input_does_not_activate_highlighted_or_marked_file(
    skill_dir: Path,
    tmp_path: Path,
    process_job: Mock,
    binding: str,
    input_id: str,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "source.md"
    source.write_text("# Source\n", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    (target / "other.md").write_text("# Other\n", encoding="utf-8")
    app = _navigator(docs, skill_dir)

    async with app.run_test() as pilot:
        await pilot.press("down", "space", binding)
        field = app.screen.query_one(f"#{input_id}", Input)
        assert app.focused is field
        field.value = "source" if input_id == "filter-in" else str(target)
        await pilot.press("enter")
        await _finish_workers(app)

        process_job.assert_not_called()
        assert app.selected == {source.resolve()}
        if input_id == "filter-in":
            assert app.name_filter == "source"
        elif input_id == "jump-in":
            assert app.cwd == target.resolve()
        else:
            assert field.value == str(target)


@pytest.mark.asyncio
async def test_repeated_enter_and_p_do_not_queue_duplicate_running_jobs(
    skill_dir: Path, tmp_path: Path, process_job: Mock
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "source.md"
    source.write_text("# Source\n", encoding="utf-8")
    app = _navigator(docs, skill_dir)
    started = threading.Event()
    release = threading.Event()

    def blocked_process(*args: object, **kwargs: object) -> SimpleNamespace:
        started.set()
        assert release.wait(timeout=10), "test did not release the mocked provider"
        return SimpleNamespace(ok=True)

    process_job.side_effect = blocked_process
    async with app.run_test() as pilot:
        await pilot.press("down", "space")
        try:
            await asyncio.wait_for(pilot.press("enter"), timeout=3)
            assert await asyncio.to_thread(started.wait, 2), "Enter did not start processing"
            await asyncio.wait_for(pilot.press("enter", "enter", "p"), timeout=3)
            process_job.assert_called_once()
        finally:
            release.set()
        await _finish_workers(app)
        process_job.assert_called_once()
        assert process_job.call_args.args[0].src == source.resolve()


@pytest.mark.asyncio
async def test_processing_error_stays_visible_and_enter_can_retry(
    skill_dir: Path, tmp_path: Path, process_job: Mock
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "source.md"
    source.write_text("# Source\n", encoding="utf-8")
    process_job.side_effect = [RuntimeError("retryable-fault"), SimpleNamespace(ok=True)]
    app = _navigator(docs, skill_dir)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("down", "enter")
        await _finish_workers(app)
        assert "retryable-fault" in _log_text(app)
        assert "0/1 succeeded" in _log_text(app)

        await pilot.press("enter")
        await _finish_workers(app)
        assert [call.args[0].src for call in process_job.call_args_list] == [source, source]
        assert "retryable-fault" in _log_text(app)
        assert "1/1 succeeded" in _log_text(app)


@pytest.mark.asyncio
async def test_client_setup_error_is_visible_and_does_not_block_retry(
    monkeypatch: pytest.MonkeyPatch,
    skill_dir: Path,
    tmp_path: Path,
    process_job: Mock,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "source.md").write_text("# Source\n", encoding="utf-8")
    build_client = Mock(side_effect=[RuntimeError("client-setup-fault"), (object(), object())])
    monkeypatch.setattr("mda_cli.tui.build_restructure_client", build_client)
    app = _navigator(docs, skill_dir)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("down", "enter")
        await _finish_workers(app)
        assert "client-setup-fault" in _log_text(app)
        process_job.assert_not_called()

        await pilot.press("enter")
        await _finish_workers(app)
        process_job.assert_called_once()
        assert "client-setup-fault" in _log_text(app)
        assert "1/1 succeeded" in _log_text(app)


@pytest.mark.asyncio
async def test_enter_preserves_in_place_confirmation_safeguard(
    skill_dir: Path, tmp_path: Path, process_job: Mock
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "source.md"
    original = "# Original content\n"
    source.write_text(original, encoding="utf-8")
    app = _navigator(docs, skill_dir)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("down", "o", "enter")
        await _finish_workers(app)
        process_job.assert_not_called()
        assert source.read_text(encoding="utf-8") == original
        assert "diff confirmation" in _log_text(app)


def test_run_tui_remembers_folder_for_the_next_direct_navigator_session(
    monkeypatch: pytest.MonkeyPatch, skill_dir: Path, tmp_path: Path
) -> None:
    remembered = tmp_path / "remembered"
    inside = remembered / "inside"
    inside.mkdir(parents=True)
    (inside / "source.md").write_text("# Source\n", encoding="utf-8")
    config.set_last_folder(remembered)
    navigator_starts: list[Path] = []

    def run_navigator(app: MdaNavigatorApp) -> None:
        async def browse() -> None:
            async with app.run_test() as pilot:
                navigator_starts.append(app.cwd)
                if len(navigator_starts) == 1:
                    await pilot.press("down", "enter")
                    assert app.cwd == inside.resolve()
                    assert _listed_names(app) == ["..", "source.md"]
                await pilot.press("q")

        asyncio.run(browse())

    def forbidden_picker(*, start: Path | None) -> Path | None:
        pytest.fail("Restoring a folder must not introduce a folder-picker/U step")

    monkeypatch.setattr(MdaNavigatorApp, "run", run_navigator)
    monkeypatch.setattr("mda_cli.folder_picker.pick_folder", forbidden_picker)
    from mda_cli.tui import run_tui

    assert run_tui(start=None, skill_dir=skill_dir, model="test-model", max_tokens=100) == 0
    assert config.get_last_folder() == inside.resolve()
    assert run_tui(start=None, skill_dir=skill_dir, model="test-model", max_tokens=100) == 0
    assert navigator_starts == [remembered.resolve(), inside.resolve()]


@pytest.mark.asyncio
async def test_clicking_highlighted_file_does_not_process_until_enter(
    skill_dir: Path, tmp_path: Path, process_job: Mock
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "source.md"
    source.write_text("# Source\n", encoding="utf-8")
    app = _navigator(docs, skill_dir)

    async with app.run_test() as pilot:
        await pilot.press("down")
        table = app.query_one("#listing", DataTable)
        assert _listed_names(app)[table.cursor_row] == source.name
        await pilot.click("#listing", offset=(5, table.header_height + table.cursor_row))
        await _finish_workers(app)
        assert _listed_names(app)[table.cursor_row] == source.name
        process_job.assert_not_called()

        await pilot.press("enter")
        await _finish_workers(app)
        process_job.assert_called_once()
        assert process_job.call_args.args[0].src == source.resolve()
