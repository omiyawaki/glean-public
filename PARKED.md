# Parked work

Decisions that are settled but not yet implemented. Surface the relevant section when the corresponding build step begins.

## Bookends BibTeX probe (do before writing the importer in step 5)

Before touching `glean/import_bookends.py`, export a handful of records from Bookends as BibTeX and inspect the output. The question: does the export include a `file = {path}` field tying each entry to its PDF?

- If yes, the importer's attachment mapping is deterministic off the BibTeX alone.
- If no, write a small companion AppleScript first that emits `<bookends-id>\t<attachment-filename>` alongside the BibTeX dump, so the importer has an authoritative ID-to-path map. Only then start on the importer.

Do not commit the importer to a design that relies on fuzzy author/year/title matching against filenames in `~/Library/Mobile Documents/iCloud~com~sonnysoftware~bot/Documents/Attachments/`. That is the lossy fallback and should be avoided if a cleaner signal exists.

See DESIGN.md "Migration from Bookends".


## TUI keymap (for step 6, TUI implementation)

### Editing actions (from result list with a record selected)

| Key     | Action                                      |
|---------|---------------------------------------------|
| Enter   | open main attachment                        |
| r       | toggle read                                 |
| t / T   | add / remove keyword (prompts)              |
| n       | edit notes in an in-TUI multi-line widget   |
| a       | edit abstract in $EDITOR via App.suspend()  |
| e       | edit whole YAML in $EDITOR                  |
| R       | rename citekey (runs `glean rename`)        |
| q / :q  | quit                                        |

### Navigation: vim default, emacs alternate, conventional fallbacks

| Action                    | Vim             | Emacs            | Also                      |
|---------------------------|-----------------|------------------|---------------------------|
| Down / up                 | j / k           | Ctrl-N / Ctrl-P  | arrows                    |
| Page down / up            | Ctrl-D / Ctrl-U | Ctrl-V / Alt-V   | PgDn / PgUp               |
| Top / bottom              | gg / G          | Alt-< / Alt->    | Home / End                |
| Start / end of input      | 0 / $           | Ctrl-A / Ctrl-E  | Home / End                |
| Word back / fwd in input  | b / w           | Alt-B / Alt-F    |                           |
| Delete back / fwd in input| x               | Ctrl-H / Ctrl-D  | Backspace / Del           |
| Focus search              | /               | Ctrl-S           | (search is always live)   |

Note: `n` / `N` from vim is meaningless here since search is incremental, so `n` stays free for notes-edit.
