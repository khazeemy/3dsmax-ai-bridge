# Architecture Reference: 3dsmax-ai-bridge

This explains how the pieces fit together, for Claude's own reference when
debugging connection issues or when the user wants to extend the bridge
with new actions.

## Components

1. **MCP server** (`servers/max_bridge_server.py`) - what Claude actually
   talks to via MCP. Exposes tools (`get_scene_state`, `list_selection`,
   `list_materials`, `apply_material`). Each tool opens a short-lived TCP
   connection to the Max-side listener, sends one JSON request, reads one
   JSON response, and returns the result.

2. **Max-side listener** (`servers/max_ai_listener.py`) - runs inside 3ds
   Max's own Python interpreter via `pymxs`. Opens a TCP socket server on
   `127.0.0.1:8765` (configurable) and dispatches incoming requests to
   small handler functions (`action_get_scene_state`, etc.) that call into
   `pymxs.runtime` to read or modify the live scene.

3. **AI Bridge panel** (`server/max_ai_panel.py`) - dockable Qt panel in
   3ds Max (AI Bridge menu). Start/stop the listener, choose which tools
   Claude may use, and watch an activity log. Tool on/off states are
   enforced by the listener (a disabled tool returns an error) and saved
   to `plugcfg/AIBridge_settings.json`.

## Threading

Requests arrive on per-connection worker threads, but every handler runs
on 3ds Max's main thread: the worker posts the job through a queued Qt
signal (`_run_on_main_thread`) and waits for it. Don't switch back to
`pymxs.mxstoken()` from a worker - it runs the code on the worker thread,
and graphics/UI work there (viewport textures, Qt windows) crashed or
froze Max in testing. Non-read tools are wrapped in one undo step
(`_call_handler`; note `pymxs.undo` swallows exceptions). The panel gets
listener events through `add_observer` and a Qt signal.

## Wire protocol

Newline-delimited JSON over TCP. One request, one response, per line:

```
Request:  {"id": "<uuid>", "action": "<name>", "params": {...}}
Response: {"id": "<uuid>", "ok": true,  "result": {...}}
       or {"id": "<uuid>", "ok": false, "error": "<message>"}
```

A fresh TCP connection is opened per call from the MCP server side. This
keeps the MCP server stateless with respect to the socket - if Max is
restarted, the next tool call just reconnects.

## Why two processes instead of one

3ds Max embeds its own Python interpreter; an external Python process
(where the MCP server runs) cannot import `pymxs` directly - `pymxs` only
exists inside Max's process. So the listener must run inside Max, and the
MCP server must run outside it (since MCP servers are typically launched
as independent stdio subprocesses by the MCP client). The TCP socket is
the bridge between these two separate Python processes.

## Adding a new action

To add a new capability (e.g. a future rigging action):

1. In `max_ai_listener.py`: write a new `action_<name>(params)` function
   that does the pymxs work and returns a JSON-serializable dict. Register
   it in the `ACTIONS` dict, and add a `TOOLS` entry (label, category,
   risk, default_enabled, description) so it appears in the AI Bridge
   panel. `build_release.py` refuses to build if the two don't match.
2. In `max_bridge_server.py`: add a new `@mcp.tool()` function with a
   clear docstring (this docstring is what Claude reads to decide when and
   how to call the tool), which calls `_send_to_max("<name>", {...})`.
3. Keep handler functions small and defensive - validate `params` and
   raise `ValueError` with a clear message for bad input, since that
   message is what surfaces back to the user through the bridge.

## Known limitations (v0.1)

- No undo support exposed through the bridge - if `apply_material` is
  called with a wrong mapping, the user must undo manually in Max
  (Ctrl+Z) or fix it with a follow-up `apply_material` call.
- No live event listening yet (selection-change callbacks, modifier-added
  callbacks). Scene state is only available as an on-demand snapshot via
  polling (`get_scene_state` / `list_selection`), not a continuous stream
  of "what the user is doing." This is intentional for v0.1 - callback-
  based listening is planned for a later version once the polling-based
  foundation is proven reliable.
- Rigging and camera setup actions are not yet implemented.
