"""
3ds Max AI Bridge - Claude Prompt Panel
==========================================

A small dockable panel inside 3ds Max with a text box and a "Send to
Claude" button. Typing a prompt and clicking the button (or pressing
Ctrl+Enter) builds a claude:// deep link and opens it, which launches (or
focuses) Claude Desktop with your prompt pre-filled in a new chat.

This does NOT call the Anthropic API directly - it stays on your existing
Claude subscription, no separate API billing. The tradeoff: you do still
need to switch to the Claude Desktop window and press Enter there to
actually send the prompt - this panel saves you from typing the prompt
twice and alt-tabbing to find an empty chat box, but it can't auto-send
for you (Claude Desktop's claude:// link intentionally requires a manual
review-and-send step; see https://support.claude.com/en/articles/14729294).

How to run it inside Max:
    1. Open 3ds Max.
    2. Scripting > Run Script... and select this file.
    3. A small floating panel appears with a text box and a "Send to
       Claude" button.
    4. Type your prompt (e.g. "apply brick material to selected objects")
       and click the button, or press Ctrl+Enter.
    5. Claude Desktop opens/focuses with a new chat and your prompt
       pre-filled. Review it and press Enter there to actually send it.

To make this auto-start with 3ds Max alongside the listener, see the
"Auto-starting both" section in this plugin's README.
"""

import urllib.parse

import pymxs

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    # Fallback for older 3ds Max versions (2022-2024) that ship PySide2
    # instead of PySide6. The two APIs are near-identical for the widgets
    # used here.
    from PySide2 import QtCore, QtGui, QtWidgets  # noqa: F401

rt = pymxs.runtime

# Claude Desktop's max prompt length for the q= deep-link parameter is
# documented at roughly 14,000 characters. We warn well before that, since
# a truncated prompt sent to Claude silently loses content rather than
# erroring.
PROMPT_CHAR_WARNING_THRESHOLD = 12000


def _get_max_main_window():
    """
    Get 3ds Max's main window as a Qt widget, so our panel can be parented
    to it (keeps it on top of Max, closes cleanly with Max, etc).
    """
    # MaxPlus is deprecated/removed in recent Max versions; the modern way
    # is via pymxs and an explicit Qt wrap of Max's window handle.
    top_level_widgets = QtWidgets.QApplication.topLevelWidgets()
    for widget in top_level_widgets:
        if widget.objectName() == "QmaxApplication" or "3ds Max" in widget.windowTitle():
            return widget
    return None


class ClaudePromptPanel(QtWidgets.QWidget):
    """A small floating panel: a multi-line prompt box and a send button."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Claude Prompt")
        self.setMinimumWidth(360)
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        label = QtWidgets.QLabel("Ask Claude to do something in this scene:")
        layout.addWidget(label)

        self.prompt_box = QtWidgets.QPlainTextEdit()
        self.prompt_box.setPlaceholderText(
            "e.g. apply the brick material to the selected objects"
        )
        self.prompt_box.setFixedHeight(80)
        layout.addWidget(self.prompt_box)

        self.char_count_label = QtWidgets.QLabel("")
        self.char_count_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.char_count_label)
        self.prompt_box.textChanged.connect(self._update_char_count)

        button_row = QtWidgets.QHBoxLayout()
        self.send_button = QtWidgets.QPushButton("Send to Claude")
        self.send_button.clicked.connect(self._on_send_clicked)
        button_row.addWidget(self.send_button)
        layout.addLayout(button_row)

        hint = QtWidgets.QLabel(
            "Opens Claude Desktop with this prompt pre-filled.\n"
            "Review it there and press Enter to actually send it."
        )
        hint.setStyleSheet("color: gray; font-size: 10px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Ctrl+Enter as a shortcut to send, since the text box itself
        # needs plain Enter for newlines in longer prompts.
        send_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Return"), self)
        send_shortcut.activated.connect(self._on_send_clicked)

    def _update_char_count(self):
        count = len(self.prompt_box.toPlainText())
        if count > PROMPT_CHAR_WARNING_THRESHOLD:
            self.char_count_label.setText(
                f"{count} characters - approaching Claude Desktop's ~14,000 "
                "character limit for pre-filled prompts; it may be truncated."
            )
            self.char_count_label.setStyleSheet("color: #d9822b; font-size: 10px;")
        else:
            self.char_count_label.setText(f"{count} characters")
            self.char_count_label.setStyleSheet("color: gray; font-size: 10px;")

    def _on_send_clicked(self):
        prompt_text = self.prompt_box.toPlainText().strip()
        if not prompt_text:
            QtWidgets.QMessageBox.information(
                self, "Nothing to send", "Type a prompt first."
            )
            return

        encoded_prompt = urllib.parse.quote(prompt_text)
        deep_link = f"claude://claude.ai/new?q={encoded_prompt}"

        # QDesktopServices.openUrl hands the URL to the OS, which routes
        # claude:// to Claude Desktop the same way it would route mailto:
        # to an email client. This does NOT call any API directly - it's
        # the same mechanism as clicking a claude:// link in a browser.
        opened = QtGui.QDesktopServices.openUrl(QtCore.QUrl(deep_link))
        if not opened:
            QtWidgets.QMessageBox.warning(
                self,
                "Could not open Claude Desktop",
                "The claude:// link could not be opened. Make sure Claude "
                "Desktop is installed - it registers this URL scheme with "
                "Windows on install.",
            )
            return

        # Clear the box after a successful send so the panel is ready for
        # the next prompt, rather than leaving stale text behind.
        self.prompt_box.clear()


_panel_instance = None


def show_panel():
    """Create (if needed) and show the Claude prompt panel."""
    global _panel_instance
    if _panel_instance is not None:
        _panel_instance.close()

    max_window = _get_max_main_window()
    _panel_instance = ClaudePromptPanel(parent=max_window)
    _panel_instance.setWindowFlags(QtCore.Qt.Tool)  # floats above Max, closes with it
    _panel_instance.show()


if __name__ == "__main__":
    show_panel()
