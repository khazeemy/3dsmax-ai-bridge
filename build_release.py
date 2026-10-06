"""
3ds Max AI Bridge - release builder
===================================

Builds the shareable release package from this source folder:

    dist/3dsmax-ai-bridge-<version>.zip
        READ_ME_FIRST.txt
        Step1_Install_in_3dsMax.ms              drag onto a 3ds Max viewport
        Step2_3dsmax-ai-bridge-<version>.mcpb   double-click to install in Claude Desktop
        Uninstall_from_3dsMax.ms
    dist/SHA256SUMS.txt
    dist/RELEASE_NOTES.md

Scope: only the Claude Desktop <-> 3ds Max bridge (MCP server + in-Max
listener). The in-Max Claude panels (claude_api_panel.py,
claude_prompt_panel.py), .env, server/lib and test files are never packaged -
files are taken from an explicit allowlist, and every output is scanned for
API keys before the build is allowed to finish.

The .mcpb uses the MCPB "uv" server type (manifest 0.4): Claude Desktop
installs Python and the `mcp` package itself, so users don't need Python.

Usage:
    python build_release.py                  # uses VERSION below
    python build_release.py --version 0.2.1

Plain Python 3.10+, standard library only, no downloads.
"""

import argparse
import ast
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime

VERSION = "0.3.0"
EXT_NAME = "3dsmax-ai-bridge"
DISPLAY_NAME = "3ds Max AI Bridge"
AUTHOR = {"name": "Gamer Architect Labs", "url": "https://gamerarchlabs.com"}
MCP_REQUIREMENT = "mcp>=1.28,<2"   # 1.28 is the version the bridge was proven on
MIN_MAX_VERSION = 27000            # maxVersion() of 3ds Max 2025

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_SRC = os.path.join(ROOT, "server", "max_bridge_server.py")
LISTENER_SRC = os.path.join(ROOT, "server", "max_ai_listener.py")
PANEL_SRC = os.path.join(ROOT, "server", "max_ai_panel.py")
DIST = os.path.join(ROOT, "dist")

SECRET_PATTERNS = [
    re.compile(rb"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(rb"ANTHROPIC_API_KEY\s*=\s*[^\s\"']"),
]


class BuildError(Exception):
    pass


# ---------------------------------------------------------------------------
# Source inspection
# ---------------------------------------------------------------------------

def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _is_mcp_tool_decorator(dec):
    target = dec.func if isinstance(dec, ast.Call) else dec
    return isinstance(target, ast.Attribute) and target.attr == "tool"


def server_tools(path):
    """Tool names + first docstring paragraph, straight from the server code."""
    tree = ast.parse(read_text(path))
    tools = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and any(_is_mcp_tool_decorator(d) for d in node.decorator_list):
            doc = ast.get_docstring(node) or ""
            first_paragraph = " ".join(doc.strip().split("\n\n")[0].split())
            tools.append({"name": node.name, "description": first_paragraph})
    if not tools:
        raise BuildError("No @mcp.tool() functions found in max_bridge_server.py")
    return tools


def module_constant(path, name):
    tree = ast.parse(read_text(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise BuildError("Could not find %s in %s" % (name, os.path.basename(path)))


def listener_actions(path):
    tree = ast.parse(read_text(path))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(isinstance(t, ast.Name) and t.id == "ACTIONS" for t in node.targets)):
            return [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    raise BuildError("Could not find the ACTIONS dict in max_ai_listener.py")


def check_sources():
    for path in (SERVER_SRC, LISTENER_SRC, PANEL_SRC):
        if not os.path.isfile(path):
            raise BuildError("Missing source file: " + path)
        compile(read_text(path), path, "exec")

    tools = server_tools(SERVER_SRC)
    actions = listener_actions(LISTENER_SRC)
    tool_names = [t["name"] for t in tools]
    if sorted(tool_names) != sorted(actions):
        raise BuildError("Server tools %s do not match listener ACTIONS %s" % (tool_names, actions))
    panel_tools = module_constant(LISTENER_SRC, "TOOLS")
    if sorted(panel_tools) != sorted(actions):
        raise BuildError("Listener TOOLS (panel metadata) %s do not match ACTIONS %s"
                         % (sorted(panel_tools), actions))

    server_host = module_constant(SERVER_SRC, "MAX_HOST")
    listener_host = module_constant(LISTENER_SRC, "HOST")
    if server_host != "127.0.0.1" or listener_host != "127.0.0.1":
        raise BuildError("Refusing to build: the bridge must only listen/connect on 127.0.0.1")

    port = module_constant(SERVER_SRC, "MAX_PORT")
    if port != module_constant(LISTENER_SRC, "PORT"):
        raise BuildError("MAX_PORT in the server and PORT in the listener differ")
    return tools, port


# ---------------------------------------------------------------------------
# Generated content
# ---------------------------------------------------------------------------

def manifest(version, tools):
    return {
        "manifest_version": "0.4",
        "name": EXT_NAME,
        "display_name": DISPLAY_NAME,
        "version": version,
        "description": "Lets Claude model, lay out, optimise and frame cameras in your live 3ds Max scene.",
        "long_description": (
            "Connects Claude Desktop to a running 3ds Max session over a local socket (127.0.0.1 only). "
            "Claude can read the scene and current selection, list and apply materials, and run MAXScript "
            "directly against the open scene.\n\n"
            "Requires Windows and 3ds Max 2025 or newer, with the bridge listener installed inside 3ds Max "
            "(run Step1_Install_in_3dsMax.ms from the release package once). In the AI Bridge panel inside "
            "3ds Max you choose which tools Claude may use. Each change is one undo step in 3ds Max; "
            "execute_maxscript can change or delete anything in the open scene, so save your work first."
        ),
        "author": AUTHOR,
        "license": "MIT",
        "keywords": ["3ds max", "3dsmax", "maxscript", "archviz", "cgi", "materials", "pymxs"],
        "server": {
            "type": "uv",
            "entry_point": "server/max_bridge_server.py",
            "mcp_config": {
                "command": "uv",
                "args": ["run", "--directory", "${__dirname}", "server/max_bridge_server.py"],
            },
        },
        "tools": tools,
        "tools_generated": False,
        "compatibility": {
            "platforms": ["win32"],
            "runtimes": {"python": ">=3.10"},
        },
    }


def pyproject(version):
    return (
        "[project]\n"
        'name = "%s"\n'
        'version = "%s"\n'
        'description = "%s - MCP server for Claude Desktop"\n'
        'requires-python = ">=3.10"\n'
        'dependencies = ["%s"]\n'
    ) % (EXT_NAME, version, DISPLAY_NAME, MCP_REQUIREMENT)


EXTENSION_README = """# 3ds Max AI Bridge __VERSION__

MCP server that connects Claude Desktop to a live 3ds Max scene.

This extension is only half of the bridge. The other half is a small listener
that runs inside 3ds Max - install it with `Step1_Install_in_3dsMax.ms` from
the release package (see READ_ME_FIRST.txt there).

Tools: __TOOL_LIST__

The listener accepts connections from this computer only (127.0.0.1:__PORT__).
"""


BOOT_PY = '''"""3ds Max AI Bridge __VERSION__ - boot script.

Run by AIBridge_Startup.ms every time 3ds Max starts, and once by the
installer. Starts max_ai_listener from this folder, unless a listener is
already running in this 3ds Max session or another program (usually a second
3ds Max window) already owns the port.
"""
import importlib
import os
import socket
import sys

import pymxs

AIBRIDGE_PORT = __PORT__
AIBRIDGE_BOOT_STATUS = "not_run"


def _aibridge_boot():
    rt = pymxs.runtime
    bridge_dir = os.path.join(str(rt.getDir(rt.Name("userScripts"))), "AIBridge")
    # On the path even when we don't start a listener, so the AI Bridge
    # panel (menu / macroScript) can always be imported.
    while bridge_dir in sys.path:
        sys.path.remove(bridge_dir)
    sys.path.insert(0, bridge_dir)
    importlib.invalidate_caches()

    existing = sys.modules.get("max_ai_listener")
    if existing is not None:
        thread = getattr(existing, "_server_thread", None)
        if thread is not None and thread.is_alive():
            print("[AIBridge] Listener already running in this 3ds Max session.")
            return "already_running"

    try:
        with socket.create_connection(("127.0.0.1", AIBRIDGE_PORT), timeout=0.5):
            pass
    except OSError:
        pass
    else:
        print("[AIBridge] Port %d is already in use (another 3ds Max window probably has the bridge). "
              "Not starting a second listener here." % AIBRIDGE_PORT)
        return "port_in_use"

    import max_ai_listener
    max_ai_listener.start_listener(port=AIBRIDGE_PORT)
    return "started"


try:
    AIBRIDGE_BOOT_STATUS = _aibridge_boot()
except Exception as exc:
    AIBRIDGE_BOOT_STATUS = "error: %s: %s" % (type(exc).__name__, exc)
    print("[AIBridge] Could not start the listener - " + AIBRIDGE_BOOT_STATUS)
'''


STARTUP_SHIM_MS = """-- AIBridge_Startup.ms
-- 3ds Max AI Bridge __VERSION__ - starts the listener Claude Desktop connects to.
-- Installed by Step1_Install_in_3dsMax.ms. To stop it starting with 3ds Max,
-- run Uninstall_from_3dsMax.ms (or delete this file).

__MACRO__
-- Adds an "AI Bridge" menu to the main menu bar (3ds Max 2025+ menu system).
fn AIBridge_registerMenu =
(
	local menuMgr = callbacks.notificationParam()
	local aiMenu = menuMgr.mainMenuBar.CreateSubMenu "6f4b1c2e-8d3a-4e7b-9c1f-2a5d8e0b3c71" "AI Bridge"
	aiMenu.CreateAction "b2e7d9a4-3c6f-4a18-8e5b-7d1c0f9a2e63" 647394 "AIBridge_Panel`AI Bridge"
)
callbacks.removeScripts id:#AIBridgeMenu
callbacks.addScript #cuiRegisterMenus AIBridge_registerMenu id:#AIBridgeMenu

(
	local bootPy = (getDir #userScripts) + "\\\\AIBridge\\\\aibridge_boot.py"
	if doesFileExist bootPy then
		python.ExecuteFile bootPy
	else
		format "[AIBridge] Boot script not found: %\\n" bootPy
)
"""


PANEL_MACRO_MS = """-- Opens the AI Bridge panel (tools Claude may use, start/stop, activity log).
-- Also available in Customize User Interface under the "AI Bridge" category.
macroScript AIBridge_Panel category:"AI Bridge" buttonText:"AI Bridge" tooltip:"AI Bridge - choose the tools Claude can use"
(
	python.execute "import max_ai_panel; max_ai_panel.show_panel()"
)
"""


INSTALLER_PY = '''# 3ds Max AI Bridge __VERSION__ - installer (runs inside 3ds Max)
import base64
import glob
import os
import shutil
import sys
import traceback

import pymxs

rt = pymxs.runtime
VERSION = "__VERSION__"
PORT = __PORT__
TITLE = "3ds Max AI Bridge " + VERSION
FILES = {
    "max_ai_listener.py": "__LISTENER_B64__",
    "max_ai_panel.py": "__PANEL_B64__",
    "aibridge_boot.py": "__BOOT_B64__",
}
SHIM_NAME = "AIBridge_Startup.ms"
SHIM_B64 = "__SHIM_B64__"
MACRO_B64 = "__MACRO_B64__"


def _max_dir(name):
    return str(rt.getDir(rt.Name(name)))


def _same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _message(text):
    print("[AIBridge] " + text.replace("\\n", "\\n[AIBridge] "))
    try:
        rt.messageBox(text, title=TITLE)
    except TypeError:
        rt.messageBox(text)


def _stop(module):
    try:
        module.stop_listener()
        thread = getattr(module, "_server_thread", None)
        if thread is not None:
            thread.join(timeout=3.0)
    except Exception:
        pass


def _install():
    bridge_dir = os.path.join(_max_dir("userScripts"), "AIBridge")
    startup_dir = _max_dir("userStartupScripts")
    os.makedirs(bridge_dir, exist_ok=True)
    os.makedirs(startup_dir, exist_ok=True)
    notes = []

    # Re-installing / upgrading: close the panel (it holds the old listener module)...
    panel = sys.modules.get("max_ai_panel")
    if panel is not None:
        try:
            if panel._panel is not None:
                panel._panel.close()
                panel._panel.deleteLater()
        except Exception:
            pass
        del sys.modules["max_ai_panel"]

    # ...and stop our own copy of the listener if it is running in this session.
    running = sys.modules.get("max_ai_listener")
    if running is not None:
        running_file = getattr(running, "__file__", "") or ""
        if running_file and _same_path(os.path.dirname(running_file), bridge_dir):
            _stop(running)
            del sys.modules["max_ai_listener"]
        else:
            notes.append("A bridge listener from another folder is already running in this session:\\n  "
                         + running_file)

    shutil.rmtree(os.path.join(bridge_dir, "__pycache__"), ignore_errors=True)
    for name, payload in FILES.items():
        with open(os.path.join(bridge_dir, name), "wb") as f:
            f.write(base64.b64decode(payload))
    with open(os.path.join(bridge_dir, "VERSION.txt"), "w", encoding="utf-8") as f:
        f.write(VERSION + "\\n")
    shim_path = os.path.join(startup_dir, SHIM_NAME)
    with open(shim_path, "wb") as f:
        f.write(base64.b64decode(SHIM_B64))
    # Define the panel macroScript now; the menu itself appears from the next start.
    rt.execute(base64.b64decode(MACRO_B64).decode("utf-8"))

    # Other startup scripts that also start a listener would fight over the port.
    others = []
    for folder in {startup_dir, _max_dir("startupScripts")}:
        for path in sorted(glob.glob(os.path.join(folder, "*.ms")) + glob.glob(os.path.join(folder, "*.py"))):
            if _same_path(path, shim_path):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    if "max_ai_listener" in f.read():
                        others.append(path)
            except OSError:
                pass
    if others:
        notes.append("These other startup scripts also start a bridge listener and may conflict. "
                     "Remove them unless you know you need them:\\n  " + "\\n  ".join(others))

    boot_path = os.path.join(bridge_dir, "aibridge_boot.py")
    namespace = {"__name__": "aibridge_boot", "__file__": boot_path}
    with open(boot_path, encoding="utf-8") as f:
        exec(compile(f.read(), boot_path, "exec"), namespace)
    return bridge_dir, shim_path, namespace.get("AIBRIDGE_BOOT_STATUS", "unknown"), notes


def _main():
    try:
        bridge_dir, shim_path, status, notes = _install()
    except Exception:
        _message("Installation failed:\\n\\n" + traceback.format_exc(limit=4))
        return

    where = "\\n\\nInstalled to:\\n  " + bridge_dir + "\\n  " + shim_path
    try:
        import max_ai_panel
        max_ai_panel.show_panel()
    except Exception:
        notes.append("Could not open the AI Bridge panel:\\n" + traceback.format_exc(limit=2))
    if status == "started":
        text = ("Installed and running.\\n\\nThe listener is live on 127.0.0.1:%d and will start "
                "automatically whenever 3ds Max opens.\\n\\nUse the AI Bridge panel to choose which "
                "tools Claude can use. Reopen it any time from the AI Bridge menu (after restarting "
                "3ds Max once) or the \\"AI Bridge\\" category in Customize User Interface."
                "\\n\\nNext: install the .mcpb file in Claude Desktop (Step 2 in READ_ME_FIRST.txt)." % PORT)
    elif status == "port_in_use":
        text = ("Installed, but NOT started: port %d is already in use - usually another open "
                "3ds Max window already runs the bridge.\\n\\nClose all 3ds Max windows, then "
                "open just one." % PORT)
    elif status == "already_running":
        text = ("Installed, but a bridge listener from another folder is still running in this "
                "session.\\n\\nRestart 3ds Max to switch to the new version.")
    else:
        text = "Installed, but the listener could not start:\\n  " + str(status)
    if notes:
        text += "\\n\\nNOTE:\\n" + "\\n\\n".join(notes)
    _message(text + where)


_main()
'''


UNINSTALLER_PY = '''# 3ds Max AI Bridge __VERSION__ - uninstaller (runs inside 3ds Max)
import os
import shutil
import sys
import traceback

import pymxs

rt = pymxs.runtime
TITLE = "3ds Max AI Bridge - uninstall"


def _max_dir(name):
    return str(rt.getDir(rt.Name(name)))


def _message(text):
    print("[AIBridge] " + text.replace("\\n", "\\n[AIBridge] "))
    try:
        rt.messageBox(text, title=TITLE)
    except TypeError:
        rt.messageBox(text)


def _main():
    bridge_dir = os.path.join(_max_dir("userScripts"), "AIBridge")
    shim_path = os.path.join(_max_dir("userStartupScripts"), "AIBridge_Startup.ms")
    macro_path = os.path.join(_max_dir("userMacros"), "AI Bridge-AIBridge_Panel.mcr")
    settings_path = os.path.join(_max_dir("plugcfg"), "AIBridge_settings.json")
    done = []

    panel = sys.modules.get("max_ai_panel")
    if panel is not None:
        try:
            if panel._panel is not None:
                panel._panel.close()
                panel._panel.deleteLater()
        except Exception:
            pass
        del sys.modules["max_ai_panel"]
        done.append("Closed the AI Bridge panel")

    rt.execute("callbacks.removeScripts id:#AIBridgeMenu")

    running = sys.modules.get("max_ai_listener")
    running_file = getattr(running, "__file__", "") or "" if running is not None else ""
    if running_file and os.path.normcase(os.path.dirname(os.path.abspath(running_file))) == os.path.normcase(os.path.abspath(bridge_dir)):
        try:
            running.stop_listener()
            thread = getattr(running, "_server_thread", None)
            if thread is not None:
                thread.join(timeout=3.0)
        except Exception:
            pass
        del sys.modules["max_ai_listener"]
        done.append("Stopped the running listener")

    for path in (shim_path, macro_path, settings_path):
        if os.path.isfile(path):
            os.remove(path)
            done.append("Removed " + path)
    if os.path.isdir(bridge_dir):
        shutil.rmtree(bridge_dir)
        done.append("Removed " + bridge_dir)
    while bridge_dir in sys.path:
        sys.path.remove(bridge_dir)

    if not done:
        _message("Nothing to remove - the bridge is not installed for this 3ds Max user.")
    else:
        _message("Uninstalled from 3ds Max:\\n  " + "\\n  ".join(done)
                 + "\\n\\nThe AI Bridge menu disappears the next time 3ds Max starts."
                 + "\\n\\nAlso remove the 3ds Max AI Bridge extension from Claude Desktop's "
                   "extension settings.")


try:
    _main()
except Exception:
    _message("Uninstall failed:\\n\\n" + traceback.format_exc(limit=4))
'''


MS_WRAPPER = """-- __FILENAME__
-- 3ds Max AI Bridge __VERSION__
--
-- __PURPOSE__
--
-- HOW TO USE: drag this file onto a 3ds Max viewport
--             (or Scripting > Run Script... and choose it).
--
-- The Python code below is base64-encoded only so this stays a single,
-- self-contained file; build_release.py generated it from readable source.
(
	if (maxVersion())[1] < __MIN_MAX__ then
	(
		messageBox "3ds Max AI Bridge needs 3ds Max 2025 or newer." title:"3ds Max AI Bridge"
	)
	else
	(
		local parts = #(
__CHUNKS__
		)
		local ss = stringStream ""
		for p in parts do format "%" p to:ss
		python.execute ("import base64; exec(compile(base64.b64decode('" + (ss as string) + "').decode('utf-8'), '__CODENAME__', 'exec'))")
	)
)
"""


READ_ME_FIRST = """3ds Max AI Bridge __VERSION__
============================

Lets Claude (in Claude Desktop) see and edit the scene you have open in
3ds Max. You ask in plain language; Claude reads your scene, selection and
materials, applies materials, and can run MAXScript for you.

Please read this whole page once - it takes 3 minutes.


BEFORE YOU START
----------------
- Windows PC with 3ds Max 2025 or newer (built and tested on 3ds Max 2026).
- Claude Desktop, updated to the latest version, signed in.
- SAVE YOUR SCENE and test on a copy. Claude can create, change and delete
  objects. Each change Claude makes is one undo step (Ctrl+Z in 3ds Max),
  but save anyway.
- Extract this whole zip to a normal folder first (don't run files from
  inside the zip preview).


STEP 1 - Install the listener in 3ds Max (once)
-----------------------------------------------
1. Open 3ds Max.
2. Drag "Step1_Install_in_3dsMax.ms" onto a viewport
   (or Scripting > Run Script... and choose it).
3. You should see "Installed and running", and the AI Bridge panel opens.
   From now on the bridge starts by itself every time 3ds Max opens.


CHOOSE WHAT CLAUDE CAN DO - the AI Bridge panel
-----------------------------------------------
Open it from the "AI Bridge" menu in 3ds Max's menu bar (appears after you
restart 3ds Max once). In the panel you can:
  - tick the tools Claude may use - "Read only" lets Claude look but not
    change anything. Changes apply immediately and are remembered.
  - turn the bridge off and on (Stop / Start)
  - watch every tool call Claude makes in the Activity list
"Run MAXScript" gives Claude full access: while it is on, Claude can do
anything the other tools do. Turn it off if you want real limits.


STEP 2 - Install the extension in Claude Desktop (once)
-------------------------------------------------------
1. Double-click "Step2___EXT_NAME__-__VERSION__.mcpb".
   Claude Desktop opens and shows what the extension does - click Install.
   (If double-clicking does nothing: open Claude Desktop's settings, go to
   Extensions, and install the .mcpb file from there.)
2. The first start can take a minute - Claude Desktop downloads Python and
   the MCP library for the extension in the background.
3. Start a NEW chat and check that "3ds Max AI Bridge" is switched on in the
   chat's tools/connectors menu.


STEP 3 - Try it
---------------
Keep 3ds Max open with a scene loaded, then ask Claude things like:
  - "What's in my 3ds Max scene right now?"
  - "What do I have selected?"
  - "List the materials in my scene."
  - "Apply the <material name> material to the selected objects."
  - "Model a simple dining table, 160 x 80 cm, 75 cm high."
  - "Put instances of the selected post every 2 m along the spline Fence_Path."
  - "Check my scene for performance problems." (then: "apply the fixes")
  - "Make an eye-level camera on the selected building, 16:9, and show me."
  - "Use C:\\refs\\chair.jpg as a front reference, 60 cm wide, and model it."
Claude Desktop may ask you to allow each tool the first time - read what it
is about to do before you allow it, especially for execute_maxscript.


GOOD TO KNOW
------------
- Only ONE 3ds Max window can hold the bridge at a time. If you open a
  second 3ds Max, it will not start another listener.
- The bridge only accepts connections from your own computer
  (127.0.0.1, port __PORT__). Nothing is opened to your network.
- Your scene data is sent to Claude when Claude calls a tool, like anything
  else you share in a chat.


TROUBLESHOOTING
---------------
"Could not reach the 3ds Max listener"
   -> Is 3ds Max open? Open Scripting > MAXScript Listener and look for
      "[max_ai_listener] Listening on 127.0.0.1:__PORT__". If it isn't there,
      run Step1_Install_in_3dsMax.ms again and read the message it shows.

"Port __PORT__ is already in use"
   -> Close every 3ds Max window, then open just one.

The extension shows an error / tools don't appear in Claude Desktop
   -> Update Claude Desktop, fully quit it (also from the system tray) and
      reopen it. Give the first start a minute or two. If it still fails,
      screenshot the error in the extension settings and report it (below).


UNINSTALL
---------
1. Drag "Uninstall_from_3dsMax.ms" onto a 3ds Max viewport.
2. Remove "3ds Max AI Bridge" from Claude Desktop's extension settings.


REPORTING A PROBLEM
-------------------
Contact __AUTHOR_NAME__ (__AUTHOR_URL__). For each problem or idea, include:
  1. 3ds Max version (Help > About) and renderer (e.g. V-Ray 7, Corona, Arnold)
  2. What you asked Claude (copy the message)
  3. What happened vs. what you expected
  4. Screenshots of any error - in Claude and in the MAXScript Listener
  5. Roughly how big the scene was (number of objects)
"""


RELEASE_NOTES = """# 3ds Max AI Bridge __VERSION__

Connects Claude Desktop to a live 3ds Max scene.

## New in 0.3.0
- AI Bridge panel inside 3ds Max (AI Bridge menu): choose which tools Claude may use, start/stop the bridge, and see a live activity log. Turned-off tools are refused by 3ds Max itself; the choice is remembered.
- Modeling tools: create objects and splines, move/rotate/scale, edit parameters, add modifiers, clone, delete.
- Model from image: `capture_viewport` lets Claude see the viewport; `create_reference_plane` sets up image references.
- `copy_along_spline`: copies/instances along drawn splines by count or spacing, with alignment and randomness.
- Scene optimiser: `analyze_scene` report, `optimize_scene` safe fixes (preview first).
- View finder: `create_camera_view` frames Physical cameras on the selection (eye-level shots use 2-point perspective).
- Every tool runs on 3ds Max's main thread (fixes freezes/crashes with UI or viewport work) and each change is one undo step.

## Tools
__TOOL_LIST__

## Downloads
- `__EXT_NAME__-__VERSION__-setup.exe` - **easiest**: run it, then click Install when Claude Desktop opens. Sets up every 3ds Max 2025+ for the current Windows user, no admin rights; uninstall from Windows Settings > Apps. (Unsigned: if Windows shows "Windows protected your PC", click More info > Run anyway.)
- `__EXT_NAME__-__VERSION__.zip` - manual install, contents below.

## What's in the zip
- `READ_ME_FIRST.txt` - setup, example prompts, troubleshooting, how to report problems
- `Step1_Install_in_3dsMax.ms` - installs and auto-starts the in-Max listener
- `Step2___EXT_NAME__-__VERSION__.mcpb` - Claude Desktop extension (uv runtime; no Python install needed)
- `Uninstall_from_3dsMax.ms` - removes the listener, panel, menu and startup script

## Requirements
Windows, 3ds Max 2025+ (built on 2026), current Claude Desktop.

## Known limitations
- `execute_maxscript` can change or delete anything in the open scene; save your work first, or turn it off in the AI Bridge panel.
- `format`/`print` output from MAXScript is not returned to Claude - only a script's final value.
- One 3ds Max window at a time holds the bridge (port __PORT__).

## Checksums (SHA-256)
__CHECKSUMS__
"""


SETUP_ISS = r"""; 3ds Max AI Bridge __VERSION__ - Windows setup (generated by build_release.py)
; Installs the bridge for every 3ds Max 2025+ user folder it finds, then hands
; the .mcpb extension to Claude Desktop. Per-user, no admin rights needed.

[Setup]
AppId={{8C2F4E1A-5B7D-4F3E-9A21-6D0B3C7E9F42}
AppName=3ds Max AI Bridge
AppVersion=__VERSION__
AppVerName=3ds Max AI Bridge __VERSION__
AppPublisher=__AUTHOR_NAME__
AppPublisherURL=__AUTHOR_URL__
DefaultDirName={localappdata}\Programs\3ds Max AI Bridge
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
DisableWelcomePage=no
LicenseFile=__LICENSE__
OutputDir=__OUTDIR__
OutputBaseFilename=__OUTNAME__
Compression=lzma2
SolidCompression=yes
UninstallDisplayName=3ds Max AI Bridge
CloseApplications=no

[Messages]
WelcomeLabel2=This sets up the 3ds Max AI Bridge, which lets Claude (in Claude Desktop) work in your 3ds Max scenes.%n%nIt installs the bridge into every 3ds Max 2025 or newer on this PC and then adds the extension to Claude Desktop.%n%nPlease close 3ds Max before continuing.
FinishedLabel=Almost done:%n%n1. Click Finish. Claude Desktop opens and shows the 3ds Max AI Bridge extension - click Install.%n%n2. Start (or restart) 3ds Max. Use the AI Bridge menu to choose which tools Claude may use.%n%nThen ask Claude: "What's in my 3ds Max scene?"

[Files]
Source: "__STAGE__\AIBridge\*"; DestDir: "{app}\AIBridge"; Flags: ignoreversion
Source: "__STAGE__\AIBridge_Startup.ms"; DestDir: "{app}"; Flags: ignoreversion
Source: "__STAGE__\__MCPB__"; DestDir: "{app}"; Flags: ignoreversion
Source: "__STAGE__\READ_ME_FIRST.txt"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "{app}\__MCPB__"; Description: "Add the extension to Claude Desktop now"; Flags: shellexec postinstall nowait skipifsilent

[Code]
const
  BridgeFiles = 'max_ai_listener.py,max_ai_panel.py,aibridge_boot.py,VERSION.txt';

var
  MaxDirs: TStringList;

{ Every <localappdata>\Autodesk\3dsMax\<year> - 64bit\<language> folder with a
  scripts folder, for 3ds Max 2025 and newer. They exist once Max has run. }
procedure FindMaxDirs;
var
  YearDir, LangDir: TFindRec;
  Base, Path: String;
begin
  MaxDirs := TStringList.Create;
  Base := ExpandConstant('{localappdata}\Autodesk\3dsMax\');
  if FindFirst(Base + '*', YearDir) then
  try
    repeat
      if (Pos(' - 64bit', YearDir.Name) > 0) and (StrToIntDef(Copy(YearDir.Name, 1, 4), 0) >= 2025) then
        if FindFirst(Base + YearDir.Name + '\*', LangDir) then
        try
          repeat
            Path := Base + YearDir.Name + '\' + LangDir.Name;
            if (LangDir.Name <> '.') and (LangDir.Name <> '..') and DirExists(Path + '\scripts') then
              MaxDirs.Add(Path);
          until not FindNext(LangDir);
        finally
          FindClose(LangDir);
        end;
    until not FindNext(YearDir);
  finally
    FindClose(YearDir);
  end;
end;

function InitializeSetup(): Boolean;
begin
  FindMaxDirs;
  Result := True;
  if MaxDirs.Count = 0 then
    Result := MsgBox('No 3ds Max 2025 or newer settings folder was found for this Windows user.' + #13#10#13#10 +
      'Open 3ds Max once (it creates its settings folder on first start), close it, and run this setup again.' + #13#10#13#10 +
      'Continue anyway and only add the Claude Desktop extension?',
      mbConfirmation, MB_YESNO) = IDYES;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  I: Integer;
begin
  Result := 'Install the bridge into:' + NewLine;
  if MaxDirs.Count = 0 then
    Result := Result + Space + '(no 3ds Max 2025+ found)' + NewLine;
  for I := 0 to MaxDirs.Count - 1 do
    Result := Result + Space + MaxDirs[I] + NewLine;
  Result := Result + NewLine + 'Then add the extension to Claude Desktop.';
end;

procedure CopyBridgeTo(Dir: String);
var
  Files: TStringList;
  I: Integer;
begin
  ForceDirectories(Dir + '\scripts\AIBridge');
  ForceDirectories(Dir + '\scripts\startup');
  DelTree(Dir + '\scripts\AIBridge\__pycache__', True, True, True);
  Files := TStringList.Create;
  try
    Files.CommaText := BridgeFiles;
    for I := 0 to Files.Count - 1 do
      if not FileCopy(ExpandConstant('{app}\AIBridge\') + Files[I], Dir + '\scripts\AIBridge\' + Files[I], False) then
        MsgBox('Could not copy ' + Files[I] + ' to ' + Dir + '. Is 3ds Max still open?', mbError, MB_OK);
  finally
    Files.Free;
  end;
  FileCopy(ExpandConstant('{app}\AIBridge_Startup.ms'), Dir + '\scripts\startup\AIBridge_Startup.ms', False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  I: Integer;
begin
  if CurStep = ssPostInstall then
    for I := 0 to MaxDirs.Count - 1 do
      CopyBridgeTo(MaxDirs[I]);
end;

procedure RemoveBridgeFrom(Dir: String);
var
  Sub: TFindRec;
begin
  DeleteFile(Dir + '\scripts\startup\AIBridge_Startup.ms');
  DelTree(Dir + '\scripts\AIBridge', True, True, True);
  DeleteFile(Dir + '\usermacros\AI Bridge-AIBridge_Panel.mcr');
  if FindFirst(Dir + '\*', Sub) then
  try
    repeat
      DeleteFile(Dir + '\' + Sub.Name + '\plugcfg\AIBridge_settings.json');
    until not FindNext(Sub);
  finally
    FindClose(Sub);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  I: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    FindMaxDirs;
    for I := 0 to MaxDirs.Count - 1 do
      RemoveBridgeFrom(MaxDirs[I]);
  end;
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent() then
    MsgBox('3ds Max AI Bridge was removed from 3ds Max (restart 3ds Max if it is open).' + #13#10#13#10 +
      'Also remove the "3ds Max AI Bridge" extension in Claude Desktop: Settings > Extensions.',
      mbInformation, MB_OK);
end;
"""


def find_iscc():
    candidates = [shutil.which("ISCC.exe") or ""]
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        for sub in (os.path.join("Programs", "Inno Setup 6"), "Inno Setup 6"):
            candidates.append(os.path.join(base, sub, "ISCC.exe"))
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def build_setup_exe(version, stage_files, mcpb_name):
    """Build dist/<name>-<version>-setup.exe with Inno Setup. Returns its path."""
    iscc = find_iscc()
    if iscc is None:
        raise BuildError("Inno Setup 6 (ISCC.exe) not found - install it (winget install JRSoftware.InnoSetup) "
                         "or build with --no-setup")
    stage = os.path.join(DIST, "_setup_stage")
    for rel, data in stage_files.items():
        path = os.path.join(stage, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    out_name = "%s-%s-setup" % (EXT_NAME, version)
    iss_path = os.path.join(stage, "setup.iss")
    with open(iss_path, "w", encoding="utf-8-sig") as f:
        f.write(fill(SETUP_ISS, VERSION=version, AUTHOR_NAME=AUTHOR["name"], AUTHOR_URL=AUTHOR["url"],
                     LICENSE=os.path.join(ROOT, "LICENSE"), OUTDIR=DIST, OUTNAME=out_name,
                     STAGE=stage, MCPB=mcpb_name))
    import subprocess
    result = subprocess.run([iscc, "/Q", iss_path], capture_output=True, text=True)
    if result.returncode != 0:
        raise BuildError("Inno Setup failed:\n" + result.stdout[-3000:] + result.stderr[-3000:])
    shutil.rmtree(stage)
    return os.path.join(DIST, out_name + ".exe")


GITIGNORE = """# secrets
.env
.env.*
# bundled deps and build output
server/lib/
dist/
*.mcpb
__pycache__/
*.pyc
.venv/
"""


def fill(template, **values):
    for key, value in values.items():
        template = template.replace("__%s__" % key, str(value))
    leftover = re.findall(r"__[A-Z0-9_]+__", template)
    leftover = [t for t in leftover if t not in ("__name__", "__file__", "__dirname__")]
    if leftover:
        raise BuildError("Unfilled template placeholders: %s" % sorted(set(leftover)))
    return template


def b64(text_or_bytes):
    data = text_or_bytes.encode("utf-8") if isinstance(text_or_bytes, str) else text_or_bytes
    return base64.b64encode(data).decode("ascii")


def crlf(text):
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def ms_wrapper(filename, purpose, codename, python_source, version):
    compile(python_source, codename, "exec")  # never ship a syntax error
    encoded = b64(python_source)
    chunks = [encoded[i:i + 1000] for i in range(0, len(encoded), 1000)]
    chunk_lines = ",\n".join('\t\t\t"%s"' % c for c in chunks)
    return crlf(fill(MS_WRAPPER, FILENAME=filename, VERSION=version, PURPOSE=purpose,
                     MIN_MAX=MIN_MAX_VERSION, CHUNKS=chunk_lines, CODENAME=codename))


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

def write_zip(path, files):
    stamp = datetime.now().timetuple()[:6]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname in sorted(files):
            info = zipfile.ZipInfo(arcname, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[arcname])


def scan_for_secrets(label, files):
    for name, data in files.items():
        for pattern in SECRET_PATTERNS:
            if pattern.search(data):
                raise BuildError("Refusing to build: %s/%s looks like it contains an API key" % (label, name))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def build(version, with_setup=True):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise BuildError("Version must look like 1.2.3, got %r" % version)

    tools, port = check_sources()
    tool_names = [t["name"] for t in tools]
    listener_source = read_text(LISTENER_SRC)
    panel_source = read_text(PANEL_SRC)
    print("Tools: " + ", ".join(tool_names))

    # 1. The Claude Desktop extension (.mcpb) ---------------------------------
    mcpb_files = {
        "manifest.json": (json.dumps(manifest(version, tools), indent=2) + "\n").encode("utf-8"),
        "pyproject.toml": pyproject(version).encode("utf-8"),
        "README.md": fill(EXTENSION_README, VERSION=version, PORT=port,
                          TOOL_LIST=", ".join("`%s`" % n for n in tool_names)).encode("utf-8"),
        "server/max_bridge_server.py": read_text(SERVER_SRC).encode("utf-8"),
    }
    scan_for_secrets("mcpb", mcpb_files)

    # 2. The 3ds Max side ---------------------------------------------------------
    boot_source = fill(BOOT_PY, VERSION=version, PORT=port)
    compile(boot_source, "aibridge_boot.py", "exec")
    shim_source = crlf(fill(STARTUP_SHIM_MS, VERSION=version, MACRO=PANEL_MACRO_MS))
    installer_source = fill(INSTALLER_PY, VERSION=version, PORT=port,
                            LISTENER_B64=b64(listener_source), PANEL_B64=b64(panel_source),
                            BOOT_B64=b64(boot_source), SHIM_B64=b64(shim_source),
                            MACRO_B64=b64(crlf(PANEL_MACRO_MS)))
    uninstaller_source = fill(UNINSTALLER_PY, VERSION=version)

    mcpb_name = "Step2_%s-%s.mcpb" % (EXT_NAME, version)
    package_files = {
        "Step1_Install_in_3dsMax.ms": ms_wrapper(
            "Step1_Install_in_3dsMax.ms",
            "Installs the bridge listener into 3ds Max, makes it start with 3ds Max, and starts it now.",
            "aibridge_installer", installer_source, version).encode("utf-8"),
        "Uninstall_from_3dsMax.ms": ms_wrapper(
            "Uninstall_from_3dsMax.ms",
            "Stops the bridge listener and removes it and its startup script from 3ds Max.",
            "aibridge_uninstaller", uninstaller_source, version).encode("utf-8"),
        "READ_ME_FIRST.txt": crlf(fill(READ_ME_FIRST, VERSION=version, PORT=port, EXT_NAME=EXT_NAME,
                                       AUTHOR_NAME=AUTHOR["name"], AUTHOR_URL=AUTHOR["url"])).encode("utf-8"),
    }
    # Scan the decoded payloads too - base64 would hide a key from a plain scan.
    scan_for_secrets("max-side", {
        "listener": listener_source.encode("utf-8"), "panel": panel_source.encode("utf-8"),
        "boot": boot_source.encode("utf-8"),
        "installer": installer_source.encode("utf-8"), "uninstaller": uninstaller_source.encode("utf-8"),
    })
    scan_for_secrets("package", package_files)

    # 3. Write outputs ------------------------------------------------------------
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST)

    mcpb_path = os.path.join(DIST, mcpb_name)
    write_zip(mcpb_path, mcpb_files)
    with open(mcpb_path, "rb") as f:
        package_files[mcpb_name] = f.read()

    zip_name = "%s-%s.zip" % (EXT_NAME, version)
    zip_path = os.path.join(DIST, zip_name)
    write_zip(zip_path, {"%s-%s/%s" % (EXT_NAME, version, k): v for k, v in package_files.items()})
    os.remove(mcpb_path)  # it lives inside the zip; keep dist/ to the shareable files
    outputs = [zip_path]

    if with_setup:
        setup_mcpb = "%s-%s.mcpb" % (EXT_NAME, version)
        outputs.append(build_setup_exe(version, {
            "AIBridge/max_ai_listener.py": listener_source.encode("utf-8"),
            "AIBridge/max_ai_panel.py": panel_source.encode("utf-8"),
            "AIBridge/aibridge_boot.py": boot_source.encode("utf-8"),
            "AIBridge/VERSION.txt": (version + "\n").encode("utf-8"),
            "AIBridge_Startup.ms": shim_source.encode("utf-8"),
            setup_mcpb: package_files[mcpb_name],
            "READ_ME_FIRST.txt": package_files["READ_ME_FIRST.txt"],
        }, setup_mcpb))

    checksums = "\n".join("%s  %s" % (sha256(p), os.path.basename(p)) for p in outputs)
    with open(os.path.join(DIST, "SHA256SUMS.txt"), "w", encoding="utf-8") as f:
        f.write(checksums + "\n")
    with open(os.path.join(DIST, "RELEASE_NOTES.md"), "w", encoding="utf-8") as f:
        f.write(fill(RELEASE_NOTES, VERSION=version, PORT=port, EXT_NAME=EXT_NAME,
                     TOOL_LIST="\n".join("- `%s` - %s" % (t["name"], t["description"]) for t in tools),
                     CHECKSUMS="```\n" + checksums + "\n```"))

    # Keep the source folder's manifest in step with what ships.
    with open(os.path.join(ROOT, "manifest.json"), "wb") as f:
        f.write(mcpb_files["manifest.json"])

    gitignore = os.path.join(ROOT, ".gitignore")
    if not os.path.exists(gitignore):
        with open(gitignore, "w", encoding="utf-8") as f:
            f.write(GITIGNORE)
        print("Wrote .gitignore (keeps .env, server/lib and dist out of git)")

    print("Built %s (%d KB)" % (zip_path, os.path.getsize(zip_path) // 1024))
    for name in sorted(package_files):
        print("  %-45s %7d bytes" % (name, len(package_files[name])))
    for path in outputs[1:]:
        print("Built %s (%d KB)" % (path, os.path.getsize(path) // 1024))
    print(checksums)
    return outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", default=VERSION)
    parser.add_argument("--no-setup", action="store_true", help="skip the Inno Setup .exe")
    args = parser.parse_args(argv)
    try:
        build(args.version, with_setup=not args.no_setup)
    except BuildError as e:
        print("BUILD FAILED: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
