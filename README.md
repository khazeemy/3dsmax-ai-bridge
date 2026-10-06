# 3ds Max AI Bridge

Connects Claude Desktop to a live 3ds Max session. Claude can read your
scene, model, lay out copies along splines, optimise the scene, frame
cameras, and run MAXScript. In the **AI Bridge** panel inside 3ds Max you
choose which of these tools Claude may use.

Requires Windows, 3ds Max 2025 or newer (built on 2026) and Claude Desktop.

## Install

**Easiest: the setup program**

1. Open 3ds Max once, then close it. (3ds Max creates its settings
   folder on first start.)
2. Download `3dsmax-ai-bridge-<version>-setup.exe` from the
   [latest release](../../releases/latest) and run it. Windows may warn
   "Windows protected your PC" because the installer isn't code-signed.
   Click **More info → Run anyway**.
3. At the end, Claude Desktop opens the extension. Click **Install**.
4. Start 3ds Max. Use the **AI Bridge** menu to choose which tools Claude
   may use.

It installs for the current Windows user only, needs no admin rights, and
can be removed from Windows **Settings → Apps**.

**Manual alternative:** download `3dsmax-ai-bridge-<version>.zip`, extract
it and follow `READ_ME_FIRST.txt`. You drag `Step1_Install_in_3dsMax.ms`
onto a 3ds Max viewport, then double-click the `.mcpb` file to install the
Claude Desktop extension.

## Tools

| Area | Tools |
|---|---|
| Scene | `get_scene_state`, `list_selection` |
| Materials | `list_materials`, `apply_material` |
| Modeling | `get_object_properties`, `create_object`, `create_spline`, `transform_objects`, `set_object_properties`, `add_modifier`, `clone_objects`, `delete_objects` |
| Reference & viewport | `capture_viewport` (Claude sees the viewport), `create_reference_plane` (model from an image) |
| Layout | `copy_along_spline` |
| Optimisation | `analyze_scene`, `optimize_scene` (previews first) |
| Cameras | `create_camera_view` (view finder: framed Physical cameras) |
| Scripting | `execute_maxscript` |

Each change Claude makes is one undo step in 3ds Max (Ctrl+Z). Tool on/off
choices are enforced inside 3ds Max and saved to
`plugcfg/AIBridge_settings.json`.

## Source layout

| File | What it is |
|---|---|
| `server/max_bridge_server.py` | MCP server Claude Desktop runs; forwards each tool call to Max |
| `server/max_ai_listener.py` | Runs inside 3ds Max: socket listener on 127.0.0.1:8765, tool handlers, permissions |
| `server/max_ai_panel.py` | The AI Bridge panel (Qt) inside 3ds Max |
| `server/architecture.md` | Protocol, threading model, how to add a tool |
| `build_release.py` | Builds the shippable zip in `dist/` (standard-library Python 3.10+) |

`server/claude_api_panel.py`, `server/claude_prompt_panel.py`, `.env`
and `server/lib/` are local development files and are never packaged.

## Build

```bash
python build_release.py --version 0.3.0
```

The build checks that the server tools, listener actions and panel
metadata match. It checks that the bridge only uses 127.0.0.1, scans every
output for API keys, and writes the zip, `SHA256SUMS.txt` and
`RELEASE_NOTES.md` to `dist/`. Note that it replaces `dist/`. The setup
exe needs Inno Setup 6 (`winget install JRSoftware.InnoSetup`); use
`--no-setup` to skip it.

## Privacy and safety

- The listener only accepts connections from this computer (127.0.0.1).
- Scene data reaches Claude only when Claude calls a tool, like anything
  else you share in a chat.
- `execute_maxscript` can do anything MAXScript can. Turn it off in the
  panel if you want Claude limited to the other tools.
