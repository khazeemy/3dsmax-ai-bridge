"""
3ds Max AI Bridge - control panel
=================================

A dockable panel inside 3ds Max for the bridge listener:

    - shows whether the listener is running, with a Start/Stop button
    - lets the user choose which tools Claude may use (saved between
      sessions; a turned-off tool is refused by the listener itself)
    - shows a live log of the tool calls Claude makes

Opened from the "AI Bridge" menu (installed by Step1_Install_in_3dsMax.ms),
the "AI Bridge" macroScript category in Customize User Interface, or:

    import max_ai_panel; max_ai_panel.show_panel()

Needs max_ai_listener.py in the same folder. 3ds Max 2025+ (PySide6).
"""

import time

from PySide6 import QtCore, QtGui, QtWidgets
import shiboken6

import max_ai_listener as listener

PANEL_TITLE = "AI Bridge"
DOCK_OBJECT_NAME = "AIBridgePanelDock"
MAX_LOG_ENTRIES = 200
STATUS_REFRESH_MS = 1000

# risk -> (tag text, tag colour)
RISK_TAGS = {
    "read": ("Read only", "#5a9e6f"),
    "modify": ("Edits scene", "#c98a2b"),
    "full": ("Full access", "#c8513f"),
}

LOG_STATUS = {
    # status -> (label, colour or None for the default text colour)
    "ok": ("OK", None),
    "error": ("ERROR", "#c8513f"),
    "blocked": ("BLOCKED", "#c98a2b"),
}


def _max_main_window():
    try:
        import qtmax
        return qtmax.GetQMaxMainWindow()
    except Exception:
        return None


class _EventRelay(QtCore.QObject):
    """Carries listener events (worker thread) to the panel (Qt main thread)."""
    event = QtCore.Signal(object)


class AIBridgePanel(QtWidgets.QDockWidget):

    def __init__(self, parent=None):
        super().__init__(PANEL_TITLE, parent)
        self.setObjectName(DOCK_OBJECT_NAME)
        self.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea)
        self._checkboxes = {}

        self._relay = _EventRelay()
        self._relay.event.connect(self._add_log_entry)
        relay = self._relay
        self._observer = lambda event: relay.event.emit(event)
        listener.add_observer(self._observer)
        observer = self._observer
        self.destroyed.connect(lambda *_: listener.remove_observer(observer))

        body = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addLayout(self._build_status_row())
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self._build_tools_box())
        layout.addWidget(scroll, 3)
        layout.addWidget(self._build_log_box(), 1)
        self.setWidget(body)

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start(STATUS_REFRESH_MS)
        self._refresh_status()

    # -- status -------------------------------------------------------------

    def _build_status_row(self):
        row = QtWidgets.QHBoxLayout()
        self._status_dot = QtWidgets.QLabel()
        self._status_dot.setFixedSize(10, 10)
        self._status_label = QtWidgets.QLabel()
        self._toggle_button = QtWidgets.QPushButton()
        self._toggle_button.setFixedWidth(70)
        self._toggle_button.clicked.connect(self._toggle_listener)
        row.addWidget(self._status_dot)
        row.addWidget(self._status_label, 1)
        row.addWidget(self._toggle_button)
        return row

    def _refresh_status(self):
        port = listener.listening_port()
        running = port is not None
        colour = "#5a9e6f" if running else "#8a8a8a"
        self._status_dot.setStyleSheet("background:%s; border-radius:5px;" % colour)
        if running:
            self._status_label.setText("Bridge on - 127.0.0.1:%d" % port)
            self._status_label.setToolTip("Claude Desktop can reach this 3ds Max session.")
        else:
            self._status_label.setText("Bridge stopped")
            self._status_label.setToolTip("Claude cannot reach this 3ds Max session.")
        self._toggle_button.setText("Stop" if running else "Start")
        self._sync_checkboxes()

    def _sync_checkboxes(self):
        # Keep the checkboxes truthful if states change outside this panel.
        changed = False
        for name, enabled in listener.get_tool_states().items():
            checkbox = self._checkboxes.get(name)
            if checkbox is not None and checkbox.isChecked() != enabled:
                checkbox.blockSignals(True)
                checkbox.setChecked(enabled)
                checkbox.blockSignals(False)
                changed = True
        if changed:
            self._update_summary()

    def _toggle_listener(self):
        if listener.is_running():
            listener.stop_listener(wait_seconds=2.0)
        else:
            try:
                listener.start_listener(port=listener.PORT)
            except OSError as e:
                QtWidgets.QMessageBox.warning(
                    self, PANEL_TITLE,
                    "Could not start the bridge on port %d.\n\nAnother 3ds Max window "
                    "probably has it - close the other windows and try again.\n\n(%s)"
                    % (listener.PORT, e))
        self._refresh_status()

    # -- tools --------------------------------------------------------------

    def _build_tools_box(self):
        box = QtWidgets.QGroupBox("Tools Claude can use")
        # Never squeeze the tool list (wrapped descriptions get clipped);
        # the activity log below takes whatever height is left.
        box.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Minimum)
        layout = QtWidgets.QVBoxLayout(box)
        layout.setSpacing(4)
        layout.setSizeConstraint(QtWidgets.QLayout.SetMinimumSize)

        states = listener.get_tool_states()
        categories = {}
        for name, meta in listener.TOOLS.items():
            categories.setdefault(meta["category"], []).append((name, meta))

        for category, tools in categories.items():
            heading = QtWidgets.QLabel(category.upper())
            heading.setStyleSheet("color: gray; font-size: 10px; font-weight: bold; margin-top: 4px;")
            layout.addWidget(heading)
            for name, meta in tools:
                layout.addLayout(self._build_tool_row(name, meta, states.get(name, False)))

        presets = QtWidgets.QHBoxLayout()
        for text, tip, chooser in (
                ("All", "Turn every tool on", lambda meta: True),
                ("Read only", "Only tools that look at the scene without changing it",
                 lambda meta: meta["risk"] == "read"),
                ("None", "Turn every tool off", lambda meta: False)):
            button = QtWidgets.QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, c=chooser: self._apply_preset(c))
            presets.addWidget(button)
        layout.addSpacing(4)
        layout.addLayout(presets)

        self._summary = QtWidgets.QLabel()
        self._summary.setStyleSheet("color: gray; font-size: 10px;")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)
        self._update_summary()
        return box

    def _build_tool_row(self, name, meta, enabled):
        row = QtWidgets.QVBoxLayout()
        row.setSpacing(0)
        top = QtWidgets.QHBoxLayout()

        checkbox = QtWidgets.QCheckBox(meta["label"])
        checkbox.setChecked(enabled)
        checkbox.setToolTip("Tool name: %s" % name)
        checkbox.toggled.connect(lambda checked, n=name: self._save_states({n: checked}))
        self._checkboxes[name] = checkbox

        tag_text, tag_colour = RISK_TAGS.get(meta["risk"], (meta["risk"], "#8a8a8a"))
        tag = QtWidgets.QLabel(tag_text)
        tag.setStyleSheet("color: %s; border: 1px solid %s; border-radius: 3px; "
                          "padding: 0 4px; font-size: 10px;" % (tag_colour, tag_colour))

        top.addWidget(checkbox, 1)
        top.addWidget(tag)
        row.addLayout(top)

        description = QtWidgets.QLabel(meta["description"])
        description.setWordWrap(True)
        description.setStyleSheet("color: gray; font-size: 10px; margin-left: 20px;")
        row.addWidget(description)
        return row

    def _apply_preset(self, chooser):
        states = {name: chooser(meta) for name, meta in listener.TOOLS.items()}
        for name, checkbox in self._checkboxes.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(states[name])
            checkbox.blockSignals(False)
        self._save_states(states)

    def _save_states(self, states):
        try:
            listener.set_tool_states(states)
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.warning(
                self, PANEL_TITLE,
                "The change applies to this session but could not be saved:\n\n%s" % e)
        self._update_summary()

    def _update_summary(self):
        states = listener.get_tool_states()
        on = sum(1 for enabled in states.values() if enabled)
        text = "%d of %d tools on. Changes apply immediately." % (on, len(states))
        if states.get("execute_maxscript"):
            text += " While \"Run MAXScript\" is on, Claude can still do anything the other tools do."
        self._summary.setText(text)

    # -- activity log -------------------------------------------------------

    def _build_log_box(self):
        box = QtWidgets.QGroupBox("Activity")
        layout = QtWidgets.QVBoxLayout(box)
        self._log = QtWidgets.QListWidget()
        self._log.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self._log.setMinimumHeight(80)
        layout.addWidget(self._log)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self._log.clear)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        row.addWidget(clear)
        layout.addLayout(row)
        return box

    def _add_log_entry(self, event):
        label, colour = LOG_STATUS.get(event["status"], (event["status"].upper(), None))
        stamp = time.strftime("%H:%M:%S", time.localtime(event["time"]))
        item = QtWidgets.QListWidgetItem("%s  %-7s  %s" % (stamp, label, event["action"]))
        if colour:
            item.setForeground(QtGui.QColor(colour))
        if event["status"] == "blocked":
            item.setToolTip("Refused: this tool is turned off above.")
        elif event.get("detail"):
            item.setToolTip(event["detail"])
        self._log.addItem(item)
        while self._log.count() > MAX_LOG_ENTRIES:
            self._log.takeItem(0)
        self._log.scrollToBottom()


_panel = None


def show_panel():
    """Show the AI Bridge panel, creating it the first time."""
    global _panel
    if _panel is not None and shiboken6.isValid(_panel):
        _panel.show()
        _panel.raise_()
        return _panel

    main_window = _max_main_window()
    _panel = AIBridgePanel(main_window)
    if isinstance(main_window, QtWidgets.QMainWindow):
        main_window.addDockWidget(QtCore.Qt.RightDockWidgetArea, _panel)
        _panel.setFloating(True)
    _panel.resize(360, 680)
    _panel.show()
    return _panel


if __name__ == "__main__":
    show_panel()
