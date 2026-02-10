#!/usr/bin/env python3
"""
Claude Yelp - A terminal-based session manager for Claude Code CLI
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, ListItem, ListView, Static

DEBUG_LOG_FILE = os.path.join(tempfile.gettempdir(), "claude-yelp-debug.log")
DEBUG_ENABLED = False


def _debug_log(msg: str):
    """Write debug message to file (only if DEBUG_ENABLED)"""
    if not DEBUG_ENABLED:
        return
    with open(DEBUG_LOG_FILE, "a") as f:
        f.write(f"{msg}\n")
        f.flush()


class EscapableInput(Input):
    """Input that handles ESC to dismiss parent modal screen"""

    BINDINGS = [
        Binding("escape", "cancel_input", "Cancel", priority=True),
    ]

    def action_cancel_input(self) -> None:
        """Cancel input and dismiss parent screen"""
        screen = self.screen
        if isinstance(screen, ModalScreen):
            screen.dismiss(None)


class Session:
    """Represents a Claude session"""

    def __init__(
        self,
        session_id: str,
        project_path: str,
        file_path: str,
        first_message: Optional[str] = None,
        timestamp: Optional[int] = None,
    ):
        self.session_id = session_id
        self.project_path = project_path
        self.file_path = file_path
        self.first_message = first_message or ""
        self.timestamp = timestamp
        self.tag: Optional[str] = None
        self._messages: Optional[List[Dict]] = None

    @property
    def display_name(self) -> str:
        """Get display name for the session"""
        if self.tag:
            return f"[{self.session_id[:8]}] {self.tag}"
        return f"[{self.session_id[:8]}]"

    @property
    def project_name(self) -> str:
        """Get project name from path"""
        return os.path.basename(self.project_path) if self.project_path else "unknown"

    @property
    def date_str(self) -> str:
        """Get formatted date string"""
        _debug_log(
            f"date_str called for session {self.session_id[:8]}, "
            f"timestamp={repr(self.timestamp)}, type={type(self.timestamp)}"
        )
        if self.timestamp:
            try:
                # Handle ISO format timestamp (e.g., "2025-11-25T12:36:37.257Z")
                if isinstance(self.timestamp, str):
                    # Remove trailing Z and parse ISO format
                    ts_str = self.timestamp.rstrip("Z")
                    _debug_log(f"  Parsing ISO string: {ts_str}")
                    # Handle milliseconds in ISO format
                    if "." in ts_str:
                        dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S.%f")
                    else:
                        dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")
                    result = dt.strftime("%Y-%m-%d %H:%M")
                    _debug_log(f"  Result: {result}")
                    return result
                # Handle numeric timestamp (milliseconds)
                elif isinstance(self.timestamp, (int, float)):
                    ts = int(self.timestamp)
                    _debug_log(f"  Parsing numeric timestamp: {ts}")
                    if ts > 0:
                        dt = datetime.fromtimestamp(ts / 1000)
                        result = dt.strftime("%Y-%m-%d %H:%M")
                        _debug_log(f"  Result: {result}")
                        return result
            except (ValueError, TypeError, OSError) as e:
                _debug_log(f"  Error parsing timestamp: {e}")
                pass
        _debug_log("  Returning 'unknown'")
        return "unknown"

    def load_messages(self) -> List[Dict]:
        """Load messages from the session file"""
        if self._messages is not None:
            return self._messages

        messages = []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "user" and "message" in entry:
                            msg = entry["message"]
                            if isinstance(msg.get("content"), str):
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": msg["content"],
                                        "timestamp": entry.get("timestamp"),
                                    }
                                )
                            elif isinstance(msg.get("content"), list):
                                for item in msg["content"]:
                                    if item.get("type") == "text":
                                        messages.append(
                                            {
                                                "role": "user",
                                                "content": item.get("text", ""),
                                                "timestamp": entry.get("timestamp"),
                                            }
                                        )
                        elif entry.get("type") == "assistant" and "message" in entry:
                            msg = entry["message"]
                            if isinstance(msg.get("content"), list):
                                for item in msg["content"]:
                                    if item.get("type") == "text":
                                        messages.append(
                                            {
                                                "role": "assistant",
                                                "content": item.get("text", ""),
                                                "timestamp": entry.get("timestamp"),
                                            }
                                        )
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            messages.append({"role": "error", "content": f"Error loading messages: {e}"})

        self._messages = messages
        return messages


class SessionManager:
    """Manages Claude sessions"""

    def __init__(self, claude_dir: Path = None):
        _debug_log("SessionManager.__init__ starting")
        if claude_dir is None:
            claude_dir = Path.home() / ".claude"
        self.claude_dir = claude_dir
        self.projects_dir = claude_dir / "projects"
        self.history_file = claude_dir / "history.jsonl"
        self.tags_file = claude_dir / "claude-yelp-tags.json"
        self.sessions: List[Session] = []
        self.tags: Dict[str, str] = {}
        self._load_tags()
        _debug_log("Calling _discover_sessions")
        self._discover_sessions()
        _debug_log(f"SessionManager.__init__ done, found {len(self.sessions)} sessions")

    def _load_tags(self):
        """Load session tags from file"""
        if self.tags_file.exists():
            try:
                with open(self.tags_file, "r") as f:
                    self.tags = json.load(f)
            except Exception:
                self.tags = {}

    def _save_tags(self):
        """Save session tags to file"""
        try:
            with open(self.tags_file, "w") as f:
                json.dump(self.tags, f, indent=2)
        except Exception as e:
            _debug_log(f"Failed to save tags: {e}")

    def _decode_project_path(self, encoded_name: str) -> str:
        """Decode Claude's encoded project path back to actual filesystem path.

        Claude encodes paths like /home/ilya.levin/dev/project as:
        -home-ilya-levin-dev-project (dots and slashes become dashes)

        We need to decode this back, handling dots in usernames and dashes in dir names.
        """
        # Remove leading dash and split by dash
        if encoded_name.startswith("-"):
            encoded_name = encoded_name[1:]

        parts = encoded_name.split("-")

        # Try to reconstruct the path by checking which combinations exist
        # Start from root and build up, checking filesystem
        current_path = "/"
        i = 0

        while i < len(parts):
            part = parts[i]

            # Try just this part
            test_path = os.path.join(current_path, part)
            if os.path.exists(test_path):
                current_path = test_path
                i += 1
                continue

            # Try combining with next parts using different separators
            found = False
            # Try progressively longer combinations with dots and dashes
            for j in range(i + 1, min(i + 6, len(parts) + 1)):  # Try up to 5 parts combined
                # Try with dots (for usernames like ilya.levin)
                combined_dot = ".".join(parts[i:j])
                test_path = os.path.join(current_path, combined_dot)
                if os.path.exists(test_path):
                    current_path = test_path
                    i = j
                    found = True
                    break

                # Try with dashes (for dir names like flex-host-agent)
                combined_dash = "-".join(parts[i:j])
                test_path = os.path.join(current_path, combined_dash)
                if os.path.exists(test_path):
                    current_path = test_path
                    i = j
                    found = True
                    break

            if not found:
                # Just use the part as-is and continue (path may not exist)
                current_path = os.path.join(current_path, part)
                i += 1

        return current_path

    def _discover_sessions(self):
        """Discover all Claude sessions"""
        _debug_log("_discover_sessions started")
        sessions = []

        # Load from history.jsonl
        history_sessions = {}
        if self.history_file.exists():
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            display = entry.get("display", "")
                            project = entry.get("project", "")
                            timestamp = entry.get("timestamp", 0)

                            # Try to extract session ID from project files
                            # We'll match this later with actual session files
                            history_sessions[project] = {"display": display, "timestamp": timestamp}
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                _debug_log(f"Failed to parse history file: {e}")

        # Scan projects directory for session files
        _debug_log(f"Scanning projects dir: {self.projects_dir}")
        if self.projects_dir.exists():
            for project_dir in self.projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue

                # Decode project path
                # e.g., -home-ilya-levin-dev-devops -> /home/ilya.levin/dev/devops
                project_path = self._decode_project_path(project_dir.name)
                _debug_log(f"  Project: {project_dir.name} -> {project_path}")

                for session_file in project_dir.glob("*.jsonl"):
                    session_id = session_file.stem

                    # Skip agent files
                    if session_id.startswith("agent-"):
                        continue

                    # Get first message and timestamp
                    first_message = None
                    timestamp = None

                    try:
                        with open(session_file, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    entry = json.loads(line)
                                    if entry.get("type") == "user" and "message" in entry:
                                        msg = entry["message"]
                                        if isinstance(msg.get("content"), str):
                                            first_message = msg["content"][:100]
                                            timestamp = entry.get("timestamp")
                                            break
                                        elif isinstance(msg.get("content"), list):
                                            for item in msg["content"]:
                                                if item.get("type") == "text":
                                                    first_message = item.get("text", "")[:100]
                                                    timestamp = entry.get("timestamp")
                                                    break
                                            if first_message:
                                                break
                                except json.JSONDecodeError:
                                    continue
                    except Exception as e:
                        _debug_log(f"Failed to read session file {session_file}: {e}")

                    _debug_log(f"Creating session {session_id[:8]}: timestamp={repr(timestamp)}")
                    session = Session(
                        session_id=session_id,
                        project_path=project_path,
                        file_path=str(session_file),
                        first_message=first_message,
                        timestamp=timestamp,
                    )

                    # Apply tag if exists
                    if session_id in self.tags:
                        session.tag = self.tags[session_id]

                    sessions.append(session)

        # Sort by timestamp (most recent first)
        # Handle both ISO format strings and numeric timestamps
        def get_timestamp(s):
            ts = s.timestamp
            if ts is None:
                return ""
            if isinstance(ts, str):
                # ISO format strings sort lexicographically correctly
                return ts
            # Numeric timestamp - convert to ISO-like string for consistent sorting
            try:
                dt = datetime.fromtimestamp(int(ts) / 1000)
                return dt.isoformat()
            except (ValueError, TypeError, OSError):
                return ""

        sessions.sort(key=get_timestamp, reverse=True)
        self.sessions = sessions

    def tag_session(self, session_id: str, tag: str):
        """Tag a session"""
        self.tags[session_id] = tag
        self._save_tags()

        # Update session object
        for session in self.sessions:
            if session.session_id == session_id:
                session.tag = tag
                break

    def start_session(self, session_id: str) -> bool:
        """Start a Claude session with the given session ID"""
        # Find the session
        session = None
        for s in self.sessions:
            if s.session_id == session_id:
                session = s
                break

        if not session:
            return False

        # Change to project directory
        project_dir = session.project_path
        if not os.path.exists(project_dir):
            project_dir = os.path.dirname(project_dir)
            if not os.path.exists(project_dir):
                project_dir = os.path.expanduser("~")

        # Start Claude with --resume flag
        try:
            cmd = ["claude", "--resume", session_id]
            subprocess.run(cmd, cwd=project_dir)
            return True
        except Exception as e:
            print(f"Error starting session: {e}", file=sys.stderr)
            return False

    def delete_session(self, session_id: str) -> bool:
        """Delete a session file"""
        # Find the session
        session = None
        for s in self.sessions:
            if s.session_id == session_id:
                session = s
                break

        if not session:
            return False

        # Delete the session file
        try:
            if os.path.exists(session.file_path):
                os.remove(session.file_path)

                # Remove from sessions list
                self.sessions = [s for s in self.sessions if s.session_id != session_id]

                # Remove tag if exists
                if session_id in self.tags:
                    del self.tags[session_id]
                    self._save_tags()

                return True
            return False
        except Exception as e:
            print(f"Error deleting session: {e}", file=sys.stderr)
            return False


class SessionList(ListView):
    """Custom list view for sessions"""

    def __init__(self, session_manager: SessionManager, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_manager = session_manager
        self.selected_index = 0
        self._sessions_to_display: List[Session] = []

    def on_mount(self):
        """Called when widget is mounted"""
        self._populate()

    def _populate(
        self,
        sessions: Optional[List[Session]] = None,
        preserve_index: bool = False,
        initial_index: Optional[int] = None,
    ):
        """Populate the list with sessions"""
        old_index = self.index if hasattr(self, "index") and preserve_index else None
        self.clear()

        # Use provided sessions or default to all sessions
        if sessions is None:
            sessions = self.session_manager.sessions

        self._sessions_to_display = sessions

        # Always use max 4 digits for alignment
        width = 4

        for i, session in enumerate(sessions, start=1):
            # Format number with right alignment and space padding (max 4 digits)
            number_str = str(i).rjust(width, " ")
            display = (
                f"{number_str} {session.date_str} | {session.display_name} | {session.project_name}"
            )
            list_item = ListItem(Static(display))
            self.append(list_item)

        # Set index after all items are added
        # Use call_after_refresh to ensure items are mounted before setting index and highlighting
        def set_index_and_highlight():
            if sessions:
                target_idx = None
                if initial_index is not None and 0 <= initial_index < len(sessions):
                    target_idx = initial_index
                elif preserve_index and old_index is not None and old_index < len(sessions):
                    target_idx = old_index
                else:
                    target_idx = 0

                if target_idx is not None:
                    self.index = target_idx
                    # Manually ensure the highlight is set
                    try:
                        if hasattr(self, "_nodes") and target_idx < len(self._nodes):
                            highlighted_item = self._nodes[target_idx]
                            if isinstance(highlighted_item, ListItem):
                                highlighted_item.highlighted = True
                    except (IndexError, AttributeError, TypeError):
                        pass

        # Set index immediately
        if sessions:
            if initial_index is not None and 0 <= initial_index < len(sessions):
                self.index = initial_index
            elif preserve_index and old_index is not None and old_index < len(sessions):
                self.index = old_index
            else:
                self.index = 0

        # Also set it after refresh to ensure highlighting is applied
        self.call_after_refresh(set_index_and_highlight)

    def get_sessions(self) -> List[Session]:
        """Get the list of sessions currently displayed"""
        return self._sessions_to_display

    def get_selected_session(self) -> Optional[Session]:
        """Get the currently selected session"""
        idx = self.index if hasattr(self, "index") and self.index is not None else 0
        sessions = (
            self._sessions_to_display
            if self._sessions_to_display
            else self.session_manager.sessions
        )
        if 0 <= idx < len(sessions):
            return sessions[idx]
        return None


class ThreadContent(Static):
    """Content widget for thread view - allows text selection"""

    ALLOW_SELECT = True
    can_focus = True


class ThreadView(ScrollableContainer):
    """View for displaying conversation thread - allows text selection"""

    ALLOW_SELECT = True

    BINDINGS = [
        Binding("up", "scroll_up", "Scroll Up", priority=True),
        Binding("down", "scroll_down", "Scroll Down", priority=True),
        Binding("pageup", "scroll_page_up", "Page Up", priority=True),
        Binding("pagedown", "scroll_page_down", "Page Down", priority=True),
    ]

    def __init__(self, session_manager: SessionManager, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_manager = session_manager
        self.current_session: Optional[Session] = None
        self._pending_update: Optional[Session] = None

    def action_scroll_up(self):
        """Scroll up in thread view"""
        self.scroll_up(animate=False)

    def action_scroll_down(self):
        """Scroll down in thread view"""
        self.scroll_down(animate=False)

    def action_scroll_page_up(self):
        """Scroll page up in thread view"""
        self.scroll_page_up(animate=False)

    def action_scroll_page_down(self):
        """Scroll page down in thread view"""
        self.scroll_page_down(animate=False)

    def compose(self):
        """Compose the widget"""
        yield ThreadContent("", markup=True, id="thread-content")

    def on_mount(self):
        """Called when widget is mounted"""
        if self._pending_update:
            self._do_update_session(self._pending_update, user_only=False)
            self._pending_update = None

    def update_session(self, session: Session, user_only: bool = False, highlight_term: str = ""):
        """Update the view with a session's messages"""
        self.current_session = session
        try:
            self._do_update_session(session, user_only, highlight_term)
        except Exception:
            # Widget not mounted yet, store for later
            self._pending_update = session

    def _highlight_text(self, text: str, term: str) -> str:
        """Highlight search term in text using Rich markup"""
        if not term:
            return text

        import re

        # Case-insensitive replacement with highlight markup
        # Using [reverse] for highlighting as it works well in terminals
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        return pattern.sub(lambda m: f"[reverse yellow]{m.group(0)}[/reverse yellow]", text)

    def _do_update_session(
        self, session: Session, user_only: bool = False, highlight_term: str = ""
    ):
        """Internal method to update the session view"""
        messages = session.load_messages()

        # Filter to user messages only if requested
        if user_only:
            messages = [msg for msg in messages if msg.get("role") == "user"]

        from rich.markdown import Markdown
        from rich.text import Text

        content = []
        content.append(f"# Session: {session.session_id}\n\n")
        content.append(f"**Project:** `{session.project_path}`\n\n")
        content.append(f"**Date:** {session.date_str}\n\n")
        if session.tag:
            content.append(f"**Tag:** {session.tag}\n\n")
        if user_only:
            content.append("**Filter:** User messages only\n\n")
        if highlight_term:
            content.append(f"**Search:** `{highlight_term}`\n\n")
        content.append("---\n\n")

        # Group consecutive messages with the same role
        if not messages:
            content.append("*No messages found in this session.*\n")
        else:
            i = 0
            while i < len(messages):
                current_role = messages[i].get("role", "unknown")
                combined_texts = [messages[i].get("content", "")]

                # Collect consecutive messages with the same role
                j = i + 1
                while j < len(messages) and messages[j].get("role") == current_role:
                    combined_texts.append(messages[j].get("content", ""))
                    j += 1

                # Combine the texts
                combined_text = "\n\n".join(combined_texts)

                # Highlight search term if provided
                if highlight_term:
                    combined_text = self._highlight_text(combined_text, highlight_term)

                # Format based on role
                if current_role == "user":
                    content.append(f"## 👤 User\n\n{combined_text}\n\n")
                elif current_role == "assistant":
                    content.append(f"## 🤖 Assistant\n\n{combined_text}\n\n")
                elif current_role == "error":
                    content.append(f"## ❌ Error\n\n{combined_text}\n\n")
                else:
                    content.append(f"## {current_role.title()}\n\n{combined_text}\n\n")

                i = j

        content_widget = self.query_one("#thread-content", ThreadContent)

        if highlight_term:
            # Use Rich Text with markup for highlighting (can't use Markdown with highlights)
            content_widget.update(Text.from_markup("".join(content)))
        else:
            # Use Markdown for normal rendering
            markdown = Markdown("".join(content))
            content_widget.update(markdown)


class HelpScreen(ModalScreen):
    """Modal screen showing keyboard shortcuts"""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", priority=True),
        Binding("ctrl+k", "dismiss", "Close", priority=True),
        Binding("up", "scroll_up", "Scroll Up", priority=True),
        Binding("down", "scroll_down", "Scroll Down", priority=True),
        Binding("left", "noop", "", priority=True),
        Binding("right", "noop", "", priority=True),
        Binding("pageup", "scroll_page_up", "Page Up", priority=True),
        Binding("pagedown", "scroll_page_down", "Page Down", priority=True),
        Binding("q", "dismiss", "Close", priority=True),
    ]

    HELP_TEXT = """\
[b]Session List Panel (Left)[/b]
  Up / Down            Navigate sessions
  PageUp / PageDown    Scroll by 10 sessions
  gg                   Jump to first session
  G                    Jump to last session
  :number              Goto session by number
  /text                Filter sessions by text
  s                    Start (resume) session
  t                    Tag session
  d                    Delete session
  e                    Export session to markdown
  Ctrl+n               Create new tagged session

[b]Thread Panel (Right)[/b]
  Up / Down            Scroll thread
  PageUp / PageDown    Page scroll
  gg                   Scroll to top
  G                    Scroll to bottom
  /text                Search text in thread
  n                    Next search match
  N                    Previous search match
  u                    Toggle user-only messages
  c                    Copy thread as markdown
  y                    Yank selected text

[b]General[/b]
  Left / Right         Switch panel focus
  Shift+Left / Right   Resize panels
  Ctrl+k               Toggle this help
  Ctrl+p               Command palette
  Escape               Cancel / close dialog
  q                    Quit"""

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="help-container"):
            yield Static("Keyboard Shortcuts", id="help-title")
            yield Static(self.HELP_TEXT)
            yield Static("Press ESC or Ctrl+K to close", id="help-footer")

    def on_mount(self):
        """Focus the scrollable container"""
        container = self.query_one("#help-container")
        container.focus()

    def on_key(self, event) -> None:
        """Intercept all keys to prevent leaking to parent app"""
        container = self.query_one("#help-container")
        key = event.key
        if key in ("up", "k"):
            container.scroll_up(animate=False)
        elif key in ("down", "j"):
            container.scroll_down(animate=False)
        elif key == "pageup":
            container.scroll_page_up(animate=False)
        elif key == "pagedown":
            container.scroll_page_down(animate=False)
        elif key in ("escape", "q", "ctrl+k"):
            self.dismiss()
        # Stop all keys from reaching the app
        event.stop()
        event.prevent_default()


class ClaudeYelpApp(App):
    """Main application"""

    ALLOW_SELECT = True

    CSS = """
    Screen {
        layout: vertical;
    }

    Horizontal {
        height: 1fr;
    }

    #session-list {
        width: 30%;
        border-right: solid $primary;
    }

    #thread-view {
        width: 70%;
    }

    #session-list:focus > ListItem.-highlight {
        background: $accent;
        text-style: bold;
    }

    #session-list > ListItem.-highlight {
        background: $accent;
        text-style: bold;
    }

    HelpScreen {
        align: center middle;
    }

    #help-container {
        width: 70;
        max-height: 90%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        overflow-y: auto;
    }

    #help-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #help-footer {
        text-align: center;
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        # Shown in footer bar
        Binding("ctrl+n", "new_session", "New Session", priority=True),
        Binding("s", "copy_session_command", "Start Session", priority=True),
        Binding("ctrl+k", "show_help", "Shortcuts", priority=True),
        # Hidden from footer, available via Ctrl+K help and Ctrl+P palette
        Binding("left", "focus_left", "Focus Left Panel", show=False, priority=True),
        Binding("right", "focus_right", "Focus Right Panel", show=False, priority=True),
        Binding("shift+left", "resize_left", "Resize Left", show=False, priority=True),
        Binding("shift+right", "resize_right", "Resize Right", show=False, priority=True),
        Binding("up", "move_up", "Move Up", show=False, priority=True),
        Binding("down", "move_down", "Move Down", show=False, priority=True),
        Binding("pageup", "page_up", "Page Up", show=False, priority=True),
        Binding("pagedown", "page_down", "Page Down", show=False, priority=True),
        Binding("t", "tag_session", "Tag Session", show=False, priority=True),
        Binding("e", "export_session", "Export Session", show=False, priority=True),
        Binding("d", "delete_session", "Delete Session", show=False, priority=True),
        Binding("u", "toggle_user_only", "Toggle User Only", show=False, priority=True),
        Binding("c", "copy_thread", "Copy Thread", show=False, priority=True),
        Binding("y", "yank", "Yank Selection", show=False, priority=True),
        Binding(":", "command_mode", "Command Mode", show=False, priority=True),
        Binding("/", "search_mode", "Search Mode", show=False, priority=True),
        Binding("n", "search_next", "Next Match", show=False, priority=True),
        Binding("N", "search_prev", "Previous Match", show=False, priority=True),
        Binding("escape", "escape", "Cancel/Close", priority=True),
        Binding("q", "quit", "Quit", priority=True),
        Binding("g", "go_to_top", "Go to Top", show=False, priority=True),
        Binding("G", "go_to_bottom", "Go to Bottom", show=False, priority=True),
    ]

    def check_action(self, action: str, parameters) -> bool | None:
        """Disable app actions when a modal (like HelpScreen) is active."""
        if any(isinstance(s, HelpScreen) for s in self.screen_stack):
            if action == "show_help":
                return True
            return False
        return True

    def __init__(
        self, session_manager: SessionManager, initial_session_number: Optional[int] = None
    ):
        super().__init__()
        self.session_manager = session_manager
        self.session_list: Optional[SessionList] = None
        self.thread_view: Optional[ThreadView] = None
        self.user_only_mode: bool = False
        self.search_query: str = ""
        self.filtered_sessions: List[Session] = []
        self.initial_session_number: Optional[int] = initial_session_number
        self._last_g_press: Optional[float] = None  # Track double g press
        # Thread search state
        self._thread_search_term: str = ""
        self._thread_search_matches: List[int] = []  # Character positions of matches
        self._thread_search_index: int = -1  # Current match index
        self._thread_raw_text: str = ""  # Raw text content for searching
        # Pane width (percentage for left pane)
        self._left_pane_width: int = 30

    def compose(self) -> ComposeResult:
        """Create child widgets"""
        yield Header()

        with Horizontal():
            yield SessionList(self.session_manager, id="session-list")
            yield ThreadView(self.session_manager, id="thread-view")

        yield Footer()

    def on_mount(self):
        """Called when app is mounted"""
        # Get references to widgets after they're mounted
        self.session_list = self.query_one("#session-list", SessionList)
        self.thread_view = self.query_one("#thread-view", ThreadView)

        # Set initial focus to session list
        self.set_focus(self.session_list)

        # If initial_session_number is provided, jump to that session
        if self.initial_session_number is not None:
            # Use a timer to ensure everything is fully mounted and rendered
            def jump_to_initial():
                # Make sure list is visible and has focus
                self.set_focus(self.session_list)
                self.session_list.refresh(layout=True)
                # Now jump to the session
                self._goto_session(self.initial_session_number)

            # Use both call_after_refresh and a timer to ensure it works
            self.call_after_refresh(jump_to_initial)
            self.set_timer(0.2, jump_to_initial)
        else:
            # Update thread view with first session
            session = self.session_list.get_selected_session()
            if session:
                self.thread_view.update_session(session, user_only=self.user_only_mode)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle session selection"""
        if self.session_list and self.thread_view:
            session = self.session_list.get_selected_session()
            if session:
                self.thread_view.update_session(session, user_only=self.user_only_mode)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Handle list view highlight changes"""
        # Ensure the highlighted item is visible
        pass

    def action_focus_left(self):
        """Focus the left panel (session list)"""
        if self.session_list:
            self.set_focus(self.session_list)

    def action_focus_right(self):
        """Focus the right panel (thread view)"""
        if self.thread_view:
            self.set_focus(self.thread_view)

    def action_resize_left(self):
        """Make left pane narrower"""
        if self._left_pane_width > 15:
            self._left_pane_width -= 5
            self._apply_pane_widths()

    def action_resize_right(self):
        """Make left pane wider"""
        if self._left_pane_width < 70:
            self._left_pane_width += 5
            self._apply_pane_widths()

    def _apply_pane_widths(self):
        """Apply current pane width settings"""
        if self.session_list and self.thread_view:
            self.session_list.styles.width = f"{self._left_pane_width}%"
            self.thread_view.styles.width = f"{100 - self._left_pane_width}%"

    def action_move_up(self):
        """Move selection up - works contextually based on focused panel"""
        focused = self.focused
        if focused == self.session_list:
            # Navigate session list - use current displayed sessions
            if self.session_list and self.session_list.index > 0:
                self.session_list.index -= 1
                session = self.session_list.get_selected_session()
                if session and self.thread_view:
                    self.thread_view.update_session(session, user_only=self.user_only_mode)
        elif focused == self.thread_view:
            # Delegate to thread view's scroll action
            self.thread_view.action_scroll_up()

    def action_move_down(self):
        """Move selection down - works contextually based on focused panel"""
        focused = self.focused
        if focused == self.session_list:
            # Navigate session list - use current displayed sessions
            current_sessions = (
                self.session_list._sessions_to_display
                if self.session_list._sessions_to_display
                else self.session_manager.sessions
            )
            if self.session_list and self.session_list.index < len(current_sessions) - 1:
                self.session_list.index += 1
                session = self.session_list.get_selected_session()
                if session and self.thread_view:
                    self.thread_view.update_session(session, user_only=self.user_only_mode)
        elif focused == self.thread_view:
            # Delegate to thread view's scroll action
            self.thread_view.action_scroll_down()

    def action_page_up(self):
        """Scroll in the active pane"""
        focused = self.focused
        if focused == self.thread_view:
            # If thread view is focused, scroll it
            self.thread_view.action_scroll_page_up()
        elif focused == self.session_list:
            # If session list is focused, scroll it
            # ListView doesn't have page scroll by default, so scroll by multiple items
            if self.session_list and self.session_list.index > 0:
                # Scroll up by a page worth (approximately 10 items or visible height)
                new_index = max(0, self.session_list.index - 10)
                self.session_list.index = new_index
                session = self.session_list.get_selected_session()
                if session and self.thread_view:
                    self.thread_view.update_session(session, user_only=self.user_only_mode)
        # Don't change focus - only work in active pane

    def action_page_down(self):
        """Scroll in the active pane"""
        focused = self.focused
        if focused == self.thread_view:
            # If thread view is focused, scroll it
            self.thread_view.action_scroll_page_down()
        elif focused == self.session_list:
            # If session list is focused, scroll it
            current_sessions = (
                self.session_list._sessions_to_display
                if self.session_list._sessions_to_display
                else self.session_manager.sessions
            )
            if self.session_list and self.session_list.index < len(current_sessions) - 1:
                # Scroll down by a page worth (approximately 10 items or visible height)
                new_index = min(len(current_sessions) - 1, self.session_list.index + 10)
                self.session_list.index = new_index
                session = self.session_list.get_selected_session()
                if session and self.thread_view:
                    self.thread_view.update_session(session, user_only=self.user_only_mode)
        # Don't change focus - only work in active pane

    def action_go_to_top(self):
        """Go to top of active panel (vim: gg)"""
        import time

        current_time = time.time()

        # Check for double g press (within 0.5 seconds)
        if self._last_g_press is not None and (current_time - self._last_g_press) < 0.5:
            # Double g press - go to top
            focused = self.focused
            if focused == self.session_list:
                # Go to first session
                if self.session_list:
                    self.session_list.index = 0
                    session = self.session_list.get_selected_session()
                    if session and self.thread_view:
                        self.thread_view.update_session(session, user_only=self.user_only_mode)
            elif focused == self.thread_view:
                # Scroll to top of thread
                if self.thread_view:
                    # Scroll to the beginning
                    self.thread_view.scroll_to(0, 0, animate=False)

            self._last_g_press = None  # Reset
        else:
            # First g press - wait for potential second g
            self._last_g_press = current_time

    def action_go_to_bottom(self):
        """Go to bottom of active panel (vim: G)"""
        focused = self.focused
        if focused == self.session_list:
            # Go to last session
            current_sessions = (
                self.session_list._sessions_to_display
                if self.session_list._sessions_to_display
                else self.session_manager.sessions
            )
            if self.session_list and current_sessions:
                self.session_list.index = len(current_sessions) - 1
                session = self.session_list.get_selected_session()
                if session and self.thread_view:
                    self.thread_view.update_session(session, user_only=self.user_only_mode)
        elif focused == self.thread_view:
            # Scroll to absolute bottom of thread
            if self.thread_view:
                try:
                    # Get the content widget to find its dimensions
                    content_widget = self.thread_view.query_one("#thread-content", ThreadContent)
                    if content_widget:
                        # Get the content region to find its height
                        content_region = content_widget.region
                        if content_region:
                            # Get the viewport height
                            viewport_height = self.thread_view.size.height
                            # Calculate maximum scroll position
                            # Max scroll = content height - viewport height
                            max_scroll_y = max(0, content_region.height - viewport_height)

                            # Scroll directly to the bottom
                            try:
                                # Try to scroll to the calculated position
                                self.thread_view.scroll_to(0, max_scroll_y, animate=False)
                            except Exception:
                                # Fallback: scroll to a very large y value
                                self.thread_view.scroll_to(0, 999999, animate=False)
                        else:
                            # Content not rendered yet, use fallback
                            self._scroll_to_bottom_fallback()
                    else:
                        # Content widget not found, use fallback
                        self._scroll_to_bottom_fallback()
                except Exception:
                    # Use fallback method
                    self._scroll_to_bottom_fallback()

    def _scroll_to_bottom_fallback(self):
        """Fallback method to scroll to bottom by repeatedly scrolling"""
        if not self.thread_view:
            return
        try:
            # Keep scrolling down until we can't scroll anymore
            last_y = None
            no_change_count = 0
            for _ in range(2000):  # Max iterations
                try:
                    # Get current scroll position
                    try:
                        _ = self.thread_view.scroll_offset.y
                    except Exception as e:
                        _debug_log(f"Failed to get scroll offset: {e}")

                    # Scroll one line down (smallest increment)
                    self.thread_view.scroll_down(animate=False)

                    # Get new scroll position
                    try:
                        new_y = self.thread_view.scroll_offset.y
                    except Exception:
                        new_y = getattr(self.thread_view, "scroll_y", 0)

                    # If scroll position didn't change, count consecutive no-changes
                    if last_y is not None and new_y == last_y:
                        no_change_count += 1
                        # If we haven't changed for 10 iterations, we're at the bottom
                        if no_change_count >= 10:
                            break
                    else:
                        no_change_count = 0

                    last_y = new_y
                except Exception as e:
                    _debug_log(f"Scroll iteration error: {e}")
                    break
        except Exception as e:
            _debug_log(f"Scroll to bottom fallback error: {e}")

    def action_tag_session(self):
        """Tag the current session"""
        if not self.session_list:
            return

        session = self.session_list.get_selected_session()
        if not session:
            return

        # Use input dialog - Textual's Input widget needs to be in a screen
        class TagInputScreen(ModalScreen):
            def compose(self):
                yield EscapableInput(placeholder="Enter tag name (ESC to cancel)", id="tag-input")

            def on_mount(self):
                """Focus the input when mounted"""
                input_widget = self.query_one("#tag-input", EscapableInput)
                input_widget.focus()

            def on_input_submitted(self, event: Input.Submitted):
                value = event.value.strip()
                if value:
                    self.dismiss(value)
                else:
                    self.dismiss(None)

        # Remember current index before tagging
        current_index = self.session_list.index

        def handle_tag(tag_value: str):
            if tag_value and tag_value.strip():
                self.session_manager.tag_session(session.session_id, tag_value.strip())
                # Refresh list but keep selection on same session
                self.session_list._populate(initial_index=current_index)
                if self.thread_view:
                    self.thread_view.update_session(session)
            # If tag_value is None, user pressed ESC - do nothing

        self.push_screen(TagInputScreen(), handle_tag)

    def action_copy_session_command(self):
        """Start claude session in the project directory"""
        if not self.session_list:
            return

        session = self.session_list.get_selected_session()
        if not session:
            return

        # Get project directory - use the session's project path
        project_dir = session.project_path

        # Ensure it's a directory (not a file)
        if os.path.isfile(project_dir):
            project_dir = os.path.dirname(project_dir)

        # Normalize the path and ensure it exists
        project_dir = os.path.abspath(os.path.expanduser(project_dir))

        # Verify the directory exists, fallback to parent if needed
        if not os.path.isdir(project_dir):
            project_dir = os.path.dirname(project_dir)
            if not os.path.isdir(project_dir):
                project_dir = os.path.expanduser("~")

        # Exit the app and return session info for launching claude
        self.exit(result={"project_dir": project_dir, "session_id": session.session_id})

    def action_copy_thread(self):
        """Copy current thread content to clipboard as markdown"""
        if not self.session_list:
            return

        session = self.session_list.get_selected_session()
        if not session:
            return

        # Build markdown content (similar to export)
        messages = session.load_messages()

        content = []
        content.append(f"# Session: {session.session_id}\n\n")
        content.append(f"**Project:** `{session.project_path}`\n\n")
        content.append(f"**Date:** {session.date_str}\n\n")
        if session.tag:
            content.append(f"**Tag:** {session.tag}\n\n")
        content.append("---\n\n")

        if not messages:
            content.append("*No messages found in this session.*\n")
        else:
            i = 0
            while i < len(messages):
                current_role = messages[i].get("role", "unknown")
                combined_texts = [messages[i].get("content", "")]

                j = i + 1
                while j < len(messages) and messages[j].get("role") == current_role:
                    combined_texts.append(messages[j].get("content", ""))
                    j += 1

                combined_text = "\n\n".join(combined_texts)

                if current_role == "user":
                    content.append(f"## User\n\n{combined_text}\n\n")
                elif current_role == "assistant":
                    content.append(f"## Assistant\n\n{combined_text}\n\n")
                elif current_role == "error":
                    content.append(f"## Error\n\n{combined_text}\n\n")
                else:
                    content.append(f"## {current_role.title()}\n\n{combined_text}\n\n")

                i = j

        markdown_content = "".join(content)

        # Copy to clipboard
        try:
            import pyperclip

            pyperclip.copy(markdown_content)
            self.notify(
                "Thread copied to clipboard!", title="Copied", severity="information", timeout=2
            )
        except Exception as e:
            self.notify(f"Failed to copy: {e}", title="Error", severity="error", timeout=3)

    def action_yank(self):
        """Yank (copy) selected text to clipboard (vim-style)

        Works with both:
        - Textual's native selection (click and drag)
        - Terminal selection (Shift+click) - reads from PRIMARY selection on X11
        """
        _debug_log("action_yank called")
        selected_text = self.screen.get_selected_text()
        _debug_log(f"Textual selection: {repr(selected_text)[:100] if selected_text else None}")
        _debug_log(f"Screen selections: {self.screen.selections}")

        if selected_text:
            # Textual selection found - copy to clipboard
            _debug_log("Using Textual selection")
            try:
                self.copy_to_clipboard(selected_text)
                self.notify(
                    f"Yanked {len(selected_text)} chars",
                    title="Yanked",
                    severity="information",
                    timeout=2,
                )
            except Exception as e:
                _debug_log(f"copy_to_clipboard failed: {e}")
                try:
                    import pyperclip

                    pyperclip.copy(selected_text)
                    self.notify(
                        f"Yanked {len(selected_text)} chars",
                        title="Yanked",
                        severity="information",
                        timeout=2,
                    )
                except Exception as e2:
                    _debug_log(f"pyperclip failed: {e2}")
                    self.notify(f"Failed to yank: {e2}", title="Error", severity="error", timeout=3)
        else:
            # Try to get text from X11 PRIMARY selection (what Shift+select copies to)
            _debug_log("Trying X11 PRIMARY selection")
            try:
                result = subprocess.run(
                    ["xclip", "-selection", "primary", "-o"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                stdout_len = len(result.stdout) if result.stdout else 0
                _debug_log(f"xclip returncode: {result.returncode}, stdout len: {stdout_len}")
                if result.returncode == 0 and result.stdout:
                    selected_text = result.stdout
                    # Copy from PRIMARY to CLIPBOARD
                    import pyperclip

                    pyperclip.copy(selected_text)
                    self.notify(
                        f"Yanked {len(selected_text)} chars from selection",
                        title="Yanked",
                        severity="information",
                        timeout=2,
                    )
                else:
                    self.notify("No text selected", title="Yank", severity="warning", timeout=2)
            except Exception as e:
                _debug_log(f"xclip failed: {e}")
                self.notify(
                    "No text selected (install xclip for terminal selection)",
                    title="Yank",
                    severity="warning",
                    timeout=2,
                )

    def action_export_session(self):
        """Export current session thread as markdown file"""
        if not self.session_list:
            return

        session = self.session_list.get_selected_session()
        if not session:
            return

        # Build filename: <session-id>-<tag>.md or <session-id>.md
        if session.tag:
            filename = f"{session.session_id}-{session.tag}.md"
        else:
            filename = f"{session.session_id}.md"

        # Get current working directory
        cwd = os.getcwd()
        filepath = os.path.join(cwd, filename)

        try:
            # Load messages
            messages = session.load_messages()

            # Build markdown content
            content = []
            content.append(f"# Claude Session: {session.session_id}\n\n")
            content.append(f"**Project:** `{session.project_path}`\n\n")
            content.append(f"**Date:** {session.date_str}\n\n")
            if session.tag:
                content.append(f"**Tag:** {session.tag}\n\n")
            content.append("---\n\n")

            # Add messages
            for msg in messages:
                role = msg.get("role", "unknown")
                text = msg.get("content", "")

                if role == "user":
                    content.append(f"## 👤 User\n\n{text}\n\n")
                elif role == "assistant":
                    # For export, keep plain text (no Rich markup)
                    content.append(f"## 🤖 Assistant\n\n{text}\n\n")
                elif role == "error":
                    content.append(f"## ❌ Error\n\n{text}\n\n")

            if not messages:
                content.append("*No messages found in this session.*\n")

            # Write to file
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("".join(content))

            self.notify(
                f"Exported to: {filepath}",
                title="Export Successful",
                severity="information",
                timeout=3,
            )

        except Exception as e:
            self.notify(
                f"Error exporting session: {e}", title="Export Error", severity="error", timeout=5
            )

    def action_delete_session(self):
        """Delete the current session"""
        if not self.session_list:
            return

        session = self.session_list.get_selected_session()
        if not session:
            return

        # Confirm deletion with a modal screen
        from textual.screen import ModalScreen
        from textual.widgets import Button

        class DeleteConfirmScreen(ModalScreen):
            def __init__(self, session_obj: Session):
                super().__init__()
                self.session_obj = session_obj

            def compose(self):
                from textual.containers import Horizontal
                from textual.widgets import Static

                with Vertical():
                    yield Static(f"Delete session?\n\n{self.session_obj.session_id[:8]}")
                    if self.session_obj.tag:
                        yield Static(f"Tag: {self.session_obj.tag}")
                    yield Static(f"Project: {self.session_obj.project_path}")
                    with Horizontal():
                        yield Button("Delete", variant="error", id="delete-btn")
                        yield Button("Cancel", variant="default", id="cancel-btn")

            def on_button_pressed(self, event: Button.Pressed):
                if event.button.id == "delete-btn":
                    self.dismiss(True)
                else:
                    self.dismiss(False)

            def key_escape(self):
                """Handle ESC key directly"""
                self.dismiss(False)

        # Remember current index before deletion
        current_index = self.session_list.index

        def handle_delete(confirmed: bool):
            if confirmed:
                if self.session_manager.delete_session(session.session_id):
                    # Calculate the new index (same position, or last if we deleted the last one)
                    new_index = 0
                    if self.session_manager.sessions:
                        new_index = min(current_index, len(self.session_manager.sessions) - 1)

                    # Refresh session list with the new index
                    self.session_list._populate(initial_index=new_index)

                    # Update thread view
                    if self.session_manager.sessions:
                        new_session = self.session_list.get_selected_session()
                        if new_session and self.thread_view:
                            self.thread_view.update_session(new_session)
                    else:
                        # No sessions left, clear thread view
                        if self.thread_view:
                            from rich.markdown import Markdown

                            empty_content = Markdown("# No Sessions\n\nNo sessions available.")
                            content_widget = self.thread_view.query_one(
                                "#thread-content", ThreadContent
                            )
                            content_widget.update(empty_content)
                    self.notify(
                        f"Session deleted: {session.session_id[:8]}",
                        title="Deleted",
                        severity="information",
                        timeout=3,
                    )
                else:
                    self.notify(
                        f"Failed to delete session: {session.session_id[:8]}",
                        title="Error",
                        severity="error",
                        timeout=3,
                    )

        self.push_screen(DeleteConfirmScreen(session), handle_delete)

    def action_toggle_user_only(self):
        """Toggle showing only user messages in thread view"""
        self.user_only_mode = not self.user_only_mode

        # Update thread view with current filter state
        if self.session_list and self.thread_view:
            session = self.session_list.get_selected_session()
            if session:
                self.thread_view.update_session(session, user_only=self.user_only_mode)

        # Show notification
        mode_text = "User messages only" if self.user_only_mode else "All messages"
        self.notify(
            f"Filter: {mode_text}", title="Filter Toggled", severity="information", timeout=2
        )

    def _search_sessions(self, query: str) -> List[Session]:
        """Search sessions by query string"""
        if not query or not query.strip():
            return self.session_manager.sessions

        query_lower = query.lower().strip()
        matching_sessions = []

        for session in self.session_manager.sessions:
            # Search in session ID
            if query_lower in session.session_id.lower():
                matching_sessions.append(session)
                continue

            # Search in tag
            if session.tag and query_lower in session.tag.lower():
                matching_sessions.append(session)
                continue

            # Search in project path
            if query_lower in session.project_path.lower():
                matching_sessions.append(session)
                continue

            # Search in project name
            if query_lower in session.project_name.lower():
                matching_sessions.append(session)
                continue

            # Search in messages content
            try:
                messages = session.load_messages()
                for msg in messages:
                    content = msg.get("content", "")
                    if query_lower in content.lower():
                        matching_sessions.append(session)
                        break
            except Exception as e:
                _debug_log(f"Failed to search session {session.session_id[:8]}: {e}")

        return matching_sessions

    def _apply_search_filter(self, query: str):
        """Apply search filter to sessions"""
        self.search_query = query
        if query and query.strip():
            self.filtered_sessions = self._search_sessions(query)
        else:
            self.filtered_sessions = []

        # Update session list with filtered results
        if self.session_list:
            if self.filtered_sessions:
                self.session_list._populate(self.filtered_sessions)
            else:
                self.session_list._populate(self.session_manager.sessions)

            # Select first session if available
            if self.session_list.index >= len(self.session_list._sessions_to_display):
                self.session_list.index = 0

            # Update thread view
            session = self.session_list.get_selected_session()
            if session and self.thread_view:
                self.thread_view.update_session(session, user_only=self.user_only_mode)

    def _get_thread_raw_text(self) -> str:
        """Get raw text content of current thread for searching"""
        if not self.session_list:
            return ""

        session = self.session_list.get_selected_session()
        if not session:
            return ""

        messages = session.load_messages()

        # Build plain text content
        content_parts = []
        content_parts.append(f"Session: {session.session_id}\n")
        content_parts.append(f"Project: {session.project_path}\n")
        content_parts.append(f"Date: {session.date_str}\n")
        if session.tag:
            content_parts.append(f"Tag: {session.tag}\n")
        content_parts.append("\n")

        if messages:
            i = 0
            while i < len(messages):
                current_role = messages[i].get("role", "unknown")
                combined_texts = [messages[i].get("content", "")]

                j = i + 1
                while j < len(messages) and messages[j].get("role") == current_role:
                    combined_texts.append(messages[j].get("content", ""))
                    j += 1

                combined_text = "\n\n".join(combined_texts)
                content_parts.append(f"{current_role.title()}:\n{combined_text}\n\n")
                i = j

        return "".join(content_parts)

    def _search_in_thread(self, query: str):
        """Search for text within the thread content"""
        _debug_log(f"_search_in_thread: query='{query}'")

        self._thread_search_term = query
        self._thread_raw_text = self._get_thread_raw_text().lower()

        # Find all match positions
        self._thread_search_matches = []
        query_lower = query.lower()
        start = 0
        while True:
            pos = self._thread_raw_text.find(query_lower, start)
            if pos == -1:
                break
            self._thread_search_matches.append(pos)
            start = pos + 1

        _debug_log(f"Found {len(self._thread_search_matches)} matches")

        # Refresh thread view with highlighting
        if self.thread_view and self.session_list:
            session = self.session_list.get_selected_session()
            if session:
                self.thread_view.update_session(
                    session, user_only=self.user_only_mode, highlight_term=query
                )

        if self._thread_search_matches:
            self._thread_search_index = 0
            self._jump_to_thread_match(0)
            self.notify(
                f"Match 1/{len(self._thread_search_matches)} for '{query}'",
                title="Search",
                severity="information",
                timeout=2,
            )
        else:
            self._thread_search_index = -1
            self.notify(f"No matches for '{query}'", title="Search", severity="warning", timeout=2)

    def _jump_to_thread_match(self, match_index: int):
        """Jump to a specific match in the thread view"""
        if not self._thread_search_matches or match_index < 0:
            return

        if match_index >= len(self._thread_search_matches):
            match_index = 0

        _debug_log(f"_jump_to_thread_match: index={match_index}")

        # Calculate approximate line number based on character position
        char_pos = self._thread_search_matches[match_index]
        text_before = self._thread_raw_text[:char_pos]
        line_number = text_before.count("\n")

        _debug_log(f"Match at char {char_pos}, approx line {line_number}")

        # Scroll thread view to that position
        if self.thread_view:
            # Estimate scroll position (rough approximation)
            # Each line is roughly 1 unit of scroll
            scroll_y = max(0, line_number - 5)  # Show a few lines above
            self.thread_view.scroll_to(0, scroll_y, animate=False)
            _debug_log(f"Scrolled to y={scroll_y}")

    def _clear_thread_search(self):
        """Clear thread search state"""
        self._thread_search_term = ""
        self._thread_search_matches = []
        self._thread_search_index = -1
        self._thread_raw_text = ""

        # Refresh thread view without highlighting
        if self.thread_view and self.session_list:
            session = self.session_list.get_selected_session()
            if session:
                self.thread_view.update_session(session, user_only=self.user_only_mode)

    def action_search_next(self):
        """Jump to next search match in thread"""
        if not self._thread_search_matches:
            self.notify("No search active", title="Search", severity="warning", timeout=2)
            return

        self._thread_search_index += 1
        if self._thread_search_index >= len(self._thread_search_matches):
            self._thread_search_index = 0
            self.notify(
                "Search wrapped to beginning", title="Search", severity="information", timeout=2
            )

        self._jump_to_thread_match(self._thread_search_index)
        self.notify(
            f"Match {self._thread_search_index + 1}/{len(self._thread_search_matches)}",
            title="Search",
            severity="information",
            timeout=2,
        )

    def action_search_prev(self):
        """Jump to previous search match in thread"""
        if not self._thread_search_matches:
            self.notify("No search active", title="Search", severity="warning", timeout=2)
            return

        self._thread_search_index -= 1
        if self._thread_search_index < 0:
            self._thread_search_index = len(self._thread_search_matches) - 1
            self.notify("Search wrapped to end", title="Search", severity="information", timeout=2)

        self._jump_to_thread_match(self._thread_search_index)
        self.notify(
            f"Match {self._thread_search_index + 1}/{len(self._thread_search_matches)}",
            title="Search",
            severity="information",
            timeout=2,
        )

    def _goto_session(self, number: int):
        """Go to session by line number"""
        # Use filtered sessions if search is active, otherwise all sessions
        sessions = (
            self.filtered_sessions if self.filtered_sessions else self.session_manager.sessions
        )

        if 1 <= number <= len(sessions):
            # Find the session in the current list
            target_session = sessions[number - 1]

            # Set index to the target
            target_index = number - 1

            # Make sure the session list shows the right sessions
            # Pass initial_index to set it during population
            if self.filtered_sessions:
                self.session_list._populate(
                    self.filtered_sessions, preserve_index=False, initial_index=target_index
                )
            else:
                self.session_list._populate(
                    self.session_manager.sessions, preserve_index=False, initial_index=target_index
                )

            # Update thread view first
            if self.thread_view:
                self.thread_view.update_session(target_session, user_only=self.user_only_mode)

            # Focus the session list immediately so highlight will be visible
            self.set_focus(self.session_list)

            # Use multiple callbacks to ensure selection is properly applied
            # after list is fully rendered
            def ensure_selection():
                if target_index < len(self.session_list._sessions_to_display):
                    # Ensure list has focus (critical for highlight to show)
                    self.set_focus(self.session_list)

                    # Re-set index to trigger highlight
                    # (this will call watch_index which sets highlighted=True)
                    self.session_list.index = target_index

                    # Manually ensure the highlight is set (in case watch_index didn't fire)
                    try:
                        if hasattr(self.session_list, "_nodes") and target_index < len(
                            self.session_list._nodes
                        ):
                            highlighted_item = self.session_list._nodes[target_index]
                            if isinstance(highlighted_item, ListItem):
                                highlighted_item.highlighted = True
                    except (IndexError, AttributeError, TypeError):
                        pass

                    # Try to scroll the selected item into view
                    try:
                        highlighted_child = self.session_list.highlighted_child
                        if highlighted_child:
                            self.session_list.scroll_to_widget(highlighted_child, animate=False)
                    except (IndexError, AttributeError, TypeError):
                        pass

                    # Force refresh to show selection/highlight
                    self.session_list.refresh(layout=True)

            # Use call_after_refresh to ensure list is fully rendered
            self.call_after_refresh(ensure_selection)

            # Also use a timer as backup (small delay to ensure rendering is complete)
            self.set_timer(0.2, ensure_selection)

            self.notify(
                f"Jumped to session {number}", title="Goto", severity="information", timeout=2
            )
        else:
            self.notify(
                f"Invalid session number: {number} (range: 1-{len(sessions)})",
                title="Error",
                severity="error",
                timeout=3,
            )

    def action_search_mode(self):
        """Enter search mode"""
        if not self.session_list:
            return

        # Use modal screen like tag input
        class SearchInputScreen(ModalScreen):
            def compose(self):
                yield EscapableInput(
                    placeholder="Search sessions... (ESC to cancel)", id="search-input"
                )

            def on_mount(self):
                """Focus the input when mounted"""
                input_widget = self.query_one("#search-input", EscapableInput)
                input_widget.focus()

            def on_input_submitted(self, event: Input.Submitted):
                value = event.value.strip()
                self.dismiss(value)

        def handle_search(value):
            if value is None:
                # User pressed ESC - do nothing
                return

            # Check which pane is focused to determine search behavior
            focused = self.focused
            is_thread_focused = focused == self.thread_view or (
                focused and hasattr(focused, "parent") and focused.parent == self.thread_view
            )

            if is_thread_focused and value:
                # Search within thread content
                self._search_in_thread(value)
            elif value:
                # Filter sessions list
                self._apply_search_filter(value)
                result_count = (
                    len(self.filtered_sessions)
                    if self.filtered_sessions
                    else len(self.session_manager.sessions)
                )
                self.notify(
                    f"Search: {value} ({result_count} results)",
                    title="Search",
                    severity="information",
                    timeout=2,
                )
            else:
                # Empty search clears filter
                self._apply_search_filter("")
                self._clear_thread_search()
                self.notify("Search cleared", title="Search", severity="information", timeout=2)

        self.push_screen(SearchInputScreen(), handle_search)

    def action_command_mode(self):
        """Enter command mode"""
        if not self.session_list:
            return

        # Use modal screen like tag input
        class CommandInputScreen(ModalScreen):
            def compose(self):
                yield EscapableInput(
                    placeholder="Enter command (number to goto session)... (ESC to cancel)",
                    id="command-input",
                )

            def on_mount(self):
                """Focus the input when mounted"""
                input_widget = self.query_one("#command-input", EscapableInput)
                input_widget.focus()

            def on_input_submitted(self, event: Input.Submitted):
                value = event.value.strip()
                self.dismiss(value)

        def handle_command(value):
            if value is None:
                # User pressed ESC - do nothing
                return
            if value:
                try:
                    number = int(value.strip())
                    self._goto_session(number)
                except ValueError:
                    self.notify(
                        f"Unknown command: {value}", title="Error", severity="error", timeout=3
                    )

        self.push_screen(CommandInputScreen(), handle_command)

    def action_show_help(self):
        """Toggle keyboard shortcuts help screen"""
        if any(isinstance(s, HelpScreen) for s in self.screen_stack):
            self.pop_screen()
            return
        self.push_screen(HelpScreen())

    def action_escape(self):
        """Handle ESC key - dismiss modal if one is active"""
        # Check if we have a modal screen on top
        if len(self.screen_stack) > 1:
            self.pop_screen()

    def action_new_session(self):
        """Create a new tagged session in the current directory"""

        class NewSessionInputScreen(ModalScreen):
            def compose(self):
                yield EscapableInput(
                    placeholder="Enter session name (ESC to cancel)", id="new-session-input"
                )

            def on_mount(self):
                """Focus the input when mounted"""
                input_widget = self.query_one("#new-session-input", EscapableInput)
                input_widget.focus()

            def on_input_submitted(self, event: Input.Submitted):
                value = event.value.strip()
                if value:
                    self.dismiss(value)
                else:
                    self.dismiss(None)

        def handle_new_session(tag_value: str):
            if tag_value and tag_value.strip():
                self.exit(result={"action": "new_session", "tag": tag_value.strip()})

        self.push_screen(NewSessionInputScreen(), handle_new_session)


def create_tagged_session(tag: str, temp: bool = False):
    """Create a new Claude session with a tag and launch it."""
    # Check if tag already exists
    tags_file = Path.home() / ".claude" / "claude-yelp-tags.json"
    old_session_id = None
    if tags_file.exists():
        try:
            tags = json.loads(tags_file.read_text())
            for session_id, existing_tag in tags.items():
                if existing_tag == tag:
                    print(f"Tag '{tag}' already exists (session {session_id[:8]})")
                    reply = input("[Y]connect / [n]abort / [o]verwrite: ").strip().lower()
                    if reply == "n":
                        print("Aborted.")
                        sys.exit(0)
                    elif reply == "o":
                        print("Removing old session and creating new...")
                        old_session_id = session_id
                        break
                    else:
                        print("Connecting to existing session...")
                        env = os.environ.copy()
                        env["CLAUDE_SESSION_ID"] = session_id
                        os.execvpe("claude", ["claude", "--resume", session_id], env)
        except Exception as e:
            _debug_log(f"Failed to check existing tags: {e}")

    init_prompt = f"Session: {tag}"
    result = subprocess.run(
        ["claude", "-p", init_prompt, "--output-format", "json"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Error creating session: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Parse JSON - clean control characters
    output = result.stdout.strip()
    output = "".join(c for c in output if ord(c) >= 32 or c in "\n\r\t")

    try:
        data = json.loads(output)
        session_id = data.get("session_id")
        if not session_id:
            print("Error: No session_id in response", file=sys.stderr)
            print(f"Response: {output[:500]}", file=sys.stderr)
            sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}", file=sys.stderr)
        print(f"Output: {output[:500]}", file=sys.stderr)
        sys.exit(1)

    if temp:
        print(f"Created TEMP session {session_id[:8]} with tag: {tag}")
    else:
        # Save tag
        tags_file = Path.home() / ".claude" / "claude-yelp-tags.json"
        tags = {}
        if tags_file.exists():
            try:
                tags = json.loads(tags_file.read_text())
            except Exception as e:
                _debug_log(f"Failed to load tags file: {e}")
        # Remove old session if overwriting
        if old_session_id:
            tags.pop(old_session_id, None)
            # Delete old session file
            projects_dir = Path.home() / ".claude" / "projects"
            for old_file in projects_dir.rglob(f"{old_session_id}.jsonl"):
                old_file.unlink()
                print(f"Removed old session {old_session_id[:8]}")
                break
        tags[session_id] = tag
        tags_file.write_text(json.dumps(tags, indent=2))
        print(f"Created session {session_id[:8]} with tag: {tag}")

    # Launch claude with session_id in environment
    env = os.environ.copy()
    env["CLAUDE_SESSION_ID"] = session_id

    if temp:
        # Run claude (not exec) so we can cleanup after
        subprocess.run(["claude", "--resume", session_id], env=env)
        # Cleanup: find and delete session file
        projects_dir = Path.home() / ".claude" / "projects"
        for session_file in projects_dir.rglob(f"{session_id}.jsonl"):
            session_file.unlink()
            # Also remove session directory if exists
            session_dir = session_file.parent / session_id
            if session_dir.is_dir():
                try:
                    session_dir.rmdir()
                except OSError:
                    pass
            print(f"Cleaned up temp session {session_id[:8]}")
            break
        sys.exit(0)
    else:
        os.execvpe("claude", ["claude", "--resume", session_id], env)


def main():
    """Main entry point"""
    global DEBUG_ENABLED
    import argparse
    from datetime import datetime

    parser = argparse.ArgumentParser(
        description="Claude Yelp - Session manager for Claude CLI",
        epilog="Examples: clod, clod +10, clod 'my-tag', clod -t 'temp-tag'",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging to /tmp/claude-yelp-debug.log"
    )
    parser.add_argument(
        "-t", "--temp", action="store_true", help="Temporary session (deleted on exit)"
    )
    parser.add_argument(
        "arg", nargs="?", type=str, help="Session number (+10 or 10) or tag name for new session"
    )

    args = parser.parse_args()

    # Enable debug logging if --debug flag is passed or env var is set
    if args.debug or os.environ.get("CLAUDE_YELP_DEBUG", "").lower() in ("1", "true", "yes"):
        DEBUG_ENABLED = True
        with open(DEBUG_LOG_FILE, "w") as f:
            source = "--debug flag" if args.debug else "CLAUDE_YELP_DEBUG env"
            f.write(f"=== claude-yelp started at {datetime.now().isoformat()} ({source}) ===\n")
        _debug_log("Debug logging enabled")

    initial_session_number = None
    if args.arg:
        # Handle both "+10" and "10" formats for session number
        session_str = args.arg.lstrip("+")
        if session_str.isdigit():
            initial_session_number = int(session_str)
        else:
            # Not a number - treat as tag for new session
            create_tagged_session(args.arg, temp=args.temp)
    elif args.temp:
        print("Error: -t requires a tag name", file=sys.stderr)
        sys.exit(1)

    _debug_log("About to create SessionManager")
    try:
        session_manager = SessionManager()
    except Exception as e:
        _debug_log(f"Error creating SessionManager: {e}")
        raise
    _debug_log("SessionManager created")
    app = ClaudeYelpApp(session_manager, initial_session_number=initial_session_number)
    _debug_log("ClaudeYelpApp created, about to run()")
    result = app.run()
    _debug_log(f"App finished, result={result}")

    if result and isinstance(result, dict):
        if result.get("action") == "new_session":
            # Create a new tagged session
            create_tagged_session(result["tag"])
        elif "session_id" in result:
            # Resume an existing session
            project_dir = result["project_dir"]
            session_id = result["session_id"]
            os.chdir(project_dir)
            os.execvp("claude", ["claude", "--resume", session_id])


if __name__ == "__main__":
    main()
