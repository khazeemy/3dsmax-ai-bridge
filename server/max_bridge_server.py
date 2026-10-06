"""
3ds Max AI Bridge - MCP Server
================================

This is the MCP server that Claude (or any MCP client) talks to. It exposes
tools for reading 3ds Max scene state and executing actions, and forwards
every call over a local TCP socket to the listener script running inside
3ds Max (see ../max-listener/max_ai_listener.py).

Install:
    pip install mcp

Run standalone (for testing the socket connection):
    python max_bridge_server.py

Wire up to Claude Desktop / Claude Code by adding to your MCP config:
    {
      "mcpServers": {
        "3dsmax-ai-bridge": {
          "command": "python",
          "args": ["/absolute/path/to/max_bridge_server.py"]
        }
      }
    }

Requires: the max_ai_listener.py script must already be running inside
3ds Max (listening on 127.0.0.1:8765 by default) before tool calls will
succeed.
"""

import json
import os
import socket
import uuid

from mcp.server.fastmcp import FastMCP, Image

MAX_HOST = "127.0.0.1"
MAX_PORT = 8765
SOCKET_TIMEOUT_SECONDS = 10
SLOW_TIMEOUT_SECONDS = 180  # big scenes: optimiser, spline copies, scripts

mcp = FastMCP(
    "3dsmax-ai-bridge",
    instructions=(
        "The user chooses which of these tools you may use in the AI Bridge panel inside "
        "3ds Max. A tool they turned off fails with a message saying so. When that happens, "
        "tell the user and let them decide; do not reach the same result another way "
        "(for example with execute_maxscript)."
    ),
)


class MaxConnectionError(RuntimeError):
    """Raised when we can't reach the listener running inside 3ds Max."""


def _send_to_max(action: str, params: dict | None = None, timeout: float = SOCKET_TIMEOUT_SECONDS) -> dict:
    """
    Open a short-lived TCP connection to the Max-side listener, send one
    JSON request, read one JSON response, and return the parsed result.

    A fresh connection per call keeps this simple and robust - if Max is
    closed and reopened, or the listener restarts, we don't need to manage
    stale connection state on this side.
    """
    request = {"id": str(uuid.uuid4()), "action": action, "params": params or {}}
    payload = (json.dumps(request) + "\n").encode("utf-8")

    try:
        with socket.create_connection((MAX_HOST, MAX_PORT), timeout=SOCKET_TIMEOUT_SECONDS) as sock:
            sock.sendall(payload)
            sock.settimeout(timeout)

            buffer = b""
            while b"\n" not in buffer:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        raise MaxConnectionError(
            "Could not reach the 3ds Max listener at "
            f"{MAX_HOST}:{MAX_PORT}. Is 3ds Max open with "
            "max_ai_listener.py running? "
            f"(underlying error: {type(e).__name__}: {e})"
        ) from e

    if not buffer:
        raise MaxConnectionError("3ds Max listener closed the connection without responding.")

    line, _, _ = buffer.partition(b"\n")
    response = json.loads(line.decode("utf-8"))

    if not response.get("ok"):
        raise RuntimeError(f"3ds Max reported an error: {response.get('error')}")

    return response.get("result", {})


@mcp.tool()
def get_scene_state() -> dict:
    """
    Get a full snapshot of the current 3ds Max scene: every top-level object,
    its class (Box, Editable_Poly, Camera, etc), assigned material, modifier
    stack, and position. Use this to understand what's in the scene before
    deciding on any action.
    """
    return _send_to_max("get_scene_state")


@mcp.tool()
def list_selection() -> dict:
    """
    Get the objects currently selected in the 3ds Max viewport. Use this to
    understand what the user is actively working on right now.
    """
    return _send_to_max("list_selection")


@mcp.tool()
def list_materials() -> dict:
    """
    List all materials currently available in the scene's material library.
    Use this before apply_material to confirm exact material names and to
    reason about which material best matches which object.
    """
    return _send_to_max("list_materials")


@mcp.tool()
def apply_material(material_name: str, object_names: list[str]) -> dict:
    """
    Apply a named material to one or more named objects in the scene.

    Args:
        material_name: Exact name of an existing material (see list_materials).
        object_names: Exact names of objects to apply it to (see get_scene_state
            or list_selection).

    For batches of 10 or more objects, confirm the plan with the user before
    calling this tool - this action is not easily undone via this interface.
    """
    return _send_to_max("apply_material", {
        "material_name": material_name,
        "object_names": object_names,
    })


@mcp.tool()
def execute_maxscript(code: str) -> dict:
    """
    Execute a block of MAXScript code directly against the live 3ds Max
    scene - anything from a single property change to a full multi-line
    script (rollouts, function definitions, object creation, createDialog).
    Returns the script's final expression as a string, or the real
    MAXScript error text if it fails.

    Note: `format`/`print` output inside the script is NOT captured here -
    it goes to the in-Max Listener, not this return value. If a script
    needs to report something back (e.g. which of several candidate
    property names worked), have it build that as its actual final
    expression rather than a `format` call.

    Scripts run on 3ds Max's main thread as one undo step, so Ctrl+Z in Max
    reverts the whole script. They can open dialogs or Qt windows. A script
    that waits for user input blocks the bridge until it is answered.

    This is the most powerful tool in this bridge - it can create, modify,
    or delete anything in the scene, with no undo guarantee from this
    interface. Prefer small, targeted scripts you can reason about over
    broad "clean up" style requests, and confirm with the user before
    running anything destructive or scene-wide.
    """
    return _send_to_max("execute_maxscript", {"code": code}, timeout=SLOW_TIMEOUT_SECONDS)


def _drop_none(**params) -> dict:
    return {k: v for k, v in params.items() if v is not None}


# ---------------------------------------------------------------------------
# Modeling
# ---------------------------------------------------------------------------

@mcp.tool()
def get_object_properties(object_name: str) -> dict:
    """
    Get full details of one object: position, rotation (degrees), scale,
    bounding box and size, face/vertex count, layer, material, parent and
    children, every base-object parameter with its current value, and each
    modifier (index 0 = top of the stack) with its parameters. Also returns
    the scene's system units.

    Use this before set_object_properties to learn exact parameter names.
    """
    return _send_to_max("get_object_properties", {"object_name": object_name})


@mcp.tool()
def create_object(object_type: str, name: str | None = None, position: list | None = None,
                  rotation: list | None = None, properties: dict | None = None) -> dict:
    """
    Create one object of any creatable 3ds Max class: geometry (Box, Sphere,
    Cylinder, Cone, Torus, Tube, Pyramid, Plane, GeoSphere, ChamferBox,
    ChamferCyl, Teapot...), shapes (Rectangle, Circle, Arc, Text, Helix...),
    lights, cameras or helpers (Point, Dummy).

    Args:
        object_type: MAXScript class name, e.g. "Box".
        name: Object name (made unique if taken).
        position: [x, y, z] in world space. Each value is a number in system
            units or a string with units ("1.2m", "40cm").
        rotation: [x, y, z] Euler degrees.
        properties: Class parameters, e.g. {"length": "2m", "width": "1m",
            "height": "75cm", "lengthsegs": 4, "wirecolor": [200, 120, 40]}.
            Unknown names are reported back with the valid ones.

    Geometry is usually built up from its pivot: Box/Cylinder pivots are at
    the bottom centre, Sphere at its centre. Changes are one undo step in Max.
    """
    return _send_to_max("create_object", _drop_none(object_type=object_type, name=name, position=position,
                                                     rotation=rotation, properties=properties))


@mcp.tool()
def create_spline(splines: list[dict], name: str | None = None, knot_type: str = "corner",
                  properties: dict | None = None) -> dict:
    """
    Draw a spline shape from point lists - outlines, floor plans, paths,
    profiles for Extrude/Lathe/Sweep, or paths for copy_along_spline.

    Args:
        splines: One entry per sub-spline: {"points": [[x, y, z], ...],
            "closed": true/false}. Points are world space; values are numbers
            in system units or strings with units.
        name: Shape name.
        knot_type: "corner" (straight segments), "smooth", "bezier" or
            "bezierCorner".
        properties: Shape settings, e.g. {"render_renderable": true,
            "render_thickness": "2cm"}.

    For a solid from an outline: create_spline with closed splines, then
    add_modifier "Extrude" with {"amount": ...}.
    """
    return _send_to_max("create_spline", _drop_none(splines=splines, name=name, knot_type=knot_type,
                                                     properties=properties))


@mcp.tool()
def transform_objects(object_names: list[str], position: list | None = None, rotation: list | None = None,
                      scale: list | float | None = None, mode: str = "absolute") -> dict:
    """
    Move, rotate and/or scale objects.

    Args:
        object_names: Objects to change.
        position: [x, y, z] (numbers in system units or strings with units).
        rotation: [x, y, z] Euler degrees, world axes, about each pivot.
        scale: One factor or [x, y, z] factors.
        mode: "absolute" sets the values; "relative" moves by / rotates by /
            multiplies by them.
    """
    return _send_to_max("transform_objects", _drop_none(object_names=object_names, position=position,
                                                         rotation=rotation, scale=scale, mode=mode))


@mcp.tool()
def set_object_properties(object_name: str, properties: dict, modifier: str | int | None = None) -> dict:
    """
    Change parameters on an object, or on one of its modifiers.

    Args:
        object_name: The object.
        properties: {name: value}. Lengths can be strings with units; colours
            are [r, g, b] 0-255; MAXScript names as "#name". Node settings
            work too (wirecolor, renderable, isHidden, boxMode...).
        modifier: Modifier name or stack index (0 = top) to edit instead of
            the object itself.

    Get exact parameter names from get_object_properties first.
    """
    return _send_to_max("set_object_properties", _drop_none(object_name=object_name, properties=properties,
                                                             modifier=modifier))


@mcp.tool()
def add_modifier(object_names: list[str], modifier_type: str, properties: dict | None = None,
                 instance: bool = True, name: str | None = None) -> dict:
    """
    Add a modifier to the top of each object's stack.

    Args:
        object_names: Objects to modify.
        modifier_type: MAXScript class, e.g. "Extrude", "Shell", "TurboSmooth",
            "Edit_Poly", "Bend", "Taper", "Lathe", "Sweep", "Symmetry",
            "FFD_2x2x2", "UVWmap", "Chamfer", "ProOptimizer".
        properties: Modifier parameters, e.g. {"amount": "30cm"} for Extrude,
            {"iterations": 2} for TurboSmooth.
        instance: With several objects, share one modifier instance (editing
            it changes all) - true by default.
        name: Optional modifier name shown in the stack.
    """
    return _send_to_max("add_modifier", _drop_none(object_names=object_names, modifier_type=modifier_type,
                                                    properties=properties, instance=instance, name=name))


@mcp.tool()
def clone_objects(object_names: list[str], count: int = 1, offset: list | None = None,
                  rotation_step: list | None = None, clone_type: str = "instance") -> dict:
    """
    Clone objects in an array: copy k is moved by k * offset and rotated by
    k * rotation_step (degrees, about its own pivot).

    Args:
        object_names: Objects to clone.
        count: Clones per object (1-1000).
        offset: [x, y, z] step between clones.
        rotation_step: [x, y, z] degrees per clone.
        clone_type: "instance" (share geometry - lighter scenes), "copy" or
            "reference".
    """
    return _send_to_max("clone_objects", _drop_none(object_names=object_names, count=count, offset=offset,
                                                     rotation_step=rotation_step, clone_type=clone_type),
                        timeout=SLOW_TIMEOUT_SECONDS)


@mcp.tool()
def delete_objects(object_names: list[str]) -> dict:
    """
    Delete objects. The user can undo it in 3ds Max (Ctrl+Z). Confirm with
    the user first unless they asked for this exact deletion.
    """
    return _send_to_max("delete_objects", {"object_names": object_names})


# ---------------------------------------------------------------------------
# Viewport and reference images
# ---------------------------------------------------------------------------

@mcp.tool()
def capture_viewport(camera_name: str | None = None, max_width: int = 1280) -> list:
    """
    Take a picture of the active 3ds Max viewport (or a camera's view, with
    safe frames showing the render area) and return it as an image, so you
    can see what the scene looks like.

    Use it to check your own work: after modeling steps, after framing a
    camera with create_camera_view, or to compare a model against a
    reference image. The view is restored afterwards.

    Args:
        camera_name: Show this camera's view for the capture.
        max_width: Downscale to this width in pixels (default 1280).
    """
    result = _send_to_max("capture_viewport", _drop_none(camera_name=camera_name, max_width=max_width),
                          timeout=60)
    path = result.pop("path")
    try:
        with open(path, "rb") as f:
            data = f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return [Image(data=data, format="png"), json.dumps(result)]


@mcp.tool()
def create_reference_plane(image_path: str, view: str = "front", width: float | str = "2m",
                           position: list | None = None, name: str | None = None,
                           see_through: bool = False, freeze: bool = True) -> dict:
    """
    Model from an image: put a reference image on a plane in the scene,
    sized to the image's aspect ratio, facing the given viewport, shown in
    the viewport, not renderable and frozen (so it can't be picked by
    accident).

    The image must be a file on this computer - ask the user for its full
    path (images pasted into the chat are not files 3ds Max can open).

    Workflow for building a model from an image: look at the image yourself,
    create the reference plane at real-world width, then trace outlines with
    create_spline (pixel (0,0) is the plane's top-left corner; the result
    gives units_per_pixel), extrude or block out with create_object /
    add_modifier, and check progress with capture_viewport.

    Args:
        image_path: Full path to a .jpg/.png/... file.
        view: "front", "back", "left", "right" or "top" - the viewport the
            plane faces.
        width: Real-world width of what the image shows (number in system
            units, or "12m").
        position: [x, y, z] centre of the plane.
        see_through: Make the plane semi-transparent in the viewport.
        freeze: Freeze it (default true).
    """
    return _send_to_max("create_reference_plane", _drop_none(
        image_path=image_path, view=view, width=width, position=position, name=name,
        see_through=see_through, freeze=freeze))


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

@mcp.tool()
def copy_along_spline(source_names: list[str], spline_name: str, count: int | None = None,
                      spacing: float | str | None = None, clone_type: str = "instance",
                      align_to_path: bool = True, forward_axis: str = "x",
                      side_offset: float | str = 0, up_offset: float | str = 0,
                      random_seed: int | None = None, rotation_jitter: float = 0,
                      scale_jitter: list | None = None, spline_index: int | None = None,
                      include_end: bool | None = None, layer: str | None = None,
                      name_prefix: str | None = None) -> dict:
    """
    Place copies of objects along a drawn spline (fences, railings, columns,
    street lights, trees along a path, chairs around a curve). Works on every
    sub-spline of the shape unless spline_index is given.

    Args:
        source_names: Object(s) to copy. Several sources are used in turn, or
            picked at random when random_seed is set.
        spline_name: The spline/shape to follow (any shape: Line, Circle...).
        count: Copies per spline (evenly spread, ends included on open
            splines). Give count OR spacing.
        spacing: Distance between copies, e.g. "2.5m". Closed splines adjust
            it slightly so copies spread evenly.
        clone_type: "instance" (default, lightest), "copy" or "reference".
        align_to_path: Rotate copies to follow the curve.
        forward_axis: Source object's axis that should point along the path:
            "x" or "y".
        side_offset: Shift left (+) / right (-) of the path direction.
        up_offset: Shift up (+) / down (-).
        random_seed: Repeatable randomness for source picking and jitter.
        rotation_jitter: Random +/- degrees about the up axis.
        scale_jitter: [min, max] random uniform scale, e.g. [0.9, 1.1].
        spline_index: Only use this sub-spline (1-based).
        include_end: With spacing on open splines, also place one at the end.
        layer: Put the copies on this layer (created if needed).
        name_prefix: Base name for the copies.

    Copies are placed by their pivot points.
    """
    return _send_to_max("copy_along_spline", _drop_none(
        source_names=source_names, spline_name=spline_name, count=count, spacing=spacing,
        clone_type=clone_type, align_to_path=align_to_path, forward_axis=forward_axis,
        side_offset=side_offset, up_offset=up_offset, random_seed=random_seed,
        rotation_jitter=rotation_jitter, scale_jitter=scale_jitter, spline_index=spline_index,
        include_end=include_end, layer=layer, name_prefix=name_prefix), timeout=SLOW_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Scene optimiser
# ---------------------------------------------------------------------------

@mcp.tool()
def analyze_scene(heavy_face_threshold: int = 500000) -> dict:
    """
    Check the scene for performance problems without changing anything:
    face totals and the heaviest objects, identical objects that are not
    instanced (and how many faces instancing would save), TurboSmooth /
    MeshSmooth with high viewport iterations, objects over
    heavy_face_threshold faces (proxy candidates), missing texture files,
    duplicate material names, empty objects and empty layers - with
    recommendations.

    Summarise the findings for the user before running optimize_scene.
    """
    return _send_to_max("analyze_scene", {"heavy_face_threshold": heavy_face_threshold},
                        timeout=SLOW_TIMEOUT_SECONDS)


@mcp.tool()
def optimize_scene(operations: list[str], dry_run: bool = True, object_names: list[str] | None = None,
                   viewport_iterations: int = 1) -> dict:
    """
    Apply safe optimisations. Runs as a preview (dry_run true) by default:
    show the user what would change, and only call again with dry_run false
    once they agree. Applying is one undo step in 3ds Max.

    Args:
        operations: Any of:
            "instance_duplicates" - turn identical copies (same geometry,
                checked vertex by vertex) into instances of one object.
                Materials and positions are kept.
            "reduce_viewport_subdivision" - TurboSmooth/MeshSmooth: drop
                viewport iterations to viewport_iterations and keep the old
                value as render iterations (renders look the same).
            "delete_empty_objects" - delete geometry with no faces.
            "remove_empty_layers" - delete layers with no objects or sublayers.
        dry_run: Preview only (default true).
        object_names: Limit to these objects (default: whole scene).
        viewport_iterations: Target for reduce_viewport_subdivision.
    """
    return _send_to_max("optimize_scene", _drop_none(
        operations=operations, dry_run=dry_run, object_names=object_names,
        viewport_iterations=viewport_iterations), timeout=SLOW_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# View finder
# ---------------------------------------------------------------------------

@mcp.tool()
def create_camera_view(shot: str = "three_quarter", subject_names: list[str] | None = None,
                       focal_length_mm: float | None = None, azimuth: float | None = None,
                       elevation: float | None = None, eye_height: float | str | None = None,
                       fill: float | None = None, resolution: list[int] | None = None,
                       front_direction: str = "-y", name: str | None = None,
                       set_render_size: bool = True, make_active: bool = True) -> dict:
    """
    View finder: create a targeted Physical camera that frames the subject(s)
    - the current selection unless subject_names is given - computing the
    distance so the whole subject fits the render frame.

    Shots (defaults you can override):
        three_quarter  35 deg round, 15 deg up, 35mm - general hero view
        front / side   straight on, 50mm - elevations, products
        top_down       from above, 35mm - plans, layouts
        aerial         40 deg round, 40 deg up, 50mm - site / overview
        eye_level      camera 160cm above the subject's base, 28mm, vertical
                       tilt correction on (straight verticals, archviz style)
        low_hero       camera 40cm up, 24mm, tilt correction on - dramatic
        closeup        85mm, subject fills the frame

    Args:
        shot: One of the shots above.
        subject_names: Objects to frame (default: selection).
        focal_length_mm: Lens (36mm film width).
        azimuth: Degrees round from the subject's front; + orbits to its right.
        elevation: Degrees above horizontal (replaces eye_height).
        eye_height: Camera height above the subject's lowest point, e.g. "170cm".
        fill: How much of the frame the subject fills (0.1-1, default 0.85).
        resolution: [width, height] in pixels; sets the render size unless
            set_render_size is false. Default: current render size.
        front_direction: World direction the subject's front faces: "-y"
            (3ds Max Front view, default), "y", "x" or "-x".
        name: Camera name.
        make_active: Show the camera in the active viewport (default true).

    Call several times for several shots. Then use capture_viewport to check
    the framing and adjust.
    """
    return _send_to_max("create_camera_view", _drop_none(
        shot=shot, subject_names=subject_names, focal_length_mm=focal_length_mm, azimuth=azimuth,
        elevation=elevation, eye_height=eye_height, fill=fill, resolution=resolution,
        front_direction=front_direction, name=name, set_render_size=set_render_size,
        make_active=make_active))


if __name__ == "__main__":
    mcp.run()
