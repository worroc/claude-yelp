# Claude Yelp

A terminal-based session manager for Claude Code CLI. Browse and manage your Claude sessions with a simple two-panel interface built using Python and Textual.

## Overview

Claude Yelp provides a TUI (Text User Interface) for managing Claude Code CLI sessions. It scans your local Claude session storage, displays them in an organized list, and allows you to browse conversation threads, tag sessions for easy identification, and quickly resume any session.

## Features

- **Two-panel interface**: Left panel shows session list, right panel shows conversation thread
- **Session tagging**: Tag sessions for easy identification (persisted in `~/.claude/claude-yelp-tags.json`)
- **Session discovery**: Automatically discovers all Claude sessions from `~/.claude/projects/`
- **Session resumption**: Directly launch Claude CLI with `--resume` flag for selected session
- **Session creation**: Create new tagged sessions from within the TUI or from the command line
- **Search & filter**: Search sessions by content, tag, project name, or session ID
- **Thread search**: Search within conversation threads with match highlighting and navigation
- **Export**: Export conversations to markdown files
- **Copy to clipboard**: Copy entire thread content or yank selected text
- **Delete sessions**: Remove sessions with confirmation dialog
- **User-only mode**: Toggle to show only user messages in the thread view
- **Resizable panels**: Adjust panel widths with keyboard shortcuts
- **Vim-style navigation**: `gg`, `G`, `/`, `n`, `N`, `:` command mode
- **Temporary sessions**: Create sessions that are auto-deleted on exit (`-t` flag)

## Keyboard Shortcuts

### Navigation
| Key | Action |
|-----|--------|
| `Up/Down` | Navigate session list (left panel) or scroll thread (right panel) |
| `Left/Right` | Switch focus between panels |
| `Shift+Left/Right` | Resize panels |
| `PageUp/PageDown` | Page scroll in active panel |
| `gg` | Go to top (double press `g`) |
| `G` | Go to bottom |

### Session Actions
| Key | Action |
|-----|--------|
| `s` | Start (resume) selected session |
| `t` | Tag selected session |
| `d` | Delete selected session |
| `e` | Export session to markdown file |
| `Ctrl+n` | Create a new tagged session |

### Search & Filter
| Key | Action |
|-----|--------|
| `/` | Search mode (filters sessions in left panel, searches text in right panel) |
| `n` | Next search match |
| `N` | Previous search match |
| `:` | Command mode (enter number to jump to session) |

### Clipboard
| Key | Action |
|-----|--------|
| `c` | Copy thread content to clipboard as markdown |
| `y` | Yank (copy) selected text to clipboard |

### Other
| Key | Action |
|-----|--------|
| `u` | Toggle user-only message filter |
| `Escape` | Cancel/close modal |
| `q` | Quit |

## Installation

### Prerequisites

- Python 3.8 or higher
- [uv](https://github.com/astral-sh/uv) package manager
- Claude Code CLI installed and configured

### Using uv (Recommended)

Install in development/editable mode:

```bash
cd ~/dev/claude-yelp
uv sync
```

This will:
- Create a virtual environment in `.venv/`
- Install all dependencies (rich, textual, pyperclip)
- Install the package in editable mode
- Generate `uv.lock` file for reproducible builds

### Install globally

```bash
./install.sh
```

This installs `claude-yelp` as a uv tool and creates a `clod` alias in `~/.local/bin/`.

## Usage

### Launch the TUI

```bash
clod              # Open session browser
clod 10           # Open and jump to session #10
clod +10          # Same as above
```

### Create a tagged session

```bash
clod my-feature   # Create a new session tagged "my-feature"
clod -t scratch   # Create a temporary session (deleted on exit)
```

### Other options

```bash
clod --debug      # Enable debug logging to /tmp/claude-yelp-debug.log
clod --help       # Show help
```

## Project Structure

```
claude-yelp/
├── claude_yelp.py          # Main application (~1000 lines)
├── pyproject.toml          # Project configuration and dependencies
├── install.sh              # Global install script
├── uninstall.sh            # Uninstall script
├── README.md               # This file
├── LICENSE                 # MIT License
├── .gitignore              # Git ignore rules
├── .pre-commit-config.yaml # Pre-commit hooks
└── .venv/                  # Virtual environment (created by uv, gitignored)
```

## Architecture

### Core Components

The application is built using the **Textual** framework for the TUI and consists of several key classes:

#### 1. `Session` Class
Represents a single Claude session with:
- `session_id`: Unique identifier (UUID format)
- `project_path`: Project directory path where the session was created
- `file_path`: Full path to the `.jsonl` session file
- `first_message`: First user message (for preview)
- `timestamp`: Session creation timestamp (ISO format or milliseconds since epoch)
- `tag`: Optional user-defined tag for the session
- `_messages`: Cached list of conversation messages

**Key Methods:**
- `load_messages()`: Parses the `.jsonl` file and extracts user/assistant messages
- `display_name`: Property that returns formatted name with tag if present
- `date_str`: Property that formats timestamp as readable date

#### 2. `SessionManager` Class
Manages session discovery, loading, tagging, and deletion:

**Initialization:**
- Scans `~/.claude/projects/` directory structure
- Each project directory is named with encoded path (e.g., `-home-ilya-levin-dev-devops`)
- Finds all `.jsonl` files (skips `agent-*.jsonl` files)
- Loads tags from `~/.claude/claude-yelp-tags.json`
- Sorts sessions by timestamp (most recent first)

**Key Methods:**
- `_discover_sessions()`: Scans filesystem and builds session list
- `_decode_project_path()`: Reconstructs filesystem path from Claude's dash-encoded directory names
- `_load_tags()` / `_save_tags()`: Persists user tags to JSON file
- `tag_session(session_id, tag)`: Adds/updates a tag for a session
- `start_session(session_id)`: Launches Claude CLI with `--resume` flag
- `delete_session(session_id)`: Deletes a session file and its tag

#### 3. `SessionList` Widget (ListView)
Custom Textual widget that displays the session list:
- Each item shows: `number | date | [session-id] tag | project-name`
- Supports filtering via search
- Tracks selected index for navigation

#### 4. `ThreadView` Widget (ScrollableContainer)
Displays the conversation thread for selected session:
- Renders messages as Rich Markdown
- Supports user-only filtering
- Supports search term highlighting
- Scrollable with keyboard navigation

#### 5. `ClaudeYelpApp` Class (App)
Main Textual application with two-panel layout:

```
┌─────────────────────────────────────┐
│ Header                              │
├──────────┬──────────────────────────┤
│          │                          │
│ Session  │  Thread View             │
│ List     │  (Scrollable)            │
│ (30%)    │  (70%)                   │
│          │                          │
├──────────┴──────────────────────────┤
│ Footer (keyboard shortcuts)         │
└─────────────────────────────────────┘
```

### Data Flow

1. **Startup:**
   ```
   main() -> SessionManager() -> _discover_sessions() -> builds Session objects
   -> ClaudeYelpApp(session_manager) -> compose() -> mounts widgets
   -> on_mount() -> SessionList._populate() -> displays sessions
   ```

2. **Session Selection:**
   ```
   User presses Down -> action_move_down() -> session_list.index += 1
   -> thread_view.update_session() -> session.load_messages() -> parse .jsonl
   -> render markdown -> update ThreadContent widget
   ```

3. **Tagging:**
   ```
   User presses t -> action_tag_session() -> push_screen(TagInputScreen)
   -> User enters tag -> session_manager.tag_session()
   -> save to ~/.claude/claude-yelp-tags.json -> refresh display
   ```

4. **Session Resume:**
   ```
   User presses s -> action_copy_session_command() -> app.exit(result)
   -> main() handles result -> os.chdir(project_dir)
   -> os.execvp('claude', ['claude', '--resume', session_id])
   ```

5. **New Session (Ctrl+n or CLI):**
   ```
   User presses Ctrl+n -> action_new_session() -> push_screen(input)
   -> User enters name -> app.exit(result) -> main() calls create_tagged_session()
   -> claude -p creates session -> saves tag -> os.execvpe to resume
   ```

## Claude Session Storage Structure

```
~/.claude/
├── projects/
│   ├── -home-ilya-levin-dev-devops/
│   │   ├── 0b31a378-e801-47fa-8470-b004292597ed.jsonl
│   │   ├── 1b93a609-f5bf-4898-a67f-0781768f83da.jsonl
│   │   └── agent-*.jsonl (skipped)
│   ├── -home-ilya-levin-dev-healthshield/
│   │   └── ...
│   └── ...
├── history.jsonl          # Session history
└── claude-yelp-tags.json  # User tags (created by claude-yelp)
```

## Dependencies

- **textual** (>=0.40.0): TUI framework — widgets, layout, keyboard handling, CSS-like styling
- **rich** (>=13.0.0): Terminal formatting and markdown rendering
- **pyperclip** (>=1.8.2): Cross-platform clipboard access

## Troubleshooting

### Sessions not showing

- Check that `~/.claude/projects/` exists and contains session files
- Verify session files are `.jsonl` format
- Check file permissions (read access required)
- Run with `--debug` and check `/tmp/claude-yelp-debug.log`

### Tags not persisting

- Check write permissions on `~/.claude/` directory
- Verify `claude-yelp-tags.json` is being created

### Claude CLI not starting

- Verify `claude` command is in PATH
- Check that project directory exists
- Verify session ID is valid

### Clipboard not working

- Install `xclip` for X11 selection support (`y` key)
- `pyperclip` requires a clipboard mechanism (xclip, xsel, or similar)

## Known Limitations

1. **Single tag per session**: Sessions can only have one tag
2. **No project grouping**: Sessions shown in flat list, not grouped by project
3. **No session preview in list**: Can't see message count or conversation length in list view
4. **No token/cost metadata**: Doesn't display token counts or costs

## License

MIT License - see LICENSE file for details.

## Credits

- Built with [Textual](https://textual.textualize.io/) TUI framework
- Uses [Rich](https://rich.readthedocs.io/) for terminal formatting
- Managed with [uv](https://github.com/astral-sh/uv) package manager
- Designed for [Claude Code CLI](https://claude.ai/code)
