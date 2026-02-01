# Claude Yelp

A terminal-based session manager for Claude Code CLI. Browse and manage your Claude sessions with a simple two-panel interface built using Python and Textual.

## Overview

Claude Yelp provides a TUI (Text User Interface) for managing Claude Code CLI sessions. It scans your local Claude session storage, displays them in an organized list, and allows you to browse conversation threads, tag sessions for easy identification, and quickly resume any session.

## Features

- **Two-panel interface**: Left panel shows session list, right panel shows conversation thread
- **Keyboard navigation**: 
  - `↑/↓` - Navigate session list
  - `PageUp/PageDown` - Scroll conversation thread
  - `t` - Tag a session
  - `s` - Select and start a session with Claude CLI
  - `q` - Quit the application
- **Session tagging**: Tag sessions for easy identification (tags shown in list, ID with braces)
- **Session discovery**: Automatically discovers all Claude sessions from `~/.claude/projects/`
- **Session resumption**: Directly launch Claude CLI with `--resume` flag for selected session

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
- Install all dependencies (rich, textual)
- Install the package in editable mode
- Generate `uv.lock` file for reproducible builds

## Usage

After running `uv sync`, you can use the tool in several ways:

**Option 1: Using uv run (Recommended for development)**
```bash
uv run claude-yelp
```

**Option 2: Install globally as a tool**
```bash
uv pip install -e .
claude-yelp
```

**Option 3: Activate the virtual environment**
```bash
source .venv/bin/activate  # or `.venv\Scripts\activate` on Windows
claude-yelp
```

## Project Structure

```
claude-yelp/
├── claude_yelp.py          # Main application (530+ lines)
├── pyproject.toml          # Project configuration and dependencies
├── README.md               # This file
├── LICENSE                 # MIT License
├── .gitignore             # Git ignore rules
└── .venv/                 # Virtual environment (created by uv, gitignored)
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
- `timestamp`: Session creation timestamp (milliseconds since epoch)
- `tag`: Optional user-defined tag for the session
- `_messages`: Cached list of conversation messages

**Key Methods:**
- `load_messages()`: Parses the `.jsonl` file and extracts user/assistant messages
- `display_name`: Property that returns formatted name with tag if present
- `date_str`: Property that formats timestamp as readable date

#### 2. `SessionManager` Class
Manages session discovery, loading, and tagging:

**Initialization:**
- Scans `~/.claude/projects/` directory structure
- Each project directory is named with encoded path (e.g., `-home-ilya-levin-dev-devops`)
- Finds all `.jsonl` files (skips `agent-*.jsonl` files)
- Loads tags from `~/.claude/claude-yelp-tags.json`
- Sorts sessions by timestamp (most recent first)

**Key Methods:**
- `_discover_sessions()`: Scans filesystem and builds session list
- `_load_tags()` / `_save_tags()`: Persists user tags to JSON file
- `tag_session(session_id, tag)`: Adds/updates a tag for a session
- `start_session(session_id)`: Launches Claude CLI with `--resume` flag

**Session File Format:**
Claude stores sessions as JSONL (JSON Lines) files. Each line is a JSON object representing:
- User messages: `{"type": "user", "message": {"role": "user", "content": "..."}, ...}`
- Assistant messages: `{"type": "assistant", "message": {"role": "assistant", "content": [...]}, ...}`
- Metadata: File snapshots, summaries, etc.

#### 3. `SessionList` Widget (ListView)
Custom Textual widget that displays the session list:

**Features:**
- Populates on mount (after widget is ready)
- Each item shows: `[session-id] tag | project-name | date`
- Tagged sessions show tag after ID in brackets
- Uses `ListItem(Static(display))` for each entry
- Tracks selected index for navigation

**Methods:**
- `_populate()`: Builds list items from session manager
- `get_selected_session()`: Returns currently selected Session object

#### 4. `ThreadView` Widget (ScrollableContainer)
Displays the conversation thread for selected session:

**Features:**
- Uses `ScrollableContainer` for scrollable content
- Contains `ThreadContent` (Static widget) with markdown rendering
- Updates when session selection changes
- Handles pending updates if called before widget is mounted

**Content Format:**
- Session metadata (ID, project, date, tag)
- User messages with 👤 icon
- Assistant messages with 🤖 icon
- Error messages with ❌ icon
- Rendered using Rich Markdown for formatting

#### 5. `ClaudeYelpApp` Class (App)
Main Textual application:

**Layout:**
```
┌─────────────────────────────────────┐
│ Header                              │
├──────────┬──────────────────────────┤
│          │                          │
│ Session  │  Thread View             │
│ List     │  (Scrollable)             │
│ (30%)    │  (70%)                    │
│          │                          │
├──────────┴──────────────────────────┤
│ Footer (keyboard shortcuts)        │
└─────────────────────────────────────┘
```

**Keyboard Bindings:**
- `up` / `down`: Navigate session list
- `pageup` / `pagedown`: Scroll thread view (focuses thread view)
- `t`: Open tag input dialog
- `s`: Exit app and start Claude with selected session
- `q`: Quit application

**Event Handlers:**
- `on_mount()`: Initializes widget references and displays first session
- `on_list_view_selected()`: Updates thread view when selection changes
- `action_*()`: Methods for keyboard actions

### Data Flow

1. **Startup:**
   ```
   main() → SessionManager() → _discover_sessions() → builds Session objects
   → ClaudeYelpApp(session_manager) → compose() → mounts widgets
   → on_mount() → SessionList._populate() → displays sessions
   ```

2. **Session Selection:**
   ```
   User presses ↓ → action_move_down() → session_list.index += 1
   → on_list_view_selected() → thread_view.update_session()
   → session.load_messages() → parse .jsonl file
   → render markdown → update ThreadContent widget
   ```

3. **Tagging:**
   ```
   User presses t → action_tag_session() → push_screen(TagInputScreen)
   → User enters tag → handle_tag() → session_manager.tag_session()
   → save to ~/.claude/claude-yelp-tags.json
   → session_list._populate() → refresh display
   ```

4. **Session Start:**
   ```
   User presses s → action_select_session() → app.exit()
   → session_manager.start_session() → subprocess.run(['claude', '--resume', session_id])
   → changes to project directory → launches Claude CLI
   ```

## Claude Session Storage Structure

Claude Code stores sessions in the following structure:

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
├── history.jsonl          # Session history (optional, not used by claude-yelp)
└── claude-yelp-tags.json  # User tags (created by claude-yelp)
```

**Project Directory Naming:**
- Directories are named with encoded paths: `/home/user/project` → `-home-user-project`
- Leading slash becomes leading dash
- Path separators become dashes

**Session Files:**
- Each session is a `.jsonl` file named with the session UUID
- Files contain conversation history in JSON Lines format
- Agent sessions (files starting with `agent-`) are excluded

## Dependencies

### Required Dependencies

- **rich** (>=13.0.0): Terminal formatting and markdown rendering
  - Used for: Markdown rendering in thread view, text formatting
- **textual** (>=0.40.0): TUI framework
  - Used for: Application framework, widgets, layout, keyboard handling

### Why These Dependencies?

- **Textual**: Modern Python TUI framework built on Rich. Provides:
  - Widget system (ListView, Static, ScrollableContainer, etc.)
  - Layout management (Horizontal, Vertical containers)
  - Event handling and keyboard bindings
  - CSS-like styling
  - Cross-platform terminal support

- **Rich**: Powerful terminal output library:
  - Markdown rendering for conversation threads
  - Text formatting and styling
  - Used by Textual internally

## Configuration

### Tag Storage

Tags are stored in `~/.claude/claude-yelp-tags.json` as a simple JSON object:
```json
{
  "session-id-1": "tag-name",
  "session-id-2": "another-tag"
}
```

Tags persist across application restarts and are automatically loaded on startup.

### Claude Directory

The application looks for Claude data in `~/.claude/` by default. This can be customized by modifying the `SessionManager.__init__()` method to accept a custom path.

## Development

### Setup Development Environment

```bash
cd ~/dev/claude-yelp
uv sync
```

### Running in Development

```bash
uv run claude-yelp
```

### Code Structure

**Main Entry Point:**
- `main()` function at bottom of `claude_yelp.py`
- Creates `SessionManager` and `ClaudeYelpApp`
- Calls `app.run()` to start Textual event loop

**Key Design Decisions:**
1. **Single File**: All code in one file for simplicity and portability
2. **Lazy Loading**: Messages loaded only when session is selected
3. **Caching**: Session messages cached after first load
4. **Error Handling**: Graceful degradation if files are missing/corrupted
5. **Widget Lifecycle**: Proper handling of widget mounting order

### Adding Features

**To add a new keyboard shortcut:**
1. Add `Binding` to `ClaudeYelpApp.BINDINGS`
2. Add `action_*()` method to handle the action
3. Update footer display (automatic with Textual)

**To add session metadata display:**
1. Modify `Session` class to extract additional data
2. Update `ThreadView._do_update_session()` to display it
3. Update `SessionList._populate()` if needed in list view

**To change layout:**
1. Modify `ClaudeYelpApp.compose()` to change widget structure
2. Update CSS in `ClaudeYelpApp.CSS` for styling
3. Adjust width percentages or use Textual layout system

### Testing

Currently no automated tests. Manual testing checklist:
- [ ] App starts without errors
- [ ] Sessions are discovered and displayed
- [ ] Navigation works (up/down arrows)
- [ ] Thread view updates when selecting sessions
- [ ] Tagging works and persists
- [ ] Session start launches Claude CLI correctly
- [ ] Scrolling works in thread view
- [ ] Handles empty session list gracefully
- [ ] Handles corrupted/missing session files gracefully

## Troubleshooting

### App doesn't start

**Error: "ImportError: cannot import name 'ScrollView'"**
- Solution: Updated to use `ScrollableContainer` instead (Textual API change)

**Error: "TypeError: unsupported operand type(s) for /: 'str' and 'int'"**
- Solution: Fixed timestamp handling in `date_str` property to handle string timestamps

**Error: "MountError: Can't mount widget(s) before SessionList is mounted"**
- Solution: Moved `_populate()` to `on_mount()` method

### Sessions not showing

- Check that `~/.claude/projects/` exists and contains session files
- Verify session files are `.jsonl` format
- Check file permissions (read access required)
- Look for errors in console output

### Tags not persisting

- Check write permissions on `~/.claude/` directory
- Verify `claude-yelp-tags.json` is being created
- Check JSON file format is valid

### Claude CLI not starting

- Verify `claude` command is in PATH
- Check that project directory exists (session manager tries to cd to it)
- Verify session ID is valid
- Check Claude CLI is properly installed

## Known Limitations

1. **No search/filter**: Can't search sessions by content or filter by project
2. **No session deletion**: Can only view and resume, not delete
3. **No export**: Can't export conversations to other formats
4. **Limited metadata**: Only shows basic session info, not cost, token counts, etc.
5. **Single tag per session**: Sessions can only have one tag
6. **No project grouping**: Sessions shown in flat list, not grouped by project
7. **No session preview**: Can't see message count or conversation length in list

## Future Improvements

### Potential Features

1. **Search/Filter:**
   - Search sessions by content
   - Filter by project, date range, tags
   - Fuzzy search for session IDs

2. **Enhanced Metadata:**
   - Show token counts, costs, duration
   - Display message count
   - Show last activity time

3. **Session Management:**
   - Delete sessions
   - Archive old sessions
   - Merge sessions

4. **Export/Import:**
   - Export conversations to markdown
   - Export to other formats (JSON, HTML)
   - Import tags from other tools

5. **UI Enhancements:**
   - Project grouping in session list
   - Color coding by project or tag
   - Preview snippets in list
   - Multiple tags per session
   - Tag colors/styles

6. **Performance:**
   - Lazy load message content (only load when viewing)
   - Cache parsed messages
   - Background session discovery
   - Incremental updates

7. **Integration:**
   - Open session in Claude Desktop
   - Copy session ID to clipboard
   - Share session links
   - Integration with Claude API

## Contributing

This is a personal project, but contributions are welcome. Key areas for contribution:
- Testing and bug fixes
- Performance improvements
- UI/UX enhancements
- Documentation improvements
- Feature additions (see Future Improvements)

## License

MIT License - see LICENSE file for details.

## Credits

- Built with [Textual](https://textual.textualize.io/) TUI framework
- Uses [Rich](https://rich.readthedocs.io/) for terminal formatting
- Managed with [uv](https://github.com/astral-sh/uv) package manager
- Designed for [Claude Code CLI](https://claude.ai/code)

## Related Projects

- [Claude Code](https://claude.ai/code) - The CLI tool this manages
- [Textual](https://textual.textualize.io/) - The TUI framework used
- [Rich](https://rich.readthedocs.io/) - Terminal formatting library

---

**Last Updated**: 2025-01-01
**Version**: 0.1.0
**Python**: 3.8+
**Status**: Active Development
