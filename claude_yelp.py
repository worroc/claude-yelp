#!/usr/bin/env python3
"""
Claude Yelp - A terminal-based session manager for Claude Code CLI
"""

import functools
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from textual.app import App, ComposeResult
from rich.console import Console as RichConsole
from rich.text import Text as RichText
from textual import keys as textual_keys
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.keys import format_key
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, ListItem, ListView, Static

# Raw content-block types that carry tool calls and their output
TOOL_USE_TYPES = frozenset({"tool_use", "server_tool_use"})
TOOL_RESULT_TYPES = frozenset({"tool_result", "server_tool_result", "advisor_tool_result"})
# The left column that carries the cursor mark
GUTTER_BLANK = "  "
CURSOR_STYLE = "on yellow"
# What a line that can be opened looks like in the gutter
FOLD_CLOSED = "▸ "
FOLD_OPEN = "▾ "
FOLD_STYLE = "bold yellow"
# Lines of context kept around the cursor when it moves
CURSOR_MARGIN = 3
# Heading rules are a fixed length, so resizing never shifts the text
HEADING_WIDTH = 34
# One line standing for a run of thinking and tool steps
CHAIN_ICON = "⚙"
CHAIN_STYLE = "dim cyan"
CHAIN_NAMES_SHOWN = 3
# A short note the agent wrote between steps
NOTE_STYLE = "dim italic"
# An agent turn that produced no text at all
WORKED_SILENTLY = "(worked without answering)"

# One heading per speaker; everything the agent does sits under the agent
ROLE_HEADINGS = {
    "user": "👤 User",
    "assistant": "🤖 Assistant",
    "thinking": "🤖 Assistant",
    "tool_use": "🤖 Assistant",
    "tool_result": "🤖 Assistant",
    "error": "❌ Error",
}
ROLE_STYLES = {
    "👤 User": "bold green",
    "🤖 Assistant": "bold cyan",
    "❌ Error": "bold red",
}
# Blocks shown as one line until they are opened
FOLDABLE_ROLES = frozenset({"thinking", "tool_use", "tool_result"})
FOLDABLE_ICONS = {"thinking": "💭", "tool_use": "⚙", "tool_result": "↩"}
FOLDABLE_STYLES = {"thinking": "italic magenta", "tool_use": "blue", "tool_result": "dim"}

# What the fold key acts on. Regions nest, so the cursor is either on a chain's
# own line or on one step inside it, and the innermost one wins.
FOLD_ROLES = frozenset({"turn", "chain", "tool_use", "tool_result", "thinking"})
# The match the user is standing on, against the other matches
CURRENT_MATCH_STYLE = "bold black on bright_yellow"
# How much of a collapsed tool block is shown on its one line
TOOL_PREVIEW_CHARS = 80

CONFIG_PATH = Path(
    os.environ.get("CLAUDE_YELP_CONFIG", Path.home() / ".config" / "claude-yelp" / "config")
)

# The one place shortcuts are defined: section, action, default keys, description.
# The key bindings, the help screen and the config file all come from this table.
KEYMAP = (
    (
        "Navigation",
        (
            ("move_up", ("up",), "Move up / scroll thread up"),
            ("move_down", ("down",), "Move down / scroll thread down"),
            ("page_up", ("pageup",), "Page up"),
            ("page_down", ("pagedown",), "Page down"),
            ("go_to_top", ("g",), "Go to top (press twice)"),
            ("go_to_bottom", ("G",), "Go to bottom"),
            ("focus_left", ("left",), "Focus session list"),
            ("focus_right", ("right",), "Focus thread"),
            ("resize_left", ("shift+left",), "Make session list narrower"),
            ("resize_right", ("shift+right",), "Make session list wider"),
        ),
    ),
    (
        "Sessions",
        (
            ("copy_session_command", ("r",), "Resume selected session"),
            ("tag_session", ("t", "f2"), "Tag session"),
            ("delete_session", ("d",), "Delete session"),
            ("export_session", ("e",), "Export session to markdown"),
            ("new_session", ("ctrl+n",), "Create new tagged session"),
            ("toggle_cwd_filter", (".",), "Toggle CWD filter (on by default)"),
            ("toggle_project_filter", (",",), "Toggle selected project filter"),
        ),
    ),
    (
        "Search",
        (
            ("search_mode", ("/",), "Search sessions (left) or thread (right)"),
            ("search_next", ("n",), "Next match"),
            ("search_prev", ("N", "p"), "Previous match"),
            ("command_mode", (":",), "Command mode (number jumps to session)"),
        ),
    ),
    (
        "Thread",
        (
            ("toggle_user_only", ("u",), "Show only user messages"),
            ("toggle_tool_output", ("o",), "Open/close the chain or step at the cursor"),
            ("copy_thread", ("c",), "Copy thread as markdown"),
            ("yank", ("y",), "Yank selected text"),
        ),
    ),
    (
        "General",
        (
            ("show_help", ("ctrl+k",), "Toggle this help"),
            ("escape", ("escape",), "Cancel / close dialog"),
            ("quit", ("q",), "Quit"),
        ),
    ),
)

# Actions kept out of the footer, so it stays readable
FOOTER_ACTIONS = frozenset({"new_session", "copy_session_command", "show_help", "escape", "quit"})

CONFIG_TEMPLATE = """\
# claude-yelp configuration
#
# One shortcut per line:
#
#     keybind = <key>=<action>
#
# The key replaces nothing else: the default key for that action still works
# until you unbind it.
#
#     keybind = ctrl+f=search_mode      # ctrl+f also opens search
#     keybind = /=unbind                # '/' stops opening search
#
# Keys are written the way you press them: a, G, ctrl+n, shift+left, f2,
# pageup, escape, '.', ',', '/'.
#
# Actions:
{actions}
"""


def _config_template() -> str:
    """The commented example config, listing every action and its default keys"""
    lines = []
    for section, entries in KEYMAP:
        lines.append(f"#   {section}")
        for action, keys, description in entries:
            shown = " ".join(keys)
            lines.append(f"#     {action:<22} {shown:<14} {description}")
    return CONFIG_TEMPLATE.format(actions="\n".join(lines))


def _normalize_key(key: str) -> str:
    """Turn a key as the user writes it into the name Textual expects"""
    key = key.strip()
    if len(key) == 1:
        try:
            return textual_keys._character_to_key(key)
        except Exception:
            return key
    return key


def read_key_config(path: Path = CONFIG_PATH):
    """Read the config file.

    Returns (bindings, problems): bindings maps a normalized key to an action
    name, or to None when the line unbinds it. Problems are human-readable
    strings; the app shows them instead of failing, so one bad line never
    stops the tool from starting.
    """
    bindings = {}
    problems = []

    if not path.exists():
        return bindings, problems

    known_actions = {action for _, entries in KEYMAP for action, _, _ in entries}

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return bindings, [f"cannot read {path}: {e}"]

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        name, _, value = line.partition("=")
        if name.strip() != "keybind" or not value.strip():
            problems.append(f"line {number}: expected 'keybind = <key>=<action>'")
            continue

        key, _, action = value.partition("=")
        key, action = _normalize_key(key), action.strip()
        if not key or not action:
            problems.append(f"line {number}: expected 'keybind = <key>=<action>'")
            continue

        if action == "unbind":
            bindings[key] = None
        elif action in known_actions:
            bindings[key] = action
        else:
            problems.append(f"line {number}: unknown action '{action}'")

    return bindings, problems


def build_bindings(config=None) -> List[Binding]:
    """Default shortcuts, with the config file layered on top"""
    config = config or {}
    keys_for = {}

    for _, entries in KEYMAP:
        for action, keys, description in entries:
            keys_for[action] = [_normalize_key(k) for k in keys]

    # A key named in the config belongs to that action only
    for key, action in config.items():
        for keys in keys_for.values():
            if key in keys:
                keys.remove(key)
        if action is not None:
            keys_for[action].append(key)

    bindings = []
    for _, entries in KEYMAP:
        for action, _, description in entries:
            for key in keys_for[action]:
                bindings.append(
                    Binding(
                        key,
                        action,
                        description,
                        show=action in FOOTER_ACTIONS,
                        priority=True,
                    )
                )
    return bindings


QUIT_COMMANDS = frozenset({"q", "q!", "quit", "exit"})
# What TAB offers after ':'
COMMANDS = ("export", "export full", "quit")


def complete_command(typed: str):
    """Finish a command name from what is typed. Returns (text, hint)."""
    prefix = typed.lstrip()
    if not prefix:
        return typed, "  ".join(COMMANDS)

    matches = [c for c in COMMANDS if c.startswith(prefix)]
    if not matches:
        return typed, None
    if len(matches) == 1:
        return matches[0], None

    # Fill in as far as every match agrees
    shared = matches[0]
    for candidate in matches[1:]:
        while not candidate.startswith(shared):
            shared = shared[:-1]
    return shared, "  ".join(matches)


def keys_for_action(bindings: List[Binding], *actions: str) -> frozenset:
    """Every key currently bound to any of these actions"""
    return frozenset(b.key for b in bindings if b.action in actions)


def build_help_text(bindings: List[Binding]) -> str:
    """Help screen text, built from the shortcuts that are actually active"""
    keys_for = {}
    for binding in bindings:
        keys_for.setdefault(binding.action, []).append(format_key(binding.key))

    lines = []
    for section, entries in KEYMAP:
        lines.append(f"[b]{section}[/b]")
        for action, _, description in entries:
            keys = " / ".join(keys_for.get(action, [])) or "(unbound)"
            lines.append(f"  {keys:<20} {description}")
        lines.append("")

    lines.append("[b]Notes[/b]")
    lines.append("  The thread shows what the agent answered; open a turn to see how")
    lines.append("  it worked: its notes, and one line per chain of tool steps.")
    lines.append("  A ▸ in the left column marks a line that opens. 'o' opens the one")
    lines.append("  the cursor is on: an agent turn, then a chain, then one step.")
    lines.append("  A lit column shows where the cursor is.")
    lines.append("  Command mode takes a number (session, or thread line when the")
    lines.append("  thread has focus) or: export, export full, quit. TAB completes.")
    lines.append(f"  Shortcuts can be changed in {CONFIG_PATH}")
    lines.append("  Run 'clod --write-config' to create it with every action listed.")
    return "\n".join(lines)


# Textual reads BINDINGS when the class is created, so the config is loaded here
KEY_CONFIG, KEY_CONFIG_PROBLEMS = read_key_config()
APP_BINDINGS = build_bindings(KEY_CONFIG)
APP_HELP_TEXT = build_help_text(APP_BINDINGS)
# The help screen closes with whatever keys open it or cancel elsewhere
HELP_CLOSE_KEYS = keys_for_action(APP_BINDINGS, "show_help", "escape", "quit")

DEBUG_LOG_FILE = os.path.join(tempfile.gettempdir(), "claude-yelp-debug.log")
DEBUG_ENABLED = False

# Filesystems that can block forever when the server is gone.
NETWORK_FS_TYPES = frozenset(
    {
        "9p",
        "afs",
        "cifs",
        "fuse.davfs",
        "fuse.sshfs",
        "ncpfs",
        "nfs",
        "nfs4",
        "smb3",
        "smbfs",
    }
)
# How long a mount gets to answer before we treat it as dead.
MOUNT_PROBE_TIMEOUT = 1.0


def _debug_log(msg: str):
    """Write debug message to file (only if DEBUG_ENABLED)"""
    if not DEBUG_ENABLED:
        return
    with open(DEBUG_LOG_FILE, "a") as f:
        f.write(f"{msg}\n")
        f.flush()


def _network_mount_points() -> set:
    """Mount points served over the network, read from /proc/mounts."""
    points = set()
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return points

    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and fields[2] in NETWORK_FS_TYPES:
            points.add(fields[1].replace("\\040", " "))
    return points


def _probe_mount(path: str, answered: threading.Event):
    """stat() a mount point and signal completion. Never raises."""
    try:
        os.stat(path)
    except OSError:
        pass
    answered.set()


@functools.lru_cache(maxsize=1)
def _dead_mount_points() -> frozenset:
    """Network mounts whose server did not answer a stat() in time.

    A stat() on such a mount blocks uninterruptibly, so every mount is probed
    once, in parallel, from throwaway threads. Threads that never come back are
    daemons and do not hold up process exit.
    """
    answers = {}
    for point in _network_mount_points():
        answered = threading.Event()
        answers[point] = answered
        threading.Thread(target=_probe_mount, args=(point, answered), daemon=True).start()

    deadline = time.monotonic() + MOUNT_PROBE_TIMEOUT
    for answered in answers.values():
        answered.wait(max(0.0, deadline - time.monotonic()))
    return frozenset(p for p, a in answers.items() if not a.is_set())


def _is_unreachable(path: str) -> bool:
    """True if path sits on a network mount that is not answering."""
    return any(
        path == point or path.startswith(point.rstrip("/") + "/") for point in _dead_mount_points()
    )


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


def _block_search_text(item) -> str:
    """Flatten one content block into searchable text."""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return ""

    block_type = item.get("type")
    if block_type == "text":
        return item.get("text", "")
    if block_type in TOOL_USE_TYPES:
        name = item.get("name", "")
        try:
            args = json.dumps(item.get("input", {}), ensure_ascii=False)
        except (TypeError, ValueError):
            args = str(item.get("input", ""))
        return f"{name} {args}"
    if block_type in TOOL_RESULT_TYPES:
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(_block_search_text(sub) for sub in content)
        return ""
    return ""


def _content_block(item, role: str, timestamp) -> Optional[Dict]:
    """Turn one raw content block into a thread block, or None to skip it."""
    if not isinstance(item, dict):
        return None

    block_type = item.get("type")
    if block_type == "text":
        return {"role": role, "content": item.get("text", ""), "timestamp": timestamp}
    if block_type == "thinking":
        # Claude Code writes the signature but not the text, so most thinking
        # blocks are empty. An empty one has nothing to show or unfold.
        thinking = item.get("thinking", "")
        if not thinking.strip():
            return None
        return {
            "role": "thinking",
            "name": "thinking",
            "content": thinking,
            "timestamp": timestamp,
        }
    if block_type in TOOL_USE_TYPES:
        try:
            args = json.dumps(item.get("input", {}), indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            args = str(item.get("input", ""))
        return {
            "role": "tool_use",
            "name": item.get("name", "tool"),
            "content": args,
            "timestamp": timestamp,
        }
    if block_type in TOOL_RESULT_TYPES:
        return {
            "role": "tool_result",
            "name": "result",
            "content": _block_search_text(item),
            "timestamp": timestamp,
        }
    return None


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
        self._blocks: Optional[List[Dict]] = None

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

    def matches(self, query_lower: str) -> bool:
        """Is the query somewhere in this session, tool calls and tool results included?

        The thread view shows only plain user/assistant text, but search must also
        reach tool inputs and tool outputs. Read as a stream: keeping the full text
        of every session in memory would cost hundreds of MB.
        """
        # A plain query survives JSON encoding unchanged, so the raw line can be
        # used as a cheap pre-filter. Anything JSON would escape skips that.
        plain_query = (
            query_lower.isascii()
            and query_lower.isprintable()
            and not set(query_lower) & set('"\\')
        )

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if plain_query and query_lower not in line.lower():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") not in ("user", "assistant"):
                        continue
                    content = entry.get("message", {}).get("content")
                    if isinstance(content, str):
                        if query_lower in content.lower():
                            return True
                    elif isinstance(content, list):
                        for item in content:
                            if query_lower in _block_search_text(item).lower():
                                return True
        except Exception as e:
            _debug_log(f"Failed to search session {self.session_id[:8]}: {e}")
        return False

    def load_blocks(self) -> List[Dict]:
        """Messages plus tool calls and tool results, in file order.

        Roles: "user", "assistant", "tool_use", "tool_result". Kept apart from
        load_messages() so export and copy stay plain conversation text.
        """
        if self._blocks is not None:
            return self._blocks

        blocks = []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    role = entry.get("type")
                    if role not in ("user", "assistant"):
                        continue
                    timestamp = entry.get("timestamp")
                    content = entry.get("message", {}).get("content")
                    if isinstance(content, str):
                        blocks.append({"role": role, "content": content, "timestamp": timestamp})
                    elif isinstance(content, list):
                        for item in content:
                            block = _content_block(item, role, timestamp)
                            if block:
                                blocks.append(block)
        except Exception as e:
            blocks.append({"role": "error", "content": f"Error loading messages: {e}"})

        self._blocks = blocks
        return blocks

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
        self.sessions: List[Session] = []
        _debug_log("Calling _discover_sessions")
        self._discover_sessions()
        self._migrate_tags_file()
        _debug_log(f"SessionManager.__init__ done, found {len(self.sessions)} sessions")

    def _migrate_tags_file(self):
        """One-time migration: move tags from claude-yelp-tags.json into JSONL files"""
        tags_file = self.claude_dir / "claude-yelp-tags.json"
        if not tags_file.exists():
            return
        try:
            tags = json.loads(tags_file.read_text())
        except Exception:
            return

        sessions_by_id = {s.session_id: s for s in self.sessions}
        for session_id, tag in tags.items():
            session = sessions_by_id.get(session_id)
            if not session:
                continue
            if session.tag:
                continue
            entry = {"type": "custom-title", "customTitle": tag, "sessionId": session_id}
            try:
                with open(session.file_path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
                session.tag = tag
            except Exception as e:
                _debug_log(f"Failed to migrate tag for {session_id[:8]}: {e}")

        tags_file.unlink()
        _debug_log(f"Migrated {len(tags)} tags from claude-yelp-tags.json")

    def _decode_project_path(self, encoded_name: str) -> str:
        """Decode Claude's encoded project path back to actual filesystem path.

        Claude encodes paths like /home/ilya.levin/dev/project as:
        -home-ilya-levin-dev-project (dots and slashes become dashes)

        We need to decode this back, handling mixed dots and dashes in names
        like healthshield-146457-8.6.10.x-ocie-fix-db-id.
        """
        if encoded_name.startswith("-"):
            encoded_name = encoded_name[1:]

        parts = encoded_name.split("-")
        current_path = "/"
        i = 0

        while i < len(parts):
            # Fast path: try the single part first (no listdir needed)
            test_path = os.path.join(current_path, parts[i])

            # Never touch a network mount whose server is gone: stat() there
            # hangs for minutes. Decode the rest without asking the disk.
            if _is_unreachable(test_path):
                return os.path.join(current_path, *parts[i:])

            if os.path.exists(test_path):
                current_path = test_path
                i += 1
                continue

            # List directory entries and find the one whose encoded form
            # matches the longest prefix of remaining parts (greedy)
            try:
                entries = os.listdir(current_path)
            except OSError:
                current_path = os.path.join(current_path, parts[i])
                i += 1
                continue

            best_entry = None
            best_len = 0
            remaining = parts[i:]

            for entry in entries:
                # Encode entry the same way Claude does: replace dots with
                # dashes, then split by dash
                encoded = entry.replace(".", "-").split("-")
                n = len(encoded)
                if n > len(remaining) or n <= best_len:
                    continue
                if encoded == remaining[:n]:
                    best_entry = entry
                    best_len = n

            if best_entry:
                current_path = os.path.join(current_path, best_entry)
                i += best_len
            else:
                current_path = os.path.join(current_path, parts[i])
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

                    # Get first message, timestamp, and custom-title
                    first_message = None
                    timestamp = None
                    custom_title = None

                    try:
                        with open(session_file, "r", encoding="utf-8") as f:
                            for line in f:
                                stripped = line.strip()
                                if not stripped:
                                    continue
                                # Fast string check before JSON parse
                                if '"custom-title"' in line:
                                    try:
                                        entry = json.loads(stripped)
                                        if entry.get("type") == "custom-title":
                                            custom_title = entry.get("customTitle")
                                    except json.JSONDecodeError:
                                        pass
                                    continue
                                if first_message:
                                    continue
                                try:
                                    entry = json.loads(stripped)
                                    if entry.get("type") == "user" and "message" in entry:
                                        msg = entry["message"]
                                        if isinstance(msg.get("content"), str):
                                            first_message = msg["content"][:100]
                                            timestamp = entry.get("timestamp")
                                        elif isinstance(msg.get("content"), list):
                                            for item in msg["content"]:
                                                if item.get("type") == "text":
                                                    first_message = item.get("text", "")[:100]
                                                    timestamp = entry.get("timestamp")
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

                    # Apply custom-title from JSONL
                    if custom_title:
                        session.tag = custom_title

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
        """Tag a session by appending a custom-title entry to the JSONL file"""
        for session in self.sessions:
            if session.session_id == session_id:
                entry = {"type": "custom-title", "customTitle": tag, "sessionId": session_id}
                with open(session.file_path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
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
        # None = never populated yet; a list (even empty) = what is on screen
        self._sessions_to_display: Optional[List[Session]] = None

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
        return self._displayed_sessions()

    def _displayed_sessions(self) -> List[Session]:
        """Sessions on screen; falls back to all sessions only before first populate"""
        if self._sessions_to_display is None:
            return self.session_manager.sessions
        return self._sessions_to_display

    def get_selected_session(self) -> Optional[Session]:
        """Get the currently selected session"""
        idx = self.index if hasattr(self, "index") and self.index is not None else 0
        sessions = self._displayed_sessions()
        if 0 <= idx < len(sessions):
            return sessions[idx]
        return None


class ThreadContent(Static):
    """Content widget for thread view - allows text selection"""

    ALLOW_SELECT = True
    can_focus = True


def _runs(blocks: List[Dict]):
    """Split the thread into turns: which blocks share one heading"""
    runs = []
    first = 0
    while first < len(blocks):
        heading = ROLE_HEADINGS.get(blocks[first].get("role"))
        last = first
        while last + 1 < len(blocks) and ROLE_HEADINGS.get(blocks[last + 1].get("role")) == heading:
            last += 1
        runs.append((first, last))
        first = last + 1
    return runs


def _answer_index(blocks: List[Dict], first: int, last: int) -> int:
    """The agent's answer is the last text it wrote in the turn"""
    for index in range(last, first - 1, -1):
        block = blocks[index]
        if block.get("role") == "assistant" and block.get("content", "").strip():
            return index
    return -1


class ThreadBuilder:
    """Collects the thread as styled pieces, plain text and block regions at once.

    One pass gives three things that must agree: the string search runs on, the
    styled text the pane draws, and the line range each block occupies.
    """

    def __init__(self, gutter: bool = True):
        self.gutter = gutter
        self.pieces = []  # (text, style) for the styled render
        self.plain = []  # the same text, unstyled
        self.line = 0  # lines written so far
        self.chars = 0  # characters written so far
        self.line_offsets = []  # where each line starts in the text
        self.regions = []  # (first_line, last_line, fold key, role)

    def _add(self, text: str, style: str = ""):
        if not text:
            return
        self.pieces.append((text, style))
        self.plain.append(text)
        self.chars += len(text)

    def add_lines(self, text: str, style: str = "", marker: str = ""):
        """Add text line by line.

        The gutter is two columns wide. It carries the fold marker of the first
        line, and it is the strip that lights up under the cursor.
        """
        for offset, line in enumerate(text.split("\n")):
            self.line_offsets.append(self.chars)
            if self.gutter:
                self._add(marker if (marker and offset == 0) else GUTTER_BLANK, FOLD_STYLE)
            self._add(line, style)
            self._add("\n")
            self.line += 1

    def region(self, key, role: str):
        """Context manager marking the lines written by one foldable thing"""
        return _BlockRegion(self, key, role)

    def text(self) -> str:
        return "".join(self.plain)


class _BlockRegion:
    """Remembers which lines a block occupied, for the cursor to find it"""

    def __init__(self, builder: ThreadBuilder, key, role: str):
        self.builder = builder
        self.key = key
        self.role = role
        self.first = builder.line

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        last = max(self.first, self.builder.line - 1)
        self.builder.regions.append((self.first, last, self.key, self.role))
        return False


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
        # Blocks opened one at a time from the cursor
        self.expanded_blocks = set()
        self.cursor_line: int = 0
        # What the pane currently shows, so the same render is not repeated
        self._rendered_state = None
        self._last_args = None
        self.rendered_text = ""
        self.block_regions = []
        self._rows_key = None
        self._row_starts = []
        self._layout_key = None
        self._layout = None

    # ------------------------------------------------------------------ rows

    def content_width(self) -> int:
        """How wide the text is drawn, in characters"""
        try:
            content = self.query_one("#thread-content", ThreadContent)
        except Exception:
            return 80
        return max(20, content.container_size.width or content.size.width or 80)

    def row_starts(self) -> List[int]:
        """Screen row where each line of the text starts.

        Long lines wrap, so a line number is not a scroll position. The rows are
        counted once per rendering and reused while text and width stay the same.
        """
        text = self.rendered_text
        width = self.content_width()
        key = (len(text), width)
        if self._rows_key == key:
            return self._row_starts

        console = RichConsole(width=width)
        starts = []
        row = 0
        for line in text.split("\n"):
            starts.append(row)
            row += max(1, len(RichText(line).wrap(console, width)))

        self._rows_key = key
        self._row_starts = starts
        return starts

    def row_of_line(self, line: int) -> int:
        """Screen row where a text line starts"""
        starts = self.row_starts()
        if not starts:
            return 0
        return starts[max(0, min(line, len(starts) - 1))]

    def row_of_offset(self, char_pos: int) -> int:
        """Screen row holding this character of the text.

        Words are not cut in half when a line wraps, so the row inside a long
        line is found by wrapping that one line, not by dividing the column.
        """
        text = self.rendered_text
        line_number = text.count("\n", 0, char_pos)
        line_start = text.rfind("\n", 0, char_pos) + 1
        line_end = text.find("\n", char_pos)
        line = text[line_start : line_end if line_end != -1 else len(text)]
        column = char_pos - line_start

        row = self.row_of_line(line_number)
        width = self.content_width()
        pieces = RichText(line).wrap(RichConsole(width=width), width)

        used = 0
        for offset, piece in enumerate(pieces):
            used += len(piece.plain)
            if column < used:
                return row + offset
            used += 1  # the space the wrap swallowed
        return row + max(0, len(pieces) - 1)

    def line_of_offset(self, char_pos: int) -> int:
        return self.rendered_text.count("\n", 0, char_pos)

    def line_count(self) -> int:
        return len(self.row_starts())

    # --------------------------------------------------------------- cursor

    def move_cursor(self, delta: int):
        """Move the cursor and scroll just enough to keep it in view"""
        self.set_cursor(self.cursor_line + delta)

    def set_cursor(self, line: int, scroll: bool = True):
        last = max(0, self.line_count() - 1)
        line = max(0, min(line, last))
        if line != self.cursor_line:
            self.cursor_line = line
            self.redraw()
        if scroll:
            self.scroll_cursor_into_view()

    def scroll_cursor_into_view(self):
        """Keep a few lines of context above and below the cursor"""
        self.scroll_row_into_view(self.row_of_line(self.cursor_line))

    def scroll_row_into_view(self, row: int):
        """Scroll just enough to put a screen row in view"""
        top = int(self.scroll_y)
        height = max(1, self.size.height)
        margin = min(CURSOR_MARGIN, max(0, height // 2 - 1))

        if row < top + margin:
            self.scroll_to(0, max(0, row - margin), animate=False)
        elif row > top + height - 1 - margin:
            self.scroll_to(0, max(0, row - height + 1 + margin), animate=False)

    def cursor_block(self, roles=None):
        """(fold key, role) the cursor sits in, or None.

        Regions nest: a turn holds its chains, an open chain holds its steps.
        The innermost one wins, so 'o' always acts on the nearest thing.
        """
        found = None
        for first, last, key, role in self.block_regions:
            if not (first <= self.cursor_line <= last):
                continue
            if roles is not None and role not in roles:
                continue
            if found is None or (last - first) <= (found[1] - found[0]):
                found = (first, last, key, role)
        return (found[2], found[3]) if found else None

    def toggle_cursor_block(self, roles) -> Optional[str]:
        """Open or close the nearest foldable line under the cursor"""
        found = self.cursor_block(roles)
        if found is None:
            return None

        key = found[0]
        if key in self.expanded_blocks:
            self.expanded_blocks.discard(key)
            result = "collapsed"
        else:
            self.expanded_blocks.add(key)
            result = "expanded"

        self.redraw()
        self.scroll_cursor_into_view()
        return result

    # ---------------------------------------------------------- scroll keys

    def action_scroll_up(self):
        """Move the cursor up one line"""
        self.move_cursor(-1)

    def action_scroll_down(self):
        """Move the cursor down one line"""
        self.move_cursor(1)

    def action_scroll_page_up(self):
        """Move the cursor up one screen"""
        self.move_cursor(-max(1, self.size.height - 2))

    def action_scroll_page_down(self):
        """Move the cursor down one screen"""
        self.move_cursor(max(1, self.size.height - 2))

    # --------------------------------------------------------------- render

    def compose(self):
        """Compose the widget"""
        yield ThreadContent("", markup=True, id="thread-content")

    def on_mount(self):
        """Called when widget is mounted"""
        if self._pending_update:
            self._do_update_session(self._pending_update, user_only=False)
            self._pending_update = None

    def update_session(
        self,
        session: Session,
        user_only: bool = False,
        highlight_term: str = "",
        current_match=None,
    ):
        """Update the view with a session's messages"""
        if self.current_session is not session:
            # A different session starts at the top, with nothing unfolded
            self.cursor_line = 0
            self.expanded_blocks = set()

        state = (
            session.session_id,
            user_only,
            highlight_term,
            current_match,
            frozenset(self.expanded_blocks),
            self.cursor_line,
        )
        if state == self._rendered_state:
            return

        self._rendered_state = state
        self._last_args = (session, user_only, highlight_term, current_match)
        self.current_session = session
        try:
            self._do_update_session(session, user_only, highlight_term, current_match)
        except Exception:
            # Widget not mounted yet, store for later
            self._pending_update = session

    def redraw(self):
        """Draw again after the cursor moved or a block was folded"""
        if self._last_args is None:
            return
        self._rendered_state = None
        self.update_session(*self._last_args)

    def clear_content(self):
        """Empty the view, e.g. when a search returns no sessions"""
        self.current_session = None
        self._pending_update = None
        self._rendered_state = None
        self._last_args = None
        self.rendered_text = ""
        self.block_regions = []
        self.cursor_line = 0
        self._rows_key = None
        self._layout_key = None
        self._layout = None
        try:
            self.query_one("#thread-content", ThreadContent).update("")
        except Exception:
            pass

    # ---------------------------------------------------------------- build

    def build(
        self,
        session: Session,
        user_only: bool = False,
        highlight_term: str = "",
        gutter: bool = True,
        expand_everything: bool = False,
    ) -> ThreadBuilder:
        """Lay out the whole thread once.

        Rendering, searching and the cursor all read the result, so a match
        found by search is always a match that is on screen.
        """
        blocks = session.load_blocks()
        if user_only:
            blocks = [b for b in blocks if b.get("role") == "user"]

        term = highlight_term.lower() if highlight_term else ""
        out = ThreadBuilder(gutter=gutter)

        out.add_lines(f"Session: {session.session_id}", "bold")
        out.add_lines(f"Project: {session.project_path}", "dim")
        out.add_lines(f"Date:    {session.date_str}", "dim")
        if session.tag:
            out.add_lines(f"Tag:     {session.tag}", "dim")

        if not blocks:
            out.add_lines("")
            out.add_lines("No messages found in this session.", "dim italic")
            return out

        for first, last in _runs(blocks):
            self._add_run(out, blocks, first, last, term, expand_everything)

        return out

    def _add_run(self, out, blocks, first, last, term, expand_everything):
        """One speaker's turn: a user message, or everything the agent did"""
        role = blocks[first].get("role", "unknown")
        heading = ROLE_HEADINGS.get(role, role.title())

        if heading != ROLE_HEADINGS["assistant"]:
            self._add_heading(out, heading)
            with out.region(first, role):
                texts = [blocks[i].get("content", "") for i in range(first, last + 1)]
                out.add_lines("\n\n".join(texts))
                out.add_lines("")
            return

        # The agent's turn. Its answer is the last thing it wrote; everything
        # before that is working: thinking, tool calls, tool results and the
        # short notes between them. The working part is folded away by default.
        answer = _answer_index(blocks, first, last)
        key = ("turn", first)
        show_work = (
            expand_everything
            or key in self.expanded_blocks
            or self._run_holds(blocks, first, last, answer, term)
        )

        with out.region(key, "turn"):
            self._add_heading(out, heading, FOLD_OPEN if show_work else FOLD_CLOSED)

            pieces = self._run_pieces(
                blocks, first, last, answer, term, expand_everything, show_work
            )
            if not pieces:
                # The agent worked but wrote nothing. Without this line the two
                # user messages around it would run together.
                out.add_lines(WORKED_SILENTLY, CHAIN_STYLE)
                out.add_lines("")
                return

            for piece in pieces:
                piece(out)

    def _run_holds(self, blocks, first, last, answer, term) -> bool:
        """Is the search term hiding in the working part of this turn?"""
        if not term:
            return False
        return any(
            term in blocks[i].get("content", "").lower()
            for i in range(first, last + 1)
            if i != answer
        )

    def _run_pieces(self, blocks, first, last, answer, term, expand_everything, show_work):
        """What of the agent's turn is worth drawing, in order"""
        pieces = []
        index = first
        while index <= last:
            role = blocks[index].get("role")

            if index == answer:
                pieces.append(self._answer_piece(blocks[index], index, role))
                index += 1
                continue

            if role in FOLDABLE_ROLES:
                stop = index
                while stop + 1 <= last and blocks[stop + 1].get("role") in FOLDABLE_ROLES:
                    stop += 1
                piece = self._chain_piece(
                    blocks, index, stop, term, expand_everything, show_work
                )
                if piece:
                    pieces.append(piece)
                index = stop + 1
                continue

            piece = self._note_piece(blocks[index], index, term, show_work)
            if piece:
                pieces.append(piece)
            index += 1

        return pieces

    def _add_heading(self, out, heading: str, marker: str = ""):
        out.add_lines("")
        rule = "━" * max(3, HEADING_WIDTH - len(heading) - 4)
        out.add_lines(f"━━ {heading} {rule}", ROLE_STYLES.get(heading, "bold"), marker=marker)
        out.add_lines("")

    def _answer_piece(self, block, index, role):
        """The text the agent finished with, always shown"""

        def draw(out):
            with out.region(index, role):
                out.add_lines(block.get("content", ""))
                out.add_lines("")

        return draw

    def _note_piece(self, block, index, term, show_work):
        """A short note the agent wrote while working, quoted with '>'"""
        text = block.get("content", "")
        if not show_work or not text.strip():
            return None

        def draw(out):
            with out.region(index, "assistant"):
                for line in text.split("\n"):
                    out.add_lines(f"> {line}" if line else ">", NOTE_STYLE)

        return draw

    def _chain_piece(self, blocks, first, last, term, expand_everything, show_work):
        """A run of thinking and tool steps, shown as one line until opened"""
        steps = list(range(first, last + 1))
        holds_term = term and any(term in blocks[i].get("content", "").lower() for i in steps)

        if not show_work:
            return None

        # A chain and its first step are different things to fold, so the chain
        # gets its own key.
        key = ("chain", first)
        opened = expand_everything or key in self.expanded_blocks
        names = []
        for i in steps:
            name = blocks[i].get("name", "")
            if blocks[i].get("role") != "tool_result" and name not in names:
                names.append(name)
        summary = ", ".join(names[:CHAIN_NAMES_SHOWN]) or "steps"
        if len(names) > CHAIN_NAMES_SHOWN:
            summary += ", …"
        label = f"{CHAIN_ICON} chain ({len(steps)} steps: {summary})"

        if not (opened or holds_term):

            def draw(out):
                with out.region(key, "chain"):
                    out.add_lines(label, CHAIN_STYLE, marker=FOLD_CLOSED)

            return draw

        def draw(out):
            # The header stays when the chain is open, so it can be closed again
            with out.region(key, "chain"):
                out.add_lines(label, CHAIN_STYLE, marker=FOLD_OPEN)
            for i in steps:
                with out.region(i, blocks[i].get("role")):
                    self._add_foldable(out, blocks[i], i, term, expand_everything)

        return draw

    def _add_foldable(self, out, block, index, term, expand_everything):
        """One tool call, tool result or thinking block"""
        text = block.get("content", "")
        role = block.get("role")
        name = block.get("name", role)
        icon = FOLDABLE_ICONS.get(role, "*")
        style = FOLDABLE_STYLES.get(role, "dim")

        is_open = (
            expand_everything or index in self.expanded_blocks or (term and term in text.lower())
        )

        if not is_open:
            if not text.strip():
                # Nothing to open, so no marker either
                out.add_lines(f"{icon} {name}: (no text output)", style)
                return
            lines = text.splitlines()
            flat = " ".join(text.split())
            head = flat[:TOOL_PREVIEW_CHARS]
            if len(flat) > TOOL_PREVIEW_CHARS:
                head += " …"
            extra = f" ({len(lines)} lines)" if len(lines) > 1 else ""
            out.add_lines(f"{icon} {name}{extra}: {head}", style, marker=FOLD_CLOSED)
            return

        out.add_lines(f"{icon} {name}", style, marker=FOLD_OPEN)
        out.add_lines(text)
        out.add_lines("")

    def build_text(
        self,
        session: Session,
        user_only: bool = False,
        highlight_term: str = "",
    ) -> str:
        """The exact text the pane shows"""
        return self.build(session, user_only=user_only, highlight_term=highlight_term).text()

    def export_text(self, session: Session, full: bool = False, user_only: bool = False) -> str:
        """Thread text for a file or the clipboard: no cursor gutter"""
        return self.build(
            session, user_only=user_only, gutter=False, expand_everything=full
        ).text()

    def _do_update_session(
        self,
        session: Session,
        user_only: bool = False,
        highlight_term: str = "",
        current_match=None,
    ):
        """Internal method to update the session view"""
        # Moving the cursor changes only which gutter is lit, so the layout is
        # kept and reused. Rebuilding it on every keypress was far too slow.
        key = (
            session.session_id,
            user_only,
            highlight_term,
            frozenset(self.expanded_blocks),
        )
        if key != self._layout_key:
            built = self.build(session, user_only=user_only, highlight_term=highlight_term)
            self._layout_key = key
            self._layout = built
            self.rendered_text = built.text()
            self.block_regions = built.regions
            self._rows_key = None

        built = self._layout
        rendered = RichText()
        for piece, style in built.pieces:
            rendered.append(piece, style or None)

        if 0 <= self.cursor_line < len(built.line_offsets):
            start = built.line_offsets[self.cursor_line]
            rendered.stylize(CURSOR_STYLE, start, start + len(GUTTER_BLANK))

        if highlight_term:
            rendered.highlight_regex(f"(?i){re.escape(highlight_term)}", style="reverse yellow")
            if current_match:
                # The match you are standing on stands out from the rest
                start, end = current_match
                rendered.stylize(CURRENT_MATCH_STYLE, start, end)

        self.query_one("#thread-content", ThreadContent).update(rendered)

class HelpScreen(ModalScreen):
    """Modal screen showing keyboard shortcuts"""

    BINDINGS = [
        Binding("up", "scroll_up", "Scroll Up", priority=True),
        Binding("down", "scroll_down", "Scroll Down", priority=True),
        Binding("left", "noop", "", priority=True),
        Binding("right", "noop", "", priority=True),
        Binding("pageup", "scroll_page_up", "Page Up", priority=True),
        Binding("pagedown", "scroll_page_down", "Page Down", priority=True),
    ] + [Binding(key, "dismiss", "Close", priority=True) for key in sorted(HELP_CLOSE_KEYS)]

    HELP_TEXT = APP_HELP_TEXT

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="help-container"):
            yield Static("Keyboard Shortcuts", id="help-title")
            yield Static(self.HELP_TEXT)
            closers = " / ".join(sorted(format_key(k) for k in HELP_CLOSE_KEYS))
            yield Static(f"Press {closers} to close", id="help-footer")

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
        elif key in HELP_CLOSE_KEYS:
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

    BINDINGS = APP_BINDINGS


    def check_action(self, action: str, parameters) -> bool | None:
        """Disable app actions when a modal is active."""
        if any(isinstance(s, ModalScreen) for s in self.screen_stack):
            if any(isinstance(s, HelpScreen) for s in self.screen_stack):
                return action == "show_help" or None
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
        # None = no search active; a list (even empty) = search results
        self.filtered_sessions: Optional[List[Session]] = None
        self.initial_session_number: Optional[int] = initial_session_number
        self._last_g_press: Optional[float] = None  # Track double g press
        # Thread search state
        self._thread_search_term: str = ""
        self._thread_search_matches: List[int] = []  # Character positions of matches
        self._thread_search_index: int = -1  # Current match index
        self._thread_raw_text: str = ""  # Raw text content for searching
        # Pane width (percentage for left pane)
        self._left_pane_width: int = 30
        # CWD filter mode (ON by default)
        self.cwd_filter_mode: bool = True
        # Project filter mode (filter to selected session's project)
        self.project_filter_mode: bool = False

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

        if KEY_CONFIG_PROBLEMS:
            self.notify(
                "\n".join(KEY_CONFIG_PROBLEMS),
                title=f"Config: {CONFIG_PATH}",
                severity="warning",
                timeout=8,
            )

        # Apply default CWD filter
        if self.cwd_filter_mode:
            cwd = os.getcwd()
            filtered = [s for s in self.session_manager.sessions if s.project_path == cwd]
            if filtered:
                self.session_list._populate(filtered)
            else:
                self.cwd_filter_mode = False

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
                self._show_session(session)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle session selection"""
        if self.session_list is not None and self.thread_view is not None:
            session = self.session_list.get_selected_session()
            if session:
                self._show_session(session)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Follow the highlighted session in the thread pane.

        The list moves for many reasons: our own keys, a mouse click, or
        ListView's built-in keys when a navigation key is unbound in the config.
        Reacting to the move covers all of them.
        """
        if self.session_list is None or self.thread_view is None:
            return
        session = self.session_list.get_selected_session()
        if session is None:
            return

        # Match positions belong to one session's text, so moving away drops them
        current = self.thread_view.current_session
        if self._thread_search_term and (current is None or current is not session):
            self._thread_search_term = ""
            self._thread_search_matches = []
            self._thread_search_index = -1
            self._thread_raw_text = ""

        self._show_session(session)

    def action_focus_left(self):
        """Focus the left panel (session list)"""
        if self.session_list is not None:
            self.set_focus(self.session_list)

    def action_focus_right(self):
        """Focus the right panel (thread view)"""
        if self.thread_view is not None:
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
        if self.session_list is not None and self.thread_view is not None:
            self.session_list.styles.width = f"{self._left_pane_width}%"
            self.thread_view.styles.width = f"{100 - self._left_pane_width}%"

    def action_move_up(self):
        """Move selection up - works contextually based on focused panel"""
        focused = self.focused
        if focused == self.session_list:
            # Navigate session list - use current displayed sessions
            if self.session_list is not None and self.session_list.index:
                self.session_list.index -= 1
                session = self.session_list.get_selected_session()
                if session and self.thread_view is not None:
                    self._show_session(session)
        elif focused == self.thread_view:
            # Delegate to thread view's scroll action
            self.thread_view.action_scroll_up()

    def action_move_down(self):
        """Move selection down - works contextually based on focused panel"""
        focused = self.focused
        if focused == self.session_list:
            # Navigate session list - use current displayed sessions
            current_sessions = self.session_list._displayed_sessions()
            index = self.session_list.index if self.session_list is not None else None
            if index is not None and index < len(current_sessions) - 1:
                self.session_list.index += 1
                session = self.session_list.get_selected_session()
                if session and self.thread_view is not None:
                    self._show_session(session)
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
            if self.session_list is not None and self.session_list.index:
                # Scroll up by a page worth (approximately 10 items or visible height)
                new_index = max(0, self.session_list.index - 10)
                self.session_list.index = new_index
                session = self.session_list.get_selected_session()
                if session and self.thread_view is not None:
                    self._show_session(session)
        # Don't change focus - only work in active pane

    def action_page_down(self):
        """Scroll in the active pane"""
        focused = self.focused
        if focused == self.thread_view:
            # If thread view is focused, scroll it
            self.thread_view.action_scroll_page_down()
        elif focused == self.session_list:
            # If session list is focused, scroll it
            current_sessions = self.session_list._displayed_sessions()
            index = self.session_list.index if self.session_list is not None else None
            if index is not None and index < len(current_sessions) - 1:
                # Scroll down by a page worth (approximately 10 items or visible height)
                new_index = min(len(current_sessions) - 1, self.session_list.index + 10)
                self.session_list.index = new_index
                session = self.session_list.get_selected_session()
                if session and self.thread_view is not None:
                    self._show_session(session)
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
                if self.session_list is not None:
                    self.session_list.index = 0
                    session = self.session_list.get_selected_session()
                    if session and self.thread_view is not None:
                        self._show_session(session)
            elif focused == self.thread_view:
                # Scroll to top of thread
                if self.thread_view is not None:
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
            current_sessions = self.session_list._displayed_sessions()
            if self.session_list is not None and current_sessions:
                self.session_list.index = len(current_sessions) - 1
                session = self.session_list.get_selected_session()
                if session and self.thread_view is not None:
                    self._show_session(session)
        elif focused == self.thread_view:
            # Scroll to absolute bottom of thread
            if self.thread_view is not None:
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
        if self.thread_view is None:
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
        if self.session_list is None:
            return

        session = self.session_list.get_selected_session()
        if not session:
            return

        existing_tag = session.tag or ""

        # Use input dialog - Textual's Input widget needs to be in a screen
        class TagInputScreen(ModalScreen):
            def compose(self):
                yield EscapableInput(
                    value=existing_tag,
                    placeholder="Enter tag name (ESC to cancel)",
                    id="tag-input",
                )

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
                self.session_list._populate(
                    self._get_filtered_sessions(), initial_index=current_index
                )
                if self.thread_view is not None:
                    self._show_session(session)
            # If tag_value is None, user pressed ESC - do nothing

        self.push_screen(TagInputScreen(), handle_tag)

    def action_copy_session_command(self):
        """Start claude session in the project directory"""
        if self.session_list is None:
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
        if self.session_list is None:
            return

        session = self.session_list.get_selected_session()
        if not session or self.thread_view is None:
            return

        # The same text the pane shows, without the cursor gutter
        markdown_content = self.thread_view.export_text(session, user_only=self.user_only_mode)

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

    def action_export_session(self, full: bool = False):
        """Export current session thread as a markdown file.

        Plain export keeps the conversation only. A full export adds thinking,
        tool calls and tool results, all opened.
        """
        if self.session_list is None:
            return

        session = self.session_list.get_selected_session()
        if not session:
            return

        suffix = "-full" if full else ""
        if session.tag:
            filename = f"{session.session_id}-{session.tag}{suffix}.md"
        else:
            filename = f"{session.session_id}{suffix}.md"

        filepath = os.path.join(os.getcwd(), filename)

        try:
            if full and self.thread_view is not None:
                body = self.thread_view.export_text(session, full=True)
            else:
                body = self._plain_export_text(session)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(body)

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

    def _plain_export_text(self, session: Session) -> str:
        """Conversation only, as markdown"""
        messages = session.load_messages()

        content = []
        content.append(f"# Claude Session: {session.session_id}\n\n")
        content.append(f"**Project:** `{session.project_path}`\n\n")
        content.append(f"**Date:** {session.date_str}\n\n")
        if session.tag:
            content.append(f"**Tag:** {session.tag}\n\n")
        content.append("---\n\n")

        for msg in messages:
            role = msg.get("role", "unknown")
            text = msg.get("content", "")
            heading = ROLE_HEADINGS.get(role)
            if heading:
                content.append(f"## {heading}\n\n{text}\n\n")

        if not messages:
            content.append("*No messages found in this session.*\n")

        return "".join(content)

    def action_delete_session(self):
        """Delete the current session"""
        if self.session_list is None:
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
                    filtered = self._get_filtered_sessions()
                    # Calculate the new index (same position, or last if we deleted the last one)
                    new_index = 0
                    if filtered:
                        new_index = min(current_index, len(filtered) - 1)

                    # Refresh session list with the new index
                    self.session_list._populate(filtered, initial_index=new_index)

                    # Update thread view
                    if self.session_manager.sessions:
                        new_session = self.session_list.get_selected_session()
                        if new_session and self.thread_view is not None:
                            self._show_session(new_session)
                    else:
                        # No sessions left, clear thread view
                        if self.thread_view is not None:
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
        if self.session_list is not None and self.thread_view is not None:
            session = self.session_list.get_selected_session()
            if session:
                self._show_session(session)

        # Show notification
        mode_text = "User messages only" if self.user_only_mode else "All messages"
        self.notify(
            f"Filter: {mode_text}", title="Filter Toggled", severity="information", timeout=2
        )

    def action_toggle_tool_output(self):
        """Open or close the chain or the single step the cursor sits in"""
        self._toggle_block(FOLD_ROLES, "foldable line")

    def _toggle_block(self, roles, what: str):
        """Fold or unfold the block under the thread cursor"""
        if self.thread_view is None:
            return

        result = self.thread_view.toggle_cursor_block(roles)
        if result is None:
            self.notify(
                f"Cursor is not on a {what}", title="Thread", severity="warning", timeout=2
            )
            return

        self._refind_matches()
        self.notify(f"{what.title()} {result}", title="Thread", severity="information", timeout=2)

    def _refind_matches(self):
        """Folding moves text around, so the matches are found again"""
        if self._thread_search_term:
            self._search_in_thread(self._thread_search_term)

    def _search_sessions(self, query: str) -> List[Session]:
        """Search sessions by query string, inside the active directory filter"""
        if not query or not query.strip():
            return self._get_filtered_sessions()

        query_lower = query.lower().strip()
        matching_sessions = []

        for session in self._get_filtered_sessions():
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

            # Search in the full session text (messages, tool calls, tool results)
            if session.matches(query_lower):
                matching_sessions.append(session)

        return matching_sessions

    def _apply_search_filter(self, query: str):
        """Apply search filter to sessions"""
        self.search_query = query
        if query and query.strip():
            self.filtered_sessions = self._search_sessions(query)
        else:
            self.filtered_sessions = None

        # Update session list with filtered results
        if self.session_list is not None:
            if self.filtered_sessions is not None:
                self.session_list._populate(self.filtered_sessions)
            else:
                self.session_list._populate(self._get_filtered_sessions())

            displayed = self.session_list._displayed_sessions()
            if self.session_list.index is None or self.session_list.index >= len(displayed):
                self.session_list.index = 0

            # Update thread view; with no results there is nothing to show
            if self.thread_view is not None:
                if displayed:
                    session = self.session_list.get_selected_session()
                    if session:
                        self._show_session(session)
                else:
                    self.thread_view.clear_content()

    def _thread_has_focus(self) -> bool:
        """Is the thread pane the active pane?"""
        focused = self.focused
        if focused is None or self.thread_view is None:
            return False
        return focused is self.thread_view or getattr(focused, "parent", None) is self.thread_view

    def _goto_thread_line(self, number: int):
        """Put the thread cursor on a line"""
        if self.thread_view is None:
            return

        last = self.thread_view.line_count()
        line = max(1, min(number, last))
        self.thread_view.set_cursor(line - 1)
        if line != number:
            self.notify(
                f"Thread has {last} lines, stopped at {line}",
                title="Goto",
                severity="warning",
                timeout=2,
            )

    def _highlight_term(self) -> str:
        """The term the thread pane should highlight right now"""
        return self._thread_search_term or self.search_query

    def _current_match_span(self):
        """Where the match the user is standing on sits in the thread text"""
        if not self._thread_search_term or not self._thread_search_matches:
            return None
        if not 0 <= self._thread_search_index < len(self._thread_search_matches):
            return None
        start = self._thread_search_matches[self._thread_search_index]
        return (start, start + len(self._thread_search_term))

    def _show_session(self, session: Session):
        """Render a session in the thread pane with the active highlight"""
        if self.thread_view is None:
            return
        self.thread_view.update_session(
            session,
            user_only=self.user_only_mode,
            highlight_term=self._highlight_term(),
            current_match=self._current_match_span(),
        )

    def _get_thread_raw_text(self) -> str:
        """Text of the current thread, exactly as the pane shows it"""
        if self.session_list is None or self.thread_view is None:
            return ""

        session = self.session_list.get_selected_session()
        if not session:
            return ""

        return self.thread_view.build_text(
            session,
            user_only=self.user_only_mode,
            highlight_term=self._highlight_term(),
        )

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
        if self.thread_view is not None and self.session_list is not None:
            session = self.session_list.get_selected_session()
            if session:
                self._show_session(session)

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
        """Put the cursor on a match and bring it into view"""
        if not self._thread_search_matches or match_index < 0:
            return

        if match_index >= len(self._thread_search_matches):
            match_index = 0
        if self.thread_view is None or self.session_list is None:
            return

        # Redraw first: the match being stood on is drawn differently
        session = self.session_list.get_selected_session()
        if session:
            self._show_session(session)

        # A long line wraps over many rows, so the cursor goes on the line and
        # the view is scrolled to the row the match itself sits on.
        char_pos = self._thread_search_matches[match_index]
        self.thread_view.set_cursor(self.thread_view.line_of_offset(char_pos), scroll=False)
        self.thread_view.scroll_row_into_view(self.thread_view.row_of_offset(char_pos))
        _debug_log(f"match {match_index} at char {char_pos}, line {self.thread_view.cursor_line}")

    def _clear_thread_search(self):
        """Clear thread search state"""
        self._thread_search_term = ""
        self._thread_search_matches = []
        self._thread_search_index = -1
        self._thread_raw_text = ""

        # Refresh thread view without highlighting
        if self.thread_view is not None and self.session_list is not None:
            session = self.session_list.get_selected_session()
            if session:
                self._show_session(session)

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
            self.filtered_sessions
            if self.filtered_sessions is not None
            else self._get_filtered_sessions()
        )

        if 1 <= number <= len(sessions):
            # Find the session in the current list
            target_session = sessions[number - 1]

            # Set index to the target
            target_index = number - 1

            # Make sure the session list shows the right sessions
            # Pass initial_index to set it during population
            if self.filtered_sessions is not None:
                self.session_list._populate(
                    self.filtered_sessions, preserve_index=False, initial_index=target_index
                )
            else:
                self.session_list._populate(
                    self._get_filtered_sessions(),
                    preserve_index=False,
                    initial_index=target_index,
                )

            # Update thread view first
            if self.thread_view is not None:
                self._show_session(target_session)

            # Focus the session list immediately so highlight will be visible
            self.set_focus(self.session_list)

            # Use multiple callbacks to ensure selection is properly applied
            # after list is fully rendered
            def ensure_selection():
                if target_index < len(self.session_list._displayed_sessions()):
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
        if self.session_list is None:
            return

        is_thread_focused = self._thread_has_focus()
        where = "thread" if is_thread_focused else "sessions"

        # Use modal screen like tag input
        class SearchInputScreen(ModalScreen):
            def compose(self):
                yield EscapableInput(
                    placeholder=f"Search {where}... (ESC to cancel)", id="search-input"
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

            if is_thread_focused and value:
                # Search within thread content
                self._search_in_thread(value)
            elif value:
                # Filter sessions list
                self._apply_search_filter(value)
                result_count = len(self.filtered_sessions or [])
                if result_count:
                    self.notify(
                        f"Search: {value} ({result_count} results)",
                        title="Search",
                        severity="information",
                        timeout=2,
                    )
                else:
                    self.notify(
                        f"No sessions match: {value}",
                        title="Search",
                        severity="warning",
                        timeout=3,
                    )
            else:
                # Empty search clears filter
                self._apply_search_filter("")
                self._clear_thread_search()
                self.notify("Search cleared", title="Search", severity="information", timeout=2)

        self.push_screen(SearchInputScreen(), handle_search)

    def action_command_mode(self):
        """Enter command mode"""
        if self.session_list is None:
            return

        # The pane that has focus decides what a number means, and focus can
        # move while the modal is open, so it is read now.
        is_thread_focused = self._thread_has_focus()
        target = "line in thread" if is_thread_focused else "session"
        app = self

        # Use modal screen like tag input
        class CommandInputScreen(ModalScreen):
            def compose(self):
                yield EscapableInput(
                    placeholder=f"Number to goto {target}, or a command... (TAB completes)",
                    id="command-input",
                )

            def on_mount(self):
                """Focus the input when mounted"""
                input_widget = self.query_one("#command-input", EscapableInput)
                input_widget.focus()

            def on_key(self, event) -> None:
                """TAB completes the command being typed"""
                if event.key != "tab":
                    return
                event.stop()
                event.prevent_default()

                widget = self.query_one("#command-input", EscapableInput)
                completion, hint = complete_command(widget.value)
                if completion != widget.value:
                    widget.value = completion
                    widget.cursor_position = len(completion)
                if hint:
                    app.notify(hint, title="Commands", severity="information", timeout=3)

            def on_input_submitted(self, event: Input.Submitted):
                value = event.value.strip()
                self.dismiss(value)

        def handle_command(value):
            if value is None:
                # User pressed ESC - do nothing
                return
            if value:
                self._run_command(value, is_thread_focused)

        self.push_screen(CommandInputScreen(), handle_command)

    def _run_command(self, value: str, is_thread_focused: bool):
        """Act on what was typed after ':'"""
        command = value.strip().lower()

        if command in QUIT_COMMANDS:
            self.exit()
            return

        if command in ("export", "e"):
            self.action_export_session()
            return

        if command in ("export full", "export-full", "ef"):
            self.action_export_session(full=True)
            return

        try:
            number = int(command)
        except ValueError:
            self.notify(f"Unknown command: {value}", title="Error", severity="error", timeout=3)
            return

        if is_thread_focused:
            self._goto_thread_line(number)
        else:
            self._goto_session(number)

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

    def _get_filtered_sessions(self) -> List[Session]:
        """Return sessions filtered by current filter state"""
        if self.cwd_filter_mode:
            cwd = os.getcwd()
            return [s for s in self.session_manager.sessions if s.project_path == cwd]
        if self.project_filter_mode and self.session_list is not None:
            session = self.session_list.get_selected_session()
            if session:
                return [
                    s for s in self.session_manager.sessions
                    if s.project_path == session.project_path
                ]
        return self.session_manager.sessions

    def action_toggle_cwd_filter(self):
        """Toggle CWD filter (ON by default, '.' shows all)"""
        self.cwd_filter_mode = not self.cwd_filter_mode
        self.project_filter_mode = False
        self._apply_directory_filter()

    def action_toggle_project_filter(self):
        """Toggle filter to selected session's project directory"""
        self.project_filter_mode = not self.project_filter_mode
        if self.project_filter_mode:
            self.cwd_filter_mode = False
        self._apply_directory_filter()

    def _apply_directory_filter(self):
        """Repopulate session list based on current filter state"""
        if self.session_list is None:
            return

        # Changing the directory filter starts a fresh view, so drop any search
        self.search_query = ""
        self.filtered_sessions = None

        if self.cwd_filter_mode:
            cwd = os.getcwd()
            filtered = [s for s in self.session_manager.sessions if s.project_path == cwd]
            if filtered:
                self.session_list._populate(filtered)
                self.notify(
                    f"CWD filter ({len(filtered)})", title="Filter", severity="information", timeout=2
                )
            else:
                self.cwd_filter_mode = False
                self.session_list._populate(self.session_manager.sessions)
                self.notify(
                    "No sessions for CWD, showing all", title="Filter", severity="warning", timeout=2
                )
        elif self.project_filter_mode:
            session = self.session_list.get_selected_session()
            if session:
                project = session.project_path
                filtered = [
                    s for s in self.session_manager.sessions if s.project_path == project
                ]
                self.session_list._populate(filtered)
                self.notify(
                    f"Project filter: {session.project_name} ({len(filtered)})",
                    title="Filter",
                    severity="information",
                    timeout=2,
                )
        else:
            self.session_list._populate(self.session_manager.sessions)
            self.notify("All sessions", title="Filter", severity="information", timeout=2)

        if self.session_list is not None and self.thread_view is not None:
            session = self.session_list.get_selected_session()
            if session:
                self._show_session(session)


def write_default_config(path: Path = CONFIG_PATH) -> bool:
    """Write the commented example config. Returns False if one is already there."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_config_template(), encoding="utf-8")
    return True


def _find_session_file(session_id: str) -> Optional[Path]:
    """Find the JSONL file for a given session ID"""
    projects_dir = Path.home() / ".claude" / "projects"
    for session_file in projects_dir.rglob(f"{session_id}.jsonl"):
        return session_file
    return None


def _find_existing_tag(tag: str) -> Optional[str]:
    """Scan all JSONL files for a custom-title matching the given tag.
    Returns the session_id if found, None otherwise."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return None
    for session_file in projects_dir.rglob("*.jsonl"):
        if session_file.stem.startswith("agent-"):
            continue
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "custom-title" and entry.get("customTitle") == tag:
                            return session_file.stem
                    except json.JSONDecodeError:
                        continue
        except Exception:
            continue
    return None


def create_tagged_session(tag: str, temp: bool = False):
    """Create a new Claude session with a tag and launch it."""
    # Check if tag already exists
    old_session_id = None
    existing_session_id = _find_existing_tag(tag)
    if existing_session_id:
        print(f"Tag '{tag}' already exists (session {existing_session_id[:8]})")
        reply = input("[Y]connect / [n]abort / [o]verwrite: ").strip().lower()
        if reply == "n":
            print("Aborted.")
            sys.exit(0)
        elif reply == "o":
            print("Removing old session and creating new...")
            old_session_id = existing_session_id
        else:
            print("Connecting to existing session...")
            env = os.environ.copy()
            env["CLAUDE_SESSION_ID"] = existing_session_id
            os.execvpe("claude", ["claude", "--resume", existing_session_id], env)

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
        # Remove old session if overwriting
        if old_session_id:
            old_file = _find_session_file(old_session_id)
            if old_file:
                old_file.unlink()
                print(f"Removed old session {old_session_id[:8]}")

        # Append custom-title to the new session's JSONL file
        session_file = _find_session_file(session_id)
        if session_file:
            entry = {"type": "custom-title", "customTitle": tag, "sessionId": session_id}
            with open(session_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        print(f"Created session {session_id[:8]} with tag: {tag}")

    # Launch claude with session_id in environment
    env = os.environ.copy()
    env["CLAUDE_SESSION_ID"] = session_id

    if temp:
        # Run claude (not exec) so we can cleanup after
        subprocess.run(["claude", "--resume", session_id], env=env)
        # Cleanup: find and delete session file
        session_file = _find_session_file(session_id)
        if session_file:
            session_file.unlink()
            # Also remove session directory if exists
            session_dir = session_file.parent / session_id
            if session_dir.is_dir():
                try:
                    session_dir.rmdir()
                except OSError:
                    pass
            print(f"Cleaned up temp session {session_id[:8]}")
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
        epilog="Examples: clod, clod +10, clod 'my-tag', clod -t 'temp-tag', clod -d abc12345",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging to /tmp/claude-yelp-debug.log"
    )
    parser.add_argument(
        "-d", "--delete", metavar="SESSION_ID", help="Delete a session by ID (supports partial match)"
    )
    parser.add_argument(
        "--write-config",
        action="store_true",
        help=f"Write an example shortcut config to {CONFIG_PATH}",
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

    if args.write_config:
        if write_default_config():
            print(f"Wrote {CONFIG_PATH}")
        else:
            print(f"{CONFIG_PATH} already exists, left as is")
        sys.exit(0)

    for problem in KEY_CONFIG_PROBLEMS:
        print(f"config: {problem}", file=sys.stderr)

    if args.delete:
        sm = SessionManager()
        matches = [s for s in sm.sessions if s.session_id.startswith(args.delete)]
        if not matches:
            print(f"No session found matching '{args.delete}'", file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"Ambiguous ID '{args.delete}', matches {len(matches)} sessions:", file=sys.stderr)
            for s in matches:
                print(f"  {s.session_id}", file=sys.stderr)
            sys.exit(1)
        session = matches[0]
        label = f"{session.session_id[:8]} ({session.tag})" if session.tag else session.session_id[:8]
        if sm.delete_session(session.session_id):
            print(f"Deleted session {label}")
        else:
            print(f"Failed to delete session {label}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

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
