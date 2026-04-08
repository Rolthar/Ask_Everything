# Ask Everything

A natural language search companion for [Everything by voidtools](https://www.voidtools.com/).

Type a plain-English query like *"big video files I haven't opened in a year"* and Ask Everything translates it into Everything's native search syntax via Claude, then shows the results in a fast overlay UI.

---

## Features

- **Natural language → Everything syntax** via Claude (Anthropic API)
- **Global hotkey** (`Ctrl+Shift+Space` by default) opens a borderless overlay
- **System tray icon** with recent searches submenu
- **LRU + SQLite cache** — repeated queries skip the API call entirely
- **Search history** — cycle with Up/Down arrow; searchable from the tray
- **Multi-monitor aware**, DPI-aware, dark/light mode auto-detect
- **Result validation** — stale paths shown in grey with strikethrough
- **Pagination** — "Load more" appends results without clearing the list
- **Extension chips** — click `pdf ×12` to instantly filter by extension
- **Right-click menu** — open, open folder, copy path, copy as markdown link

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.11+ |
| [Everything by voidtools](https://www.voidtools.com/) | 1.4 or 1.5 |
| Windows | 10 / 11 (32- or 64-bit) |
| Anthropic API key | [console.anthropic.com](https://console.anthropic.com) |

---

## Installation

```bash
# 1. Clone
git clone https://github.com/Rolthar/Ask_Everything.git
cd Ask_Everything

# 2. Create a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
copy .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=sk-ant-...

# 5. Make sure Everything.exe is running

# 6. Run
python main.py
```

> **Note:** `keyboard` requires sufficient privileges to register a global hotkey.  If the hotkey fails to register, try running as Administrator or choose a different key combination in Settings.

---

## Configuration

All settings are stored in `.env` (or can be changed via the in-app Settings window).

| Key | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required)* | Your Anthropic API key |
| `HOTKEY` | `ctrl+shift+space` | Global hotkey syntax |
| `MAX_RESULTS` | `20` | Results per page (20–200) |
| `DEFAULT_SORT_COLUMN` | `date_modified` | name, path, size, or date_modified |
| `DEFAULT_SORT_DIRECTION` | `desc` | asc or desc |
| `CACHE_ENABLED` | `true` | Enable persistent translation cache |
| `CACHE_TTL` | `86400` | Cache time-to-live in seconds (24 h) |
| `EVERYTHING_PATH` | `C:\Program Files\Everything\Everything.exe` | Path for auto-launch |
| `RESULT_VALIDATION` | `true` | Validate paths exist before display |
| `THEME` | `auto` | auto, light, or dark |
| `FONT_SIZE` | `11` | Base font size in points |
| `WINDOW_OPACITY` | `0.95` | Overlay opacity (0.85–1.0) |

---

## Usage

1. Press **Ctrl+Shift+Space** (or your configured hotkey) to open the overlay.
2. Type a natural language query and press **Enter**.
3. The overlay shows the translated Everything query and matching results.
4. Double-click a result to open it; right-click for more options.
5. Use the filter bar (`All | Files | Folders`) and extension chips to narrow results.
6. Press **Escape** or click outside the overlay to dismiss it.

### Example queries

| Natural language | Translated Everything query |
|---|---|
| big video files I haven't opened in a year | `ext:mp4|mov|mkv size:>500mb da:<last year` |
| that UE5 doc from last month | `path:*unreal*|path:*ue5* ext:docx|pdf dm:last month` |
| Python files I edited this week | `ext:py dm:this week` |
| invoices from this year, not in archive | `*invoice* ext:pdf dm:this year !path:*archive*` |
| large folders modified last month | `folder: dm:last month size:>1gb` |

---

## Architecture

```
ask-everything/
├── main.py        Entry point; hotkey; tray; lifecycle
├── ui.py          tkinter overlay window
├── translator.py  Claude API; NL → Everything syntax
├── search.py      Everything SDK integration
├── cache.py       LRU + SQLite translation cache
├── history.py     SQLite search history store
├── tray.py        System tray icon (pystray)
├── settings.py    Settings window + .env writer
├── config.py      Loads .env; typed constants
├── requirements.txt
├── .env.example
└── README.md
```

### Data flow

```
User types query
      |
      v
cache.get(nl_query)  --hit-->  search.search(eq_query)
      |                               |
     miss                             v
      |                         SearchResponse
      v                               |
translator.translate(nl_query)        v
      |                        ui: render results
      v
cache.put(nl_query, eq_query)
history.add(...)
```

### Module details

| Module | Responsibility |
|---|---|
| `config.py` | Reads `.env`; exposes typed constants; configures rotating log handler |
| `translator.py` | Calls Claude API; sanitises response; retries on 429 |
| `search.py` | IPC calls to Everything via `everything-sdk`; result parsing; path validation |
| `cache.py` | In-memory LRU (100 entries) + SQLite persistence; TTL expiry |
| `history.py` | SQLite store (500 rows max); CSV export |
| `ui.py` | Borderless tkinter overlay; results treeview; filter bar; pagination; context menu |
| `tray.py` | `pystray` icon with menu; recent searches submenu |
| `settings.py` | Tabbed settings form; writes back to `.env` |
| `main.py` | Single-instance lock; hotkey registration; lifecycle management |

---

## Error handling

- **Everything not running** — shows an error with a "Launch Everything" button
- **API key missing** — prompts user to configure before the first search
- **Rate limited** — exponential back-off (1 s → 2 s → 4 s), max 3 retries
- **Translation timeout** — 10-second timeout; user-friendly message
- **Stale paths** — validated in background threads; shown in grey
- All errors logged to `~/.ask_everything/ask-everything.log` (1 MB, 2 backups)

---

## Everything syntax reference

Everything's full search syntax is documented at  
https://www.voidtools.com/support/everything/searching/

The system prompt in `translator.py` teaches Claude the full syntax including boolean operators, wildcards, filters, size/date ranges, attributes, regex, and content search.

---

## License

MIT
