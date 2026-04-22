"""Textual TUI app.

Keymap is specified in PARKED.md. Layout sketch is in DESIGN.md "TUI layout".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, Static, TextArea

from glean.index import Index
from glean.schema import Record
from glean.store import Library


MIN_QUERY_LEN = 2
SEARCH_DEBOUNCE = 0.15
LIST_LIMIT = 500


def _fzf_filter(query: str, lines: list[str]) -> list[str]:
    if not lines or not shutil.which("fzf"):
        return lines
    blob = "\n".join(lines)
    try:
        res = subprocess.run(
            ["fzf", "--filter", query],
            input=blob, capture_output=True, text=True, check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return [l for l in res.stdout.split("\n") if l.strip()]
    except OSError:
        pass
    return lines


def _format_authors_short(authors_flat: str) -> str:
    if not authors_flat:
        return ""
    parts = [p.strip() for p in authors_flat.split(";") if p.strip()]
    families = []
    for p in parts:
        # authors_flat is "Family Given; Family Given"; take the first token.
        families.append(p.split()[0] if p else "")
    if not families:
        return ""
    if len(families) == 1:
        return families[0]
    if len(families) == 2:
        return f"{families[0]} & {families[1]}"
    return f"{families[0]} et al."


def _format_bib_line(r: Record) -> str:
    parts = [r.journal or ""]
    if r.volume:
        parts.append(r.volume)
    if r.issue:
        parts.append(f"({r.issue})")
    bib = " ".join(p for p in parts if p).strip()
    if r.pages:
        bib = f"{bib}, {r.pages}" if bib else r.pages
    return bib


def _format_preview(r: Record) -> str:
    lines: list[str] = []
    lines.append(f"[b]{r.title}[/b]")
    lines.append(
        "; ".join(
            f"{a.family}{(' ' + a.given) if a.given else ''}" for a in r.authors
        ) + f"  ({r.year})"
    )
    bib = _format_bib_line(r)
    if bib:
        lines.append(bib)
    if r.type != "journal-article":
        lines.append(f"type: {r.type}")
    idline = []
    for k in ("doi", "arxiv", "isbn", "pmid", "pmcid", "legacy_citekey"):
        v = getattr(r, k)
        if v:
            idline.append(f"{k}={v}")
    if idline:
        lines.append("  ".join(idline))
    if r.url:
        lines.append(r.url)
    if r.keywords:
        lines.append(f"[dim]keywords:[/dim] {', '.join(r.keywords)}")
    if r.attachments:
        lines.append("")
        lines.append("[dim]attachments:[/dim]")
        for a in r.attachments:
            lines.append(f"  [{a.kind}] {a.path}")
    if r.abstract:
        lines.append("")
        lines.append("[b]Abstract[/b]")
        lines.append(r.abstract)
    if r.notes:
        lines.append("")
        lines.append("[b]Notes[/b]")
        lines.append(r.notes)
    lines.append("")
    read_str = "yes" if r.read else "no"
    lines.append(f"[dim]read: {read_str}  added: {r.added_at}  modified: {r.modified_at}[/dim]")
    return "\n".join(lines)


class PromptModal(ModalScreen[Optional[str]]):
    """One-line text prompt. Returns the value on Enter, None on Esc."""

    DEFAULT_CSS = """
    PromptModal {
        align: center middle;
    }
    PromptModal > Container {
        width: 60%;
        max-width: 80;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $accent;
    }
    PromptModal Label { margin-bottom: 1; }
    """

    BINDINGS = [
        Binding("escape", "dismiss_none", "cancel", show=False),
    ]

    def __init__(self, prompt: str, initial: str = "") -> None:
        super().__init__()
        self._prompt = prompt
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Container():
            yield Label(self._prompt)
            yield Input(value=self._initial, id="prompt-input")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    @on(Input.Submitted, "#prompt-input")
    def _on_submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class NotesModal(ModalScreen[None]):
    """Multi-line notes editor. Live-saves to disk on every keystroke. Esc closes."""

    DEFAULT_CSS = """
    NotesModal {
        align: center middle;
    }
    NotesModal > Container {
        width: 80%;
        height: 80%;
        padding: 1 2;
        background: $surface;
        border: thick $accent;
    }
    NotesModal Label.hint {
        dock: bottom;
        height: 1;
        color: $text-muted;
    }
    NotesModal TextArea { height: 1fr; }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "close", show=False),
    ]

    def __init__(self, lib: Library, citekey: str, initial: str) -> None:
        super().__init__()
        self.lib = lib
        self.citekey = citekey
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Container():
            yield Label(f"Notes  ·  {self.citekey}  ·  auto-save on edit, Esc to close")
            yield TextArea(self._initial, id="notes-area")
            yield Label("", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#notes-area", TextArea).focus()

    @on(TextArea.Changed, "#notes-area")
    def _on_changed(self, event: TextArea.Changed) -> None:
        try:
            r = self.lib.load(self.citekey)
        except FileNotFoundError:
            return
        text = event.text_area.text
        r.notes = text if text.strip() else None
        self.lib.save(r)
        # notes is not in the FTS schema; no index update needed.


class GleanApp(App):
    CSS = """
    #search { dock: top; height: 3; }
    Footer { dock: bottom; }
    #results { width: 40%; }
    #preview { width: 1fr; padding: 0 1; overflow-y: auto; }
    """

    BINDINGS = [
        Binding("enter", "open_main", "open"),
        Binding("r", "toggle_read", "toggle-read"),
        Binding("t", "add_keyword", "keyword"),
        Binding("T", "remove_keyword", show=False),
        Binding("n", "edit_notes", "notes"),
        Binding("a", "edit_abstract", "abstract"),
        Binding("e", "edit_yaml", "yaml"),
        Binding("R", "rename_key", "rename"),
        Binding("slash", "focus_search", "search"),
        Binding("q", "quit_app", "quit"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("g", "chord_g", show=False),
        Binding("G", "cursor_bottom", show=False),
        Binding("ctrl+n", "cursor_down", show=False),
        Binding("ctrl+p", "cursor_up", show=False),
        Binding("ctrl+d", "page_down", show=False),
        Binding("ctrl+u", "page_up", show=False),
    ]

    _chord_g_time: float = 0.0

    def __init__(self, lib: Library, index: Index):
        super().__init__()
        self.lib = lib
        self.index = index
        # Rows currently displayed. Each is (citekey, title, authors_flat, year).
        self._rows: list[tuple[str, str, str, Optional[int]]] = []
        self._debounce_timer = None
        self._last_query: str = ""

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search...", id="search")
        with Horizontal():
            yield DataTable(id="results", cursor_type="row", zebra_stripes=False)
            yield Static("", id="preview", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.add_columns("citekey", "year", "authors", "title")
        self._load_recent()
        # Focus the table so action bindings (n, r, e, q, ...) fire immediately.
        # Search is reached via `/`; Enter on search returns focus to the table.
        table.focus()

    # ---------- initial/recent view (SQL only) ----------

    def _load_recent(self) -> None:
        conn = self.index.connect()
        rows = conn.execute(
            "SELECT r.citekey, r.title, f.authors_flat, r.year "
            "FROM records r JOIN fts f ON f.citekey = r.citekey "
            "ORDER BY r.yaml_modified DESC, r.citekey ASC LIMIT ?",
            (LIST_LIMIT,),
        ).fetchall()
        self._populate_table(rows)

    def _populate_table(self, rows: list[tuple[str, str, str, Optional[int]]]) -> None:
        table = self.query_one("#results", DataTable)
        table.clear()
        self._rows = list(rows)
        for ck, title, authors_flat, year in self._rows:
            table.add_row(
                ck,
                str(year or ""),
                _format_authors_short(authors_flat or ""),
                title,
            )
        if self._rows:
            table.move_cursor(row=0)
            self._load_preview_for(self._rows[0][0])
        else:
            self._set_preview_text("[dim](no matches)[/dim]")

    # ---------- search ----------

    @on(Input.Changed, "#search")
    def _on_search_changed(self, event: Input.Changed) -> None:
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
        query = event.value.strip()
        if len(query) < MIN_QUERY_LEN:
            self._load_recent()
            return
        self._debounce_timer = self.set_timer(
            SEARCH_DEBOUNCE, lambda: self._fire_search(query)
        )

    def _fire_search(self, query: str) -> None:
        self._last_query = query
        self._run_search_worker(query)

    @staticmethod
    def _fts_query(raw: str) -> str:
        words = [w for w in raw.split() if w]
        return " ".join(f'"{w.replace(chr(34), chr(34)*2)}"*' for w in words)

    @work(thread=True, exclusive=True)
    def _run_search_worker(self, query: str) -> None:
        fts_q = self._fts_query(query)
        conn = self.index.connect()
        try:
            raw = conn.execute(
                "SELECT f.citekey, f.title, f.authors_flat, r.year "
                "FROM fts f JOIN records r ON r.citekey = f.citekey "
                "WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_q, LIST_LIMIT),
            ).fetchall()
        except Exception:
            raw = []
        lines = [f"{ck}\t{title}\t{authors}\t{year or ''}"
                 for ck, title, authors, year in raw]
        ranked_lines = _fzf_filter(query, lines) if lines else []
        ranked: list[tuple[str, str, str, Optional[int]]] = []
        for line in ranked_lines:
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            ck, title, authors, year_str = parts
            year = int(year_str) if year_str else None
            ranked.append((ck, title, authors, year))

        # Only apply results if this is still the latest query.
        if query != self._last_query:
            return
        self.call_from_thread(self._populate_table, ranked)

    # ---------- preview ----------

    def _load_preview_for(self, citekey: str) -> None:
        try:
            r = self.lib.load(citekey)
        except FileNotFoundError:
            self._set_preview_text("[dim](record missing on disk)[/dim]")
            return
        self._set_preview_text(_format_preview(r))

    def _set_preview_text(self, text: str) -> None:
        self.query_one("#preview", Static).update(text)

    @on(DataTable.RowHighlighted, "#results")
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        row = event.cursor_row
        if 0 <= row < len(self._rows):
            self._load_preview_for(self._rows[row][0])

    def _current_citekey(self) -> Optional[str]:
        table = self.query_one("#results", DataTable)
        row = table.cursor_row
        if 0 <= row < len(self._rows):
            return self._rows[row][0]
        return None

    def _current_record(self) -> Optional[Record]:
        ck = self._current_citekey()
        if ck is None:
            return None
        try:
            return self.lib.load(ck)
        except FileNotFoundError:
            return None

    # ---------- navigation ----------

    def action_cursor_down(self) -> None:
        self.query_one("#results", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#results", DataTable).action_cursor_up()

    def action_cursor_bottom(self) -> None:
        t = self.query_one("#results", DataTable)
        if t.row_count:
            t.move_cursor(row=t.row_count - 1)

    def action_cursor_top(self) -> None:
        t = self.query_one("#results", DataTable)
        if t.row_count:
            t.move_cursor(row=0)

    def action_page_down(self) -> None:
        self.query_one("#results", DataTable).action_page_down()

    def action_page_up(self) -> None:
        self.query_one("#results", DataTable).action_page_up()

    def action_chord_g(self) -> None:
        now = time.time()
        if now - self._chord_g_time < 0.75:
            self.action_cursor_top()
            self._chord_g_time = 0.0
        else:
            self._chord_g_time = now

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    # ---------- editing: enter/open ----------

    def action_open_main(self) -> None:
        r = self._current_record()
        if not r:
            self.bell()
            return
        main = r.main_attachment()
        if not main:
            self.bell()
            return
        path = self.lib.root / main.path
        if not path.exists():
            self.bell()
            return
        subprocess.run(["open", str(path)], check=False)

    # ---------- editing: toggle read ----------

    def action_toggle_read(self) -> None:
        ck = self._current_citekey()
        if not ck:
            return
        try:
            r = self.lib.load(ck)
        except FileNotFoundError:
            return
        r.read = not r.read
        self.lib.save(r)
        conn = self.index.connect()
        with conn:
            conn.execute(
                "UPDATE records SET read = ?, yaml_modified = ? WHERE citekey = ?",
                (1 if r.read else 0, r.modified_at.isoformat(), ck),
            )
        self._set_preview_text(_format_preview(r))

    # ---------- editing: keywords (modal prompt) ----------

    @work
    async def action_add_keyword(self) -> None:
        ck = self._current_citekey()
        if not ck:
            return
        word = await self.push_screen_wait(PromptModal("Add keyword:"))
        if not word or not word.strip():
            return
        word = word.strip()
        r = self.lib.load(ck)
        if word not in r.keywords:
            r.keywords.append(word)
            self.lib.save(r)
            self.index._upsert(r)
        self._set_preview_text(_format_preview(r))

    @work
    async def action_remove_keyword(self) -> None:
        ck = self._current_citekey()
        if not ck:
            return
        r = self.lib.load(ck)
        if not r.keywords:
            self.bell()
            return
        word = await self.push_screen_wait(
            PromptModal(f"Remove keyword  (have: {', '.join(r.keywords)}):")
        )
        if not word or not word.strip():
            return
        word = word.strip()
        if word in r.keywords:
            r.keywords = [k for k in r.keywords if k != word]
            self.lib.save(r)
            self.index._upsert(r)
        self._set_preview_text(_format_preview(r))

    # ---------- editing: notes (modal, live-save) ----------

    @work
    async def action_edit_notes(self) -> None:
        ck = self._current_citekey()
        if not ck:
            return
        r = self.lib.load(ck)
        await self.push_screen_wait(NotesModal(self.lib, ck, r.notes or ""))
        # Reload the now-updated record and refresh preview.
        try:
            r2 = self.lib.load(ck)
        except FileNotFoundError:
            return
        self._set_preview_text(_format_preview(r2))

    # ---------- editing: abstract and full YAML in $EDITOR ----------

    def _edit_field_in_editor(self, citekey: str, field: str) -> None:
        r = self.lib.load(citekey)
        initial = getattr(r, field) or ""
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False,
                                          encoding="utf-8") as f:
            f.write(initial)
            tmp = f.name
        with self.suspend():
            subprocess.run(
                [os.environ.get("EDITOR", "vi"), tmp],
                check=False,
            )
        try:
            new_text = open(tmp, encoding="utf-8").read().rstrip()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        setattr(r, field, new_text or None)
        self.lib.save(r)
        self.index._upsert(r)
        self._set_preview_text(_format_preview(r))

    def action_edit_abstract(self) -> None:
        ck = self._current_citekey()
        if not ck:
            return
        self._edit_field_in_editor(ck, "abstract")

    def action_edit_yaml(self) -> None:
        ck = self._current_citekey()
        if not ck:
            return
        path = self.lib.path_for(ck)
        with self.suspend():
            subprocess.run(
                [os.environ.get("EDITOR", "vi"), str(path)],
                check=False,
            )
        try:
            r = self.lib.load(ck)
        except Exception:
            self.bell()
            return
        self.lib.save(r)
        self.index._upsert(r)
        self._set_preview_text(_format_preview(r))

    # ---------- editing: rename (modal) ----------

    @work
    async def action_rename_key(self) -> None:
        ck = self._current_citekey()
        if not ck:
            return
        new = await self.push_screen_wait(
            PromptModal(f"Rename {ck} →", initial=ck)
        )
        if not new or not new.strip() or new == ck:
            return
        new = new.strip()
        from glean.cli import main as cli_main
        rc = cli_main(["rename", ck, new])
        if rc == 0:
            # Replace the row in the list so the user stays on it.
            for i, row in enumerate(self._rows):
                if row[0] == ck:
                    self._rows[i] = (new,) + row[1:]
                    break
            table = self.query_one("#results", DataTable)
            cur = table.cursor_row
            self._populate_table(self._rows)
            if 0 <= cur < len(self._rows):
                table.move_cursor(row=cur)
        else:
            self.bell()

    # ---------- quit ----------

    def action_quit_app(self) -> None:
        self.exit()
