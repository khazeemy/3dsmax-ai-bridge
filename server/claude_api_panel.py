"""
3ds Max AI Bridge - Direct API Panel
=======================================

A dockable panel inside 3ds Max that talks DIRECTLY to the Anthropic API
(no Claude Desktop, no MCP server, no socket) and executes tool calls
in-process via the same action functions used by max_ai_listener.py.

COST WARNING: unlike Claude Desktop, this uses pay-per-token API billing,
separate from any Claude.ai/Desktop subscription. See
https://platform.claude.com/docs/en/about-claude/pricing for current
rates. For light/occasional use this is typically well under $1-2/month,
but it is not free the way chatting in Claude Desktop is.

Requirements:
    - A .env file in this same folder containing:
          ANTHROPIC_API_KEY=sk-ant-...
      NEVER put your actual key in this script or in chat with anyone -
      only in that local .env file, which stays on your machine.
    - python-dotenv (already present if you followed this project's setup)
    - max_ai_listener.py in the same folder (we import its action
      functions directly rather than duplicating tool logic)

How to run it inside Max:
    1. Open 3ds Max.
    2. Scripting > Run Script... and select this file.
    3. A panel appears with a text box and "Ask Claude" button.
    4. Type a request and click the button (or Ctrl+Enter).
    5. Claude's response appears directly in the panel - no other app
       involved.
"""

import json
import os
import urllib.error
import urllib.request

import pymxs

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets  # noqa: F401

# Reuse the existing, already-tested action handlers rather than
# duplicating tool logic. Both this panel and the listener run inside
# Max's own Python process, so we can call these functions directly -
# no socket needed when caller and executor are the same process.
import max_ai_listener

rt = pymxs.runtime

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048

# Tool definitions sent to the API, describing the same actions
# max_ai_listener.py implements. Keep these in sync with ACTIONS in that
# file if new actions are added.
TOOL_DEFINITIONS = [
    {
        "name": "get_scene_state",
        "description": "Get a snapshot of every object in the current 3ds Max scene.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_selection",
        "description": "Get the objects currently selected in the 3ds Max viewport.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_materials",
        "description": "List materials available in the scene, including ones not yet applied to anything.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "apply_material",
        "description": "Apply a named material to one or more named objects.",
        "input_schema": {
            "type": "object",
            "properties": {
                "material_name": {"type": "string"},
                "object_names": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["material_name", "object_names"],
        },
    },
]


def _load_api_key():
    """
    Read ANTHROPIC_API_KEY from a .env file sitting next to this script.
    We deliberately do NOT fall back to a hardcoded key or prompt-and-store
    pattern - the key only ever lives in that local file, never in this
    code or in any UI field that could be screenshotted/logged.
    """
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        # Some script-execution paths inside 3ds Max don't set __file__.
        # Fall back to the current working directory, and print a clear
        # diagnostic so this isn't a silent mystery if it happens again.
        script_dir = os.getcwd()
        print(f"[claude_api_panel] __file__ not available, falling back to cwd: {script_dir}")

    env_path = os.path.join(script_dir, ".env")
    print(f"[claude_api_panel] Looking for .env at: {env_path}")
    print(f"[claude_api_panel] File exists: {os.path.isfile(env_path)}")

    if not os.path.isfile(env_path):
        return None
    try:
        from dotenv import dotenv_values
        values = dotenv_values(env_path)
        key = values.get("ANTHROPIC_API_KEY")
        print(f"[claude_api_panel] Key loaded via dotenv: {'yes' if key else 'no - key not in file or empty'}")
        return key
    except ImportError:
        # Minimal manual fallback if python-dotenv isn't importable inside
        # Max's embedded Python for some reason - just parse KEY=VALUE.
        print("[claude_api_panel] python-dotenv not importable, using manual parse fallback")
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return None


def _call_anthropic_api(messages, api_key):
    """
    Make one call to the Anthropic Messages API with our tool definitions.
    Returns the parsed JSON response. Raises on HTTP/network errors with
    a message suitable for showing directly in the panel.
    """
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": messages,
        "tools": TOOL_DEFINITIONS,
    }
    request = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API error {e.code}: {error_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error reaching Anthropic API: {e}") from e


def _execute_tool(tool_name, tool_input):
    """
    Execute a tool call by name, reusing max_ai_listener's action functions
    directly (in-process, no socket). Wrapped in mxstoken() for the same
    reason the listener wraps its handlers - we may be running on a
    different thread than Max's main thread if this panel's button click
    handler isn't on the main thread (Qt slots from a button click ARE on
    the main thread in this setup, but we wrap defensively since this
    function could be called from elsewhere later).
    """
    handler = max_ai_listener.ACTIONS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool '{tool_name}'"}
    try:
        with pymxs.mxstoken():
            return handler(tool_input)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _run_agent_loop(user_prompt, api_key, on_status_update):
    """
    Send the user's prompt to Claude, execute any tool calls it requests,
    and keep looping (sending tool results back) until Claude responds
    with plain text instead of a tool_use block. Returns the final text
    response. on_status_update(str) is called with brief progress notes
    so the panel can show what's happening rather than appearing frozen.
    """
    messages = [{"role": "user", "content": user_prompt}]

    # Cap iterations defensively - a runaway tool-call loop would otherwise
    # keep calling the paid API indefinitely on a bug.
    for _ in range(10):
        on_status_update("Asking Claude...")
        response = _call_anthropic_api(messages, api_key)

        if "error" in response:
            return f"API error: {response['error'].get('message', response['error'])}"

        content_blocks = response.get("content", [])
        messages.append({"role": "assistant", "content": content_blocks})

        text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
        tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]

        if not tool_use_blocks:
            return "\n".join(text_parts) if text_parts else "(no response text)"

        tool_results = []
        for block in tool_use_blocks:
            on_status_update(f"Running {block['name']}...")
            result = _execute_tool(block["name"], block.get("input", {}))
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(result),
            })
        messages.append({"role": "user", "content": tool_results})

    return "Stopped after 10 tool-call rounds without a final answer - this may indicate a loop."


class ClaudeApiPanel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Claude (Direct API)")
        self.setMinimumWidth(420)
        self.setMinimumHeight(420)
        self.api_key = _load_api_key()
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        if not self.api_key:
            warning = QtWidgets.QLabel(
                "No API key found. Create a .env file next to this script "
                "containing:\nANTHROPIC_API_KEY=your-key-here"
            )
            warning.setStyleSheet("color: #d9534f;")
            warning.setWordWrap(True)
            layout.addWidget(warning)

        cost_notice = QtWidgets.QLabel(
            "Uses pay-per-token API billing (separate from any Claude "
            "subscription). Light use is typically pennies per request."
        )
        cost_notice.setStyleSheet("color: gray; font-size: 10px;")
        cost_notice.setWordWrap(True)
        layout.addWidget(cost_notice)

        self.response_view = QtWidgets.QPlainTextEdit()
        self.response_view.setReadOnly(True)
        self.response_view.setPlaceholderText("Claude's response will appear here...")
        layout.addWidget(self.response_view, stretch=1)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: #5bc0de; font-size: 10px;")
        layout.addWidget(self.status_label)

        self.prompt_box = QtWidgets.QPlainTextEdit()
        self.prompt_box.setPlaceholderText("e.g. apply the brick material to the selected objects")
        self.prompt_box.setFixedHeight(70)
        layout.addWidget(self.prompt_box)

        self.send_button = QtWidgets.QPushButton("Ask Claude")
        self.send_button.clicked.connect(self._on_ask_clicked)
        self.send_button.setEnabled(self.api_key is not None)
        layout.addWidget(self.send_button)

        send_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Return"), self)
        send_shortcut.activated.connect(self._on_ask_clicked)

    def _set_status(self, text):
        self.status_label.setText(text)
        QtWidgets.QApplication.processEvents()  # keep UI responsive during the loop

    def _on_ask_clicked(self):
        prompt_text = self.prompt_box.toPlainText().strip()
        if not prompt_text:
            return
        if not self.api_key:
            QtWidgets.QMessageBox.warning(self, "No API key", "Set up your .env file first.")
            return

        self.send_button.setEnabled(False)
        self.response_view.setPlainText("")
        try:
            result_text = _run_agent_loop(prompt_text, self.api_key, self._set_status)
            self.response_view.setPlainText(result_text)
        except Exception as e:
            self.response_view.setPlainText(f"Error: {type(e).__name__}: {e}")
        finally:
            self._set_status("")
            self.send_button.setEnabled(True)
            self.prompt_box.clear()


_dock_widget_instance = None


def show_panel():
    """
    Show the Claude panel docked into 3ds Max's main window, the same way
    Scene Explorer and other native Max panels dock. Uses qtmax to get
    Max's actual QMainWindow and Qt's QDockWidget mechanism - this is the
    documented, supported way to dock a custom panel into Max's UI.

    Calling this again (e.g. via the toolbar button) brings the existing
    dock widget to the front rather than creating a duplicate.
    """
    global _dock_widget_instance

    if _dock_widget_instance is not None:
        _dock_widget_instance.show()
        _dock_widget_instance.raise_()
        return

    import qtmax
    main_window = qtmax.GetQMaxMainWindow()

    content = ClaudeApiPanel()

    dock_widget = QtWidgets.QDockWidget("Claude (Direct API)", main_window)
    dock_widget.setObjectName("ClaudeApiPanelDock")  # needed for Qt to track/restore dock state
    dock_widget.setWidget(content)
    dock_widget.setAllowedAreas(
        QtCore.Qt.LeftDockWidgetArea
        | QtCore.Qt.RightDockWidgetArea
        | QtCore.Qt.BottomDockWidgetArea
    )

    main_window.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock_widget)
    # Docked (not floating) by default - matches how Scene Explorer and
    # other native panels typically first appear. The user can still drag
    # it to float or to a different dock area afterward, same as any
    # other dockable panel in Max.
    dock_widget.setFloating(False)
    dock_widget.show()

    _dock_widget_instance = dock_widget


if __name__ == "__main__":
    show_panel()
