"""
3ds Max AI Bridge - Max-side listener
======================================

Runs INSIDE 3ds Max's Python interpreter (pymxs). Opens a local TCP socket
and listens for JSON commands from the external MCP server, executes them
against the live 3ds Max scene via pymxs, and returns JSON results.

Requirements:
    - 3ds Max 2025 or newer (pymxs built in, no extra install needed)

How to run it inside Max:
    1. Open 3ds Max.
    2. Open the Python console / Scripting > New Python Script editor.
    3. Run this file (or paste its contents and execute).
    4. Leave it running - it will block the Max Python console/thread it's
       run in, so prefer running it via the "Run in background" pattern
       described in the README (a separate Python execution context).

Protocol (newline-delimited JSON over TCP, default port 8765):
    Request:  {"id": "<str>", "action": "<str>", "params": {...}}
    Response: {"id": "<str>", "ok": true,  "result": {...}}
              {"id": "<str>", "ok": false, "error": "<str>"}
"""

import json
import math
import os
import random
import socket
import threading
import time
import traceback

import pymxs

try:
    from PySide6 import QtCore
except ImportError:  # no Qt: fall back to pymxs.mxstoken (see _run_on_main_thread)
    QtCore = None

rt = pymxs.runtime

HOST = "127.0.0.1"
PORT = 8765
MAIN_THREAD_TIMEOUT_SECONDS = 600

# Per-tool metadata shown in the in-Max AI Bridge panel (max_ai_panel.py),
# where the user picks which tools Claude may use. Keys must match ACTIONS
# (build_release.py checks this). Must stay a plain literal - the build
# script reads it with ast.literal_eval.
#   risk: "read"   - only reads the scene
#         "modify" - changes the scene in a narrow, predictable way
#         "full"   - can do anything MAXScript can (create, edit, delete, files)
#   default_enabled: state used until the user changes it in the panel.
TOOLS = {
    "get_scene_state": {
        "label": "Read scene objects",
        "category": "Scene",
        "risk": "read",
        "default_enabled": True,
        "description": "List every object with its class, material, modifiers and position.",
    },
    "list_selection": {
        "label": "Read current selection",
        "category": "Scene",
        "risk": "read",
        "default_enabled": True,
        "description": "See which objects are selected in the viewport.",
    },
    "list_materials": {
        "label": "List materials",
        "category": "Materials",
        "risk": "read",
        "default_enabled": True,
        "description": "List materials in the scene, Material Editor slots and the active Slate view.",
    },
    "apply_material": {
        "label": "Apply materials",
        "category": "Materials",
        "risk": "modify",
        "default_enabled": True,
        "description": "Assign an existing material to named objects.",
    },
    "get_object_properties": {
        "label": "Read object details",
        "category": "Modeling",
        "risk": "read",
        "default_enabled": True,
        "description": "Transform, size, face count, base object and modifier settings of one object.",
    },
    "create_object": {
        "label": "Create objects",
        "category": "Modeling",
        "risk": "modify",
        "default_enabled": True,
        "description": "Create primitives, shapes, lights, cameras and helpers.",
    },
    "create_spline": {
        "label": "Draw splines",
        "category": "Modeling",
        "risk": "modify",
        "default_enabled": True,
        "description": "Create splines from point lists (outlines, paths, profiles).",
    },
    "transform_objects": {
        "label": "Move / rotate / scale",
        "category": "Modeling",
        "risk": "modify",
        "default_enabled": True,
        "description": "Change the position, rotation or scale of objects.",
    },
    "set_object_properties": {
        "label": "Edit object settings",
        "category": "Modeling",
        "risk": "modify",
        "default_enabled": True,
        "description": "Change parameters of an object or one of its modifiers.",
    },
    "add_modifier": {
        "label": "Add modifiers",
        "category": "Modeling",
        "risk": "modify",
        "default_enabled": True,
        "description": "Add modifiers such as Extrude, Shell, TurboSmooth or Bend.",
    },
    "clone_objects": {
        "label": "Clone objects",
        "category": "Modeling",
        "risk": "modify",
        "default_enabled": True,
        "description": "Make copies or instances in a row or ring.",
    },
    "delete_objects": {
        "label": "Delete objects",
        "category": "Modeling",
        "risk": "modify",
        "default_enabled": True,
        "description": "Delete named objects (undoable with Ctrl+Z).",
    },
    "capture_viewport": {
        "label": "See the viewport",
        "category": "Reference & viewport",
        "risk": "read",
        "default_enabled": True,
        "description": "Send Claude a picture of the active viewport or a camera view.",
    },
    "create_reference_plane": {
        "label": "Reference image planes",
        "category": "Reference & viewport",
        "risk": "modify",
        "default_enabled": True,
        "description": "Put a reference image on a plane to model from.",
    },
    "copy_along_spline": {
        "label": "Copy along splines",
        "category": "Layout",
        "risk": "modify",
        "default_enabled": True,
        "description": "Place copies or instances along drawn splines by count or spacing.",
    },
    "analyze_scene": {
        "label": "Analyse scene performance",
        "category": "Optimisation",
        "risk": "read",
        "default_enabled": True,
        "description": "Find heavy objects, missed instancing, high subdivision, missing textures.",
    },
    "optimize_scene": {
        "label": "Optimise scene",
        "category": "Optimisation",
        "risk": "modify",
        "default_enabled": True,
        "description": "Apply chosen fixes, e.g. instance duplicates. Previews first by default.",
    },
    "create_camera_view": {
        "label": "View finder cameras",
        "category": "Cameras",
        "risk": "modify",
        "default_enabled": True,
        "description": "Create a Physical camera framed on the selected subject(s).",
    },
    "execute_maxscript": {
        "label": "Run MAXScript",
        "category": "Scripting",
        "risk": "full",
        "default_enabled": True,
        "description": "Run any MAXScript. Can create, change or delete anything, "
                       "including what the other tools do.",
    },
}

SETTINGS_FILE_NAME = "AIBridge_settings.json"


# ---------------------------------------------------------------------------
# Action handlers - each takes a `params` dict and returns a JSON-serializable
# result. All pymxs calls happen here, kept deliberately small and explicit
# so behavior is easy to audit and extend.
# ---------------------------------------------------------------------------

def _node_summary(node):
    """Build a compact, JSON-safe description of a single scene node."""
    try:
        mat_name = node.material.name if node.material is not None else None
    except Exception:
        mat_name = None

    try:
        modifiers = [m.name for m in node.modifiers] if hasattr(node, "modifiers") else []
    except Exception:
        modifiers = []

    node_class = str(rt.classOf(node))
    # Helper/organizational objects (import anchors, dummies, point helpers,
    # groups) aren't visible geometry. We don't drop them - a user asking
    # "what's in my scene" might reasonably want to know about these too -
    # but we flag them so Claude (or the user) can distinguish "real"
    # geometry from scaffolding without guessing from the class name alone.
    helper_class_markers = ("helper", "dummy", "point", "group")
    is_helper = any(marker in node_class.lower() for marker in helper_class_markers)

    return {
        "name": node.name,
        "class": node_class,
        "is_helper": is_helper,
        "material": mat_name,
        "modifiers": modifiers,
        "position": [round(node.position.x, 3), round(node.position.y, 3), round(node.position.z, 3)],
    }


def action_get_scene_state(params):
    """Return a snapshot of all top-level objects in the scene."""
    nodes = list(rt.objects)
    summaries = [_node_summary(n) for n in nodes]
    helper_count = sum(1 for s in summaries if s["is_helper"])
    return {
        "units": _units_info(),
        "object_count": len(nodes),
        "helper_count": helper_count,
        "objects": summaries,
    }


def _units_info():
    """Scene units, so sizes and positions can be given in the right scale."""
    return {
        "system_unit": str(rt.units.SystemType),
        "system_scale": rt.units.SystemScale,
        "note": "Numbers in tool calls are in system units. Strings with units "
                "(e.g. \"160cm\", \"2m\", \"6ft\") are converted automatically.",
    }


def action_list_selection(params):
    """Return the currently selected objects in the Max viewport."""
    selection = list(rt.selection)
    return {
        "selected_count": len(selection),
        "objects": [_node_summary(n) for n in selection],
    }


def _slate_material_editor_entries():
    """
    Best-effort enumeration of (name, material_object) pairs sitting in the
    Slate Material Editor's active node view. Materials created here do NOT
    appear in sceneMaterials (if unapplied) or meditMaterials (Slate
    bypasses the legacy 24-slot system entirely) - this is a confirmed 3ds
    Max behavior, not an edge case we're inventing.

    Each SME node exposes the actual underlying material/map via its
    `.reference` property, which gives us a real, usable object - not just
    a display name - so materials found this way CAN be applied via
    apply_material, not just listed.

    We only have a confirmed, documented way to read the ACTIVE view
    (sme.GetView(sme.activeView)/.GetNumNodes()/.GetNode(i), 1-indexed per
    MAXScript convention). There may be other, inactive SME views with
    materials we can't see this way - no confirmed API for enumerating all
    views was found, so this is intentionally best-effort: it improves
    coverage for the common case (one active view) without claiming
    completeness. Returns (list of (name, material_obj) tuples, error_or_None).
    """
    try:
        active_view = rt.sme.GetView(rt.sme.activeView)
        node_count = active_view.GetNumNodes()
        entries = []
        for i in range(1, node_count + 1):  # 1-indexed per MAXScript convention
            node = active_view.GetNode(i)
            if node is None or not hasattr(node, "name"):
                continue
            material_obj = getattr(node, "reference", None)
            entries.append((node.name, material_obj))
        return entries, None
    except Exception as e:
        # Don't let an SME quirk break list_materials/apply_material
        # entirely - report the gap instead of crashing the whole tool call.
        return [], f"{type(e).__name__}: {e}"


def action_list_materials(params):
    """
    Return all materials currently available to the user: materials
    already applied to something in the scene (rt.sceneMaterials),
    materials sitting unused in legacy Material Editor slots
    (rt.meditMaterials), AND materials sitting in the Slate Material
    Editor's active node view that aren't in either of the above (Slate
    bypasses the legacy slot system - see _slate_material_editor_names).

    meditMaterials is a fixed-size array (24 slots by default) where empty
    slots still hold a real material object (typically a default, unedited
    Standard material), not None. We can't perfectly distinguish "an empty
    slot" from "a deliberately plain grey Standard material the user made,"
    so we use a heuristic: skip slots whose material name matches the
    generic "## - Default" pattern. This errs toward inclusion - a real
    material that happens to look like a default is better shown than a
    real one hidden.

    Materials found only via Slate (not in sceneMaterials/meditMaterials)
    are reported with "applied": false and "source": "slate", since we
    only have their name from the SME, not a live reference we could use
    later to apply them - see the note on apply_material's Slate
    limitation.
    """
    seen_names = set()
    materials = []

    # Materials already applied somewhere in the scene.
    for m in rt.sceneMaterials:
        materials.append({"name": m.name, "class": str(rt.classOf(m)), "applied": True, "source": "scene"})
        seen_names.add(m.name)

    # Materials sitting in legacy Material Editor slots, not yet applied.
    for slot_material in rt.meditMaterials:
        if slot_material is None:
            continue
        name = slot_material.name
        if name in seen_names:
            continue
        if name and ("- Default" in name or name.strip() == ""):
            continue
        materials.append({"name": name, "class": str(rt.classOf(slot_material)), "applied": False, "source": "medit"})
        seen_names.add(name)

    # Materials sitting in the Slate Material Editor's active view that
    # aren't already accounted for above.
    slate_entries, slate_error = _slate_material_editor_entries()
    for name, _material_obj in slate_entries:
        if name and name not in seen_names:
            materials.append({"name": name, "class": "Unknown (Slate)", "applied": False, "source": "slate"})
            seen_names.add(name)

    result = {
        "material_count": len(materials),
        "materials": materials,
    }
    if slate_error:
        result["slate_scan_warning"] = (
            "Could not fully scan the Slate Material Editor's active view "
            f"({slate_error}). Materials visible only there and not yet "
            "applied to anything may be missing from this list."
        )
    return result


def action_apply_material(params):
    """
    Apply a named material to a list of named objects.

    params:
        material_name: str
        object_names: list[str]
    """
    material_name = params.get("material_name")
    object_names = params.get("object_names", [])

    if not material_name:
        raise ValueError("material_name is required")
    if not object_names:
        raise ValueError("object_names must be a non-empty list")

    # Search applied scene materials, unapplied Material Editor slots, AND
    # the Slate Material Editor's active view, since list_materials now
    # reports all three sources - apply_material must be able to find
    # anything list_materials shows. Collect ALL matches (not just the
    # first) so we can detect and report ambiguity rather than silently
    # picking one when duplicate names exist - which does happen in real
    # scenes (e.g. the same material imported twice).
    candidates = []
    seen_ids = set()
    for m in rt.sceneMaterials:
        if m.name == material_name and id(m) not in seen_ids:
            candidates.append(m)
            seen_ids.add(id(m))
    for m in rt.meditMaterials:
        if m is not None and m.name == material_name and id(m) not in seen_ids:
            candidates.append(m)
            seen_ids.add(id(m))
    slate_entries, _slate_error = _slate_material_editor_entries()
    for name, material_obj in slate_entries:
        if name == material_name and material_obj is not None and id(material_obj) not in seen_ids:
            candidates.append(material_obj)
            seen_ids.add(id(material_obj))

    if not candidates:
        raise ValueError(f"No material named '{material_name}' found in scene")
    if len(candidates) > 1:
        raise ValueError(
            f"Ambiguous material name '{material_name}': {len(candidates)} different "
            "materials share this name in the scene. Re-check list_materials and, if "
            "possible, rename one of them in 3ds Max to disambiguate before retrying."
        )
    target_material = candidates[0]

    applied = []
    missing = []
    for name in object_names:
        node = rt.getNodeByName(name)
        if node is None:
            missing.append(name)
            continue
        node.material = target_material
        applied.append(name)

    return {
        "material_applied": material_name,
        "applied_to": applied,
        "not_found": missing,
    }


def action_execute_maxscript(params):
    """
    Execute an arbitrary block of MAXScript code against the live scene.

    params:
        code: str - MAXScript source, one line or a full multi-line script
              (rollouts, function definitions, createDialog, etc. are all
              fine - this is the same as running a .ms file).

    Returns the value of the script's final expression as a string (MAXScript
    values like nodes, arrays, and Point3s aren't JSON-serializable, so we
    stringify with `rt.execute`'s own return value via str()). Raises if the
    script errors, which _handle_request already converts into a structured
    {"ok": false, "error": ...} response - so callers on the MCP side get
    real MAXScript error text back instead of nothing.

    KNOWN LIMITATION: `format`/`print` statements inside the executed code
    write to the in-Max Listener window, not to this return value - pymxs's
    `execute` doesn't capture that stream. If a script needs to report
    diagnostic info (e.g. "which property name worked"), have it build a
    string/dict as its actual final expression instead of relying on
    `format` - that value comes back through this tool; a `format` call
    would silently not.

    This is the most powerful action in this bridge - it can create,
    modify, or delete anything in the live scene, with no undo guarantee
    from this interface. Treat it with the same care as any other
    irreversible action: prefer targeted, reviewed scripts over broad
    "clean up the scene" style requests, and confirm with the user before
    running anything destructive or scene-wide.
    """
    code = params.get("code")
    if not code:
        raise ValueError("code is required")
    result = rt.execute(code)
    return {"result": str(result) if result is not None else None}


# ---------------------------------------------------------------------------
# Shared helpers for the modeling, layout, optimisation and camera tools
# ---------------------------------------------------------------------------

def _require_node(name):
    node = rt.getNodeByName(name) if name else None
    if node is None:
        raise ValueError(f"No object named '{name}' in the scene")
    return node


def _require_nodes(names):
    if not names:
        raise ValueError("object_names must be a non-empty list")
    nodes, missing = [], []
    for name in names:
        node = rt.getNodeByName(name)
        if node is None:
            missing.append(name)
        else:
            nodes.append(node)
    if missing:
        raise ValueError(f"Objects not found: {missing}")
    return nodes


def _length(value, what="value"):
    """A length in system units: a number, or a string with units like "160cm"."""
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a number or a length like \"2m\"")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(rt.units.decodeValue(value))
        except Exception:
            raise ValueError(f"Could not read {what} {value!r} as a length (try \"120cm\", \"2m\" or a number)")
    raise ValueError(f"{what} must be a number or a length like \"2m\", got {value!r}")


def _vec(value, what="value"):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{what} must be a list of 3 values [x, y, z], got {value!r}")
    return [_length(v, what) for v in value]


def _p3(value, what="value"):
    return rt.Point3(*_vec(value, what))


def _unique_name(name):
    return name if rt.getNodeByName(name) is None else str(rt.uniqueName(name))


def _round_list(values, digits=3):
    return [round(float(v), digits) for v in values]


def _to_json(value, depth=0):
    """Best-effort JSON-safe view of a MAXScript value."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 4)
    cls = str(rt.classOf(value))
    if cls in ("Point3", "Point2", "Point4"):
        return _round_list([value.x, value.y] + ([value.z] if cls != "Point2" else []))
    if cls == "Color":
        return _round_list([value.r, value.g, value.b], 1)
    if cls == "Quat":
        e = rt.quatToEuler(value)
        return {"euler_degrees": _round_list([e.x, e.y, e.z])}
    if cls == "Name":
        return "#" + str(value)
    if cls == "Array" and depth < 2:
        items = list(value)
        out = [_to_json(v, depth + 1) for v in items[:20]]
        if len(items) > 20:
            out.append(f"... {len(items) - 20} more")
        return out
    text = str(value)
    return text if len(text) <= 200 else text[:200] + "..."


def _coerce(current, value):
    """Convert a JSON value to the MAXScript type the property currently holds."""
    cls = str(rt.classOf(current)) if current is not None else ""
    if isinstance(value, list):
        if cls == "Color":
            return rt.Color(*value)
        if cls == "Point2":
            return rt.Point2(*value)
        if len(value) == 3:
            return _p3(value)
        return value
    if isinstance(value, str):
        if cls == "Name" or value.startswith("#"):
            return rt.Name(value.lstrip("#"))
        if cls in ("Float", "Double", "Integer", "WorldUnits"):
            number = _length(value)
            return int(round(number)) if cls == "Integer" else number
    if cls == "Integer" and isinstance(value, float):
        return int(round(value))
    return value


def _prop_names(target):
    try:
        return [str(n) for n in rt.getPropNames(target)]
    except Exception:
        return []


def _set_properties(target, properties):
    """Set {name: value} on a node, object or modifier. Returns (applied, errors)."""
    applied, errors = [], {}
    for key, value in (properties or {}).items():
        if not rt.isProperty(target, key):
            names = _prop_names(target)
            errors[key] = ("not a property of %s. Valid: %s" % (rt.classOf(target), ", ".join(names[:60])))
            continue
        try:
            rt.setProperty(target, key, _coerce(rt.getProperty(target, key), value))
            applied.append(key)
        except Exception as e:
            errors[key] = f"{type(e).__name__}: {e}"
    return applied, errors


def _euler(node):
    """Rotation as shown in the 3ds Max UI (degrees, XYZ)."""
    e = rt.quatToEuler(rt.inverse(node.rotation))
    return _round_list([e.x, e.y, e.z])


def _set_rotation(node, euler_degrees):
    pos = node.pos
    node.rotation = rt.Quat(0, 0, 0, 1)
    rt.rotate(node, rt.EulerAngles(*_vec(euler_degrees, "rotation")))
    node.pos = pos


def _class_kind(cls_value):
    try:
        return str(rt.classOf(cls_value))
    except Exception:
        return ""


def _class_from_name(type_name, kinds, what):
    cls_value = getattr(rt, type_name, None) if isinstance(type_name, str) and type_name else None
    if cls_value is None or _class_kind(cls_value) not in kinds:
        raise ValueError(f"'{type_name}' is not a {what} class in this 3ds Max "
                         f"(expected one of: {', '.join(kinds)} classes, e.g. Box, Cylinder, Plane)")
    return cls_value


def _find_modifier(node, modifier):
    mods = list(node.modifiers)
    if isinstance(modifier, int):
        if not 0 <= modifier < len(mods):
            raise ValueError(f"'{node.name}' has {len(mods)} modifier(s); index {modifier} is out of range")
        return mods[modifier]
    for m in mods:
        if m.name.lower() == str(modifier).lower():
            return m
    raise ValueError(f"'{node.name}' has no modifier named '{modifier}'. Modifiers (top first): "
                     f"{[m.name for m in mods]}")


# ---------------------------------------------------------------------------
# Modeling
# ---------------------------------------------------------------------------

CREATABLE_KINDS = ("GeometryClass", "shape", "light", "camera", "helper")


def action_create_object(params):
    cls_value = _class_from_name(params.get("object_type"), CREATABLE_KINDS, "creatable object")
    node = cls_value()
    if params.get("name"):
        node.name = _unique_name(params["name"])
    if params.get("rotation") is not None:
        _set_rotation(node, params["rotation"])
    if params.get("position") is not None:
        node.pos = _p3(params["position"], "position")
    applied, errors = _set_properties(node, params.get("properties"))
    result = {"created": _node_summary(node), "properties_set": applied}
    if errors:
        result["property_errors"] = errors
    return result


def action_create_spline(params):
    splines = params.get("splines") or []
    if not splines:
        raise ValueError("splines must be a non-empty list of {points: [[x,y,z], ...], closed: bool}")
    knot_type = params.get("knot_type", "corner")
    if knot_type not in ("corner", "smooth", "bezier", "bezierCorner"):
        raise ValueError("knot_type must be corner, smooth, bezier or bezierCorner")
    segment_type = "line" if knot_type == "corner" else "curve"

    shape = rt.SplineShape()
    for index, spline in enumerate(splines, start=1):
        points = spline.get("points") or []
        if len(points) < 2:
            raise ValueError(f"Spline {index} needs at least 2 points")
        rt.addNewSpline(shape)
        for point in points:
            rt.addKnot(shape, index, rt.Name(knot_type), rt.Name(segment_type), _p3(point, "point"))
        if spline.get("closed"):
            rt.close(shape, index)
    rt.updateShape(shape)
    rt.CenterPivot(shape)
    if params.get("name"):
        shape.name = _unique_name(params["name"])
    applied, errors = _set_properties(shape, params.get("properties"))
    result = {"created": _node_summary(shape), "spline_count": len(splines), "properties_set": applied}
    if errors:
        result["property_errors"] = errors
    return result


def action_transform_objects(params):
    nodes = _require_nodes(params.get("object_names"))
    mode = params.get("mode", "absolute")
    if mode not in ("absolute", "relative"):
        raise ValueError("mode must be 'absolute' or 'relative'")
    position, rotation, scale = params.get("position"), params.get("rotation"), params.get("scale")
    if position is None and rotation is None and scale is None:
        raise ValueError("Give at least one of position, rotation, scale")
    if isinstance(scale, (int, float)):
        scale = [scale, scale, scale]

    for node in nodes:
        if rotation is not None:
            if mode == "absolute":
                _set_rotation(node, rotation)
            else:
                rt.rotate(node, rt.EulerAngles(*_vec(rotation, "rotation")))
        if scale is not None:
            factors = rt.Point3(*[float(s) for s in scale])
            if mode == "absolute":
                node.scale = factors
            else:
                rt.scale(node, factors)
        if position is not None:
            if mode == "absolute":
                node.pos = _p3(position, "position")
            else:
                rt.move(node, _p3(position, "position"))
    return {"transformed": [
        {"name": n.name, "position": _round_list([n.pos.x, n.pos.y, n.pos.z]), "rotation": _euler(n),
         "scale": _round_list([n.scale.x, n.scale.y, n.scale.z])} for n in nodes]}


def action_get_object_properties(params):
    node = _require_node(params.get("object_name"))
    bb_min, bb_max = node.min, node.max
    faces, verts = 0, 0
    try:
        faces, verts = rt.getPolygonCount(node)
    except Exception:
        pass

    def read_props(target, limit):
        values = {}
        for name in _prop_names(target)[:limit]:
            try:
                values[name] = _to_json(rt.getProperty(target, name))
            except Exception:
                values[name] = "<unreadable>"
        return values

    return {
        "name": node.name,
        "class": str(rt.classOf(node)),
        "position": _round_list([node.pos.x, node.pos.y, node.pos.z]),
        "rotation": _euler(node),
        "scale": _round_list([node.scale.x, node.scale.y, node.scale.z]),
        "bounding_box": {"min": _round_list([bb_min.x, bb_min.y, bb_min.z]),
                         "max": _round_list([bb_max.x, bb_max.y, bb_max.z]),
                         "size": _round_list([bb_max.x - bb_min.x, bb_max.y - bb_min.y, bb_max.z - bb_min.z])},
        "faces": faces,
        "vertices": verts,
        "layer": node.layer.name,
        "hidden": node.isHidden,
        "frozen": node.isFrozen,
        "renderable": node.renderable,
        "wirecolor": _to_json(node.wirecolor),
        "material": node.material.name if node.material is not None else None,
        "parent": node.parent.name if node.parent is not None else None,
        "children": [c.name for c in node.children],
        "base_object": {"class": str(rt.classOf(node.baseObject)),
                        "properties": read_props(node.baseObject, 80)},
        "modifiers": [{"index": i, "name": m.name, "class": str(rt.classOf(m)), "enabled": m.enabled,
                       "properties": read_props(m, 40)} for i, m in enumerate(node.modifiers)],
        "units": _units_info(),
    }


def action_set_object_properties(params):
    node = _require_node(params.get("object_name"))
    properties = params.get("properties") or {}
    if not properties:
        raise ValueError("properties must be a non-empty {name: value} object")
    modifier = params.get("modifier")
    target = _find_modifier(node, modifier) if modifier is not None else node
    applied, errors = _set_properties(target, properties)
    result = {"object": node.name, "target": str(rt.classOf(target)), "properties_set": applied}
    if errors:
        result["property_errors"] = errors
    return result


def action_add_modifier(params):
    nodes = _require_nodes(params.get("object_names"))
    cls_value = _class_from_name(params.get("modifier_type"), ("modifier",), "modifier")
    shared = params.get("instance", True)
    added, skipped, errors = [], [], {}
    modifier = None
    for node in nodes:
        if modifier is None or not shared:
            modifier = cls_value()
            if params.get("name"):
                modifier.name = params["name"]
            _, errors = _set_properties(modifier, params.get("properties"))
        if not rt.validModifier(node, modifier):
            skipped.append(node.name)
            continue
        rt.addModifier(node, modifier)
        added.append(node.name)
    result = {"modifier": str(cls_value), "added_to": added, "shared_instance": bool(shared and len(added) > 1)}
    if skipped:
        result["not_valid_for"] = skipped
    if errors:
        result["property_errors"] = errors
    return result


_CLONE_FUNCTIONS = {"copy": "copy", "instance": "instance", "reference": "reference"}


def _clone(node, clone_type):
    if clone_type not in _CLONE_FUNCTIONS:
        raise ValueError("clone_type must be copy, instance or reference")
    return getattr(rt, _CLONE_FUNCTIONS[clone_type])(node)


def action_clone_objects(params):
    nodes = _require_nodes(params.get("object_names"))
    count = int(params.get("count", 1))
    if not 1 <= count <= 1000:
        raise ValueError("count must be between 1 and 1000")
    clone_type = params.get("clone_type", "instance")
    offset = _vec(params.get("offset", [0, 0, 0]), "offset")
    rotation_step = _vec(params.get("rotation_step", [0, 0, 0]), "rotation_step")
    created = []
    for node in nodes:
        for k in range(1, count + 1):
            clone = _clone(node, clone_type)
            clone.name = _unique_name(node.name)
            if any(rotation_step):
                rt.rotate(clone, rt.EulerAngles(*[r * k for r in rotation_step]))
            clone.pos = node.pos + rt.Point3(*[o * k for o in offset])
            created.append(clone.name)
    return {"created_count": len(created), "created": created[:50], "clone_type": clone_type}


def action_delete_objects(params):
    nodes = _require_nodes(params.get("object_names"))
    names = [n.name for n in nodes]
    rt.delete(nodes)
    return {"deleted": names}


# ---------------------------------------------------------------------------
# Viewport capture and reference images (for "model from image")
# ---------------------------------------------------------------------------

def action_capture_viewport(params):
    max_width = int(params.get("max_width", 1280))
    camera_name = params.get("camera_name")
    camera = _require_node(camera_name) if camera_name else None

    old_type = rt.viewport.getType()
    old_camera = rt.viewport.getCamera()
    old_tm = rt.viewport.getTM() if old_camera is None else None
    old_safe_frames = rt.displaySafeFrames
    try:
        if camera is not None:
            rt.viewport.setCamera(camera)
            rt.displaySafeFrames = True
        rt.completeRedraw()
        dib = rt.gw.getViewportDib()
        width, height = dib.width, dib.height
        if width > max_width:
            scaled = rt.Bitmap(max_width, int(round(height * max_width / float(width))))
            rt.copy(dib, scaled)
            rt.close(dib)
            dib = scaled
        path = os.path.join(str(rt.getDir(rt.Name("temp"))), "aibridge_capture_%d.png" % int(time.time() * 1000))
        dib.filename = path
        rt.save(dib)
        size = [dib.width, dib.height]
        rt.close(dib)
        view = "camera " + rt.viewport.getCamera().name if rt.viewport.getCamera() is not None \
            else str(rt.viewport.getType())
    finally:
        if camera is not None:
            if old_camera is not None:
                rt.viewport.setCamera(old_camera)
            else:
                rt.viewport.setType(old_type)
                if old_tm is not None:
                    rt.viewport.setTM(old_tm)
            rt.displaySafeFrames = old_safe_frames
            rt.completeRedraw()
    return {"path": path, "size": size, "view": view}


_PLANE_ROTATIONS = {  # plane faces the matching viewport, image upright
    "top": [], "front": [(90, 0, 0)], "back": [(90, 0, 0), (0, 0, 180)],
    "left": [(90, 0, 0), (0, 0, -90)], "right": [(90, 0, 0), (0, 0, 90)],
}


def action_create_reference_plane(params):
    image_path = params.get("image_path")
    if not image_path or not os.path.isfile(image_path):
        raise ValueError(f"Image file not found: {image_path!r}. Give the full path of an image on this computer.")
    view = params.get("view", "front")
    if view not in _PLANE_ROTATIONS:
        raise ValueError("view must be one of: " + ", ".join(_PLANE_ROTATIONS))

    bitmap = rt.openBitMap(image_path)
    if bitmap is None:
        raise ValueError(f"3ds Max could not open {image_path!r} as an image")
    image_w, image_h = bitmap.width, bitmap.height
    rt.close(bitmap)

    width = _length(params.get("width", "2m"), "width")
    length = width * image_h / float(image_w)
    plane = rt.Plane(width=width, length=length, widthsegs=1, lengthsegs=1)
    plane.name = _unique_name(params.get("name") or "AI_Reference_" + view)
    for rotation in _PLANE_ROTATIONS[view]:
        rt.rotate(plane, rt.EulerAngles(*rotation))
    if params.get("position") is not None:
        plane.pos = _p3(params["position"], "position")

    material = rt.StandardMaterial(name=plane.name + "_Mat")
    material.diffuseMap = rt.BitmapTexture(fileName=image_path)
    material.selfIllumAmount = 100
    rt.showTextureMap(material, True)
    plane.material = material
    plane.renderable = False
    plane.xray = bool(params.get("see_through", False))
    plane.showFrozenInGray = False
    plane.isFrozen = bool(params.get("freeze", True))
    return {
        "created": plane.name,
        "view": view,
        "size": _round_list([width, length]),
        "image_pixels": [image_w, image_h],
        "units_per_pixel": round(width / float(image_w), 6),
        "note": "Plane is centred on its position; image pixel (0,0) is the top-left corner. "
                "It is frozen and not renderable - unfreeze it to move it.",
    }


# ---------------------------------------------------------------------------
# Copy along splines
# ---------------------------------------------------------------------------

def _normalize(v):
    length = math.sqrt(sum(c * c for c in v))
    return [c / length for c in v] if length > 1e-9 else [0.0, 0.0, 0.0]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _path_frame(tangent, forward_axis):
    """Rows (x, y, z) of a rotation whose forward_axis follows the tangent and z stays up."""
    t = _normalize(tangent)
    up = [0.0, 0.0, 1.0] if abs(t[2]) < 0.999 else [0.0, 1.0, 0.0]
    if forward_axis == "x":
        x = t
        y = _normalize(_cross(up, x))
        z = _cross(x, y)
    else:
        y = t
        x = _normalize(_cross(y, up))
        z = _cross(x, y)
    return x, y, z


def _spline_params(length, closed, count, spacing, include_end):
    """Positions along one spline as fractions 0..1 of its length."""
    if count is not None:
        if count == 1:
            return [0.0 if closed else 0.5]
        return [k / float(count) for k in range(count)] if closed else \
            [k / float(count - 1) for k in range(count)]
    if length <= 0:
        return []
    if closed:
        n = max(1, int(round(length / spacing)))
        return [k / float(n) for k in range(n)]
    n = int(length / spacing + 1e-6) + 1
    us = [min(1.0, k * spacing / length) for k in range(n)]
    if include_end and us[-1] < 1.0 - 1e-6:
        us.append(1.0)
    return us


MAX_SPLINE_COPIES = 5000


def action_copy_along_spline(params):
    sources = _require_nodes(params.get("source_names"))
    spline_node = _require_node(params.get("spline_name"))
    if str(rt.superClassOf(spline_node)) != "shape":
        raise ValueError(f"'{spline_node.name}' is not a spline/shape (it is {rt.classOf(spline_node)})")
    count, spacing = params.get("count"), params.get("spacing")
    if (count is None) == (spacing is None):
        raise ValueError("Give exactly one of count (copies per spline) or spacing (distance between copies)")
    if count is not None:
        count = int(count)
        if count < 1:
            raise ValueError("count must be at least 1")
    else:
        spacing = _length(spacing, "spacing")
        if spacing <= 0:
            raise ValueError("spacing must be greater than 0")
    clone_type = params.get("clone_type", "instance")
    forward_axis = params.get("forward_axis", "x")
    if forward_axis not in ("x", "y"):
        raise ValueError("forward_axis must be 'x' or 'y'")
    align = params.get("align_to_path", True)
    side_offset = _length(params.get("side_offset", 0), "side_offset")
    up_offset = _length(params.get("up_offset", 0), "up_offset")
    rotation_jitter = float(params.get("rotation_jitter", 0))
    scale_jitter = params.get("scale_jitter")
    rng = random.Random(params.get("random_seed"))
    pick_random = params.get("random_seed") is not None and len(sources) > 1

    shape, temp = spline_node, None
    if str(rt.classOf(spline_node)) not in ("SplineShape", "line"):
        temp = rt.copy(spline_node)
        rt.convertToSplineShape(temp)
        shape = temp
    try:
        spline_count = rt.numSplines(shape)
        wanted = params.get("spline_index")
        indices = [int(wanted)] if wanted is not None else list(range(1, spline_count + 1))
        for i in indices:
            if not 1 <= i <= spline_count:
                raise ValueError(f"spline_index {i} is out of range (shape has {spline_count} spline(s))")

        placements = []
        for i in indices:
            length = rt.curveLength(shape, i)
            for u in _spline_params(length, rt.isClosed(shape, i), count, spacing,
                                    params.get("include_end", count is not None)):
                p = rt.lengthInterp(shape, i, u)
                t = rt.lengthTangent(shape, i, u)
                placements.append((i, [p.x, p.y, p.z], [t.x, t.y, t.z]))
        if len(placements) > MAX_SPLINE_COPIES:
            raise ValueError(f"That would create {len(placements)} copies (limit {MAX_SPLINE_COPIES}). "
                             "Use a larger spacing or smaller count.")
    finally:
        if temp is not None:
            rt.delete(temp)

    layer = None
    if params.get("layer"):
        layer = rt.LayerManager.getLayerFromName(params["layer"]) or \
            rt.LayerManager.newLayerFromName(params["layer"])

    created, per_spline = [], {}
    for n, (spline_index, pos, tangent) in enumerate(placements):
        source = rng.choice(sources) if pick_random else sources[n % len(sources)]
        tm = source.transform
        local = rt.Matrix3(tm.row1, tm.row2, tm.row3, rt.Point3(0, 0, 0))
        if scale_jitter:
            s = rng.uniform(float(scale_jitter[0]), float(scale_jitter[1]))
            local = local * rt.scaleMatrix(rt.Point3(s, s, s))
        if rotation_jitter:
            local = local * rt.rotateZMatrix(rng.uniform(-rotation_jitter, rotation_jitter))

        left = _normalize(_cross([0.0, 0.0, 1.0], tangent)) if abs(_normalize(tangent)[2]) < 0.999 else [0, 1, 0]
        world_pos = [pos[k] + left[k] * side_offset for k in range(3)]
        world_pos[2] += up_offset
        if align:
            x, y, z = _path_frame(tangent, forward_axis)
            frame = rt.Matrix3(rt.Point3(*x), rt.Point3(*y), rt.Point3(*z), rt.Point3(*world_pos))
            final = local * frame
        else:
            final = local
            final.row4 = rt.Point3(*world_pos)

        clone = _clone(source, clone_type)
        clone.transform = final
        clone.name = _unique_name(params.get("name_prefix") or source.name)
        if layer is not None:
            layer.addNode(clone)
        created.append(clone.name)
        per_spline[spline_index] = per_spline.get(spline_index, 0) + 1

    return {
        "created_count": len(created),
        "per_spline": {str(k): v for k, v in per_spline.items()},
        "created": created[:30],
        "clone_type": clone_type,
        "note": "Copies are placed by their pivot points. If they face the wrong way, retry with "
                "forward_axis 'y' or rotate the source object first.",
    }


# ---------------------------------------------------------------------------
# Scene optimiser
# ---------------------------------------------------------------------------

SUBDIVISION_MODIFIERS = ("TurboSmooth", "MeshSmooth")
BITMAP_FILE_PROPERTIES = (("BitmapTexture", "filename"), ("VRayBitmap", "HDRIMapName"),
                          ("CoronaBitmap", "filename"))


def _geometry_nodes():
    return [n for n in rt.geometry if str(rt.classOf(n)) != "Targetobject"]


def _face_count(node):
    try:
        return int(rt.getPolygonCount(node)[0])
    except Exception:
        return 0


def _instance_signature(node, faces):
    bb = rt.nodeGetBoundingBox(node, node.transform)
    size = max(bb[1].x - bb[0].x, bb[1].y - bb[0].y, bb[1].z - bb[0].z, 1e-6)
    q = size * 1e-4

    def r(v):
        return int(round(v / q))
    return (str(rt.classOf(node)), faces, tuple(m.name for m in node.modifiers),
            r(bb[0].x), r(bb[0].y), r(bb[0].z), r(bb[1].x), r(bb[1].y), r(bb[1].z))


def _instance_groups(nodes, faces_by_name):
    """Groups of objects that look identical but are not instances of each other."""
    buckets = {}
    for node in nodes:
        faces = faces_by_name[node.name]
        if faces == 0:
            continue
        buckets.setdefault(_instance_signature(node, faces), []).append(node)
    groups = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        distinct = []  # one representative per existing instance family
        for node in members:
            if not any(rt.areNodesInstances(node, d) for d in distinct):
                distinct.append(node)
        if len(distinct) > 1:
            groups.append(distinct)
    return groups


def _same_geometry(a, b, samples=24):
    """Compare sampled vertices in each node's own space."""
    mesh_a, mesh_b = rt.snapshotAsMesh(a), rt.snapshotAsMesh(b)
    count = rt.getNumVerts(mesh_a)
    if count != rt.getNumVerts(mesh_b):
        return False
    inv_a, inv_b = rt.inverse(a.transform), rt.inverse(b.transform)
    bb = a.max - a.min
    tolerance = max(bb.x, bb.y, bb.z, 1e-6) * 1e-3
    step = max(1, count // samples)
    for i in range(1, count + 1, step):
        pa = rt.getVert(mesh_a, i) * inv_a
        pb = rt.getVert(mesh_b, i) * inv_b
        if rt.distance(pa, pb) > tolerance:
            return False
    return True


def _empty_layers():
    return list(rt.execute(
        "(local r = #(); for i = 1 to LayerManager.count - 1 do ("
        "local l = LayerManager.getLayer i; local ns; l.nodes &ns;"
        "if ns.count == 0 and l.getNumChildren() == 0 do append r l.name); r)"))


def _subdivision_modifiers(nodes, min_iterations):
    found = []
    for node in nodes:
        for m in node.modifiers:
            if str(rt.classOf(m)) in SUBDIVISION_MODIFIERS and m.iterations >= min_iterations:
                found.append((node, m))
    return found


def action_analyze_scene(params):
    heavy_threshold = int(params.get("heavy_face_threshold", 500000))
    geometry = _geometry_nodes()
    faces_by_name = {n.name: _face_count(n) for n in geometry}
    total_faces = sum(faces_by_name.values())
    heaviest = sorted(geometry, key=lambda n: faces_by_name[n.name], reverse=True)[:15]

    groups = _instance_groups(geometry, faces_by_name)
    saveable = sum(faces_by_name[g[0].name] * (len(g) - 1) for g in groups)

    missing = set()
    for class_name, prop in BITMAP_FILE_PROPERTIES:
        cls_value = getattr(rt, class_name, None)
        if cls_value is None:
            continue
        for texture in rt.getClassInstances(cls_value):
            path = rt.getProperty(texture, prop) if rt.isProperty(texture, prop) else None
            if path and not os.path.isfile(path) and not rt.mapPaths.getFullFilePath(path):
                missing.add(str(path))

    material_names = {}
    for m in rt.sceneMaterials:
        material_names.setdefault(m.name, 0)
        material_names[m.name] += 1

    subdivision = _subdivision_modifiers(geometry, 2)
    empty = [n.name for n in geometry if faces_by_name[n.name] == 0]
    hidden_heavy = [n.name for n in geometry if n.isHidden and faces_by_name[n.name] >= heavy_threshold]
    heavy = [n.name for n in geometry if faces_by_name[n.name] >= heavy_threshold]
    empty_layers = _empty_layers()

    recommendations = []
    if groups:
        recommendations.append(f"{len(groups)} group(s) of identical objects are separate copies - instancing "
                               f"them saves ~{saveable:,} faces of memory (optimize_scene: instance_duplicates).")
    if subdivision:
        recommendations.append(f"{len(subdivision)} TurboSmooth/MeshSmooth modifier(s) use 2+ viewport iterations - "
                               "keep quality at render time only (optimize_scene: reduce_viewport_subdivision).")
    if heavy:
        recommendations.append(f"{len(heavy)} object(s) have {heavy_threshold:,}+ faces - consider renderer proxies "
                               "(V-Ray/Corona proxy) or ProOptimizer for distant objects.")
    if missing:
        recommendations.append(f"{len(missing)} texture file(s) are missing - they render black or slow loading. "
                               "Fix paths with the Asset Tracker (Shift+T).")
    if empty:
        recommendations.append(f"{len(empty)} geometry object(s) have no faces (optimize_scene: delete_empty_objects).")
    if empty_layers:
        recommendations.append(f"{len(empty_layers)} empty layer(s) (optimize_scene: remove_empty_layers).")
    if hidden_heavy:
        recommendations.append("Hidden heavy objects still use memory and load time: " + ", ".join(hidden_heavy[:10]))

    return {
        "counts": {"geometry": len(geometry), "shapes": len(list(rt.shapes)), "lights": len(list(rt.lights)),
                   "cameras": sum(1 for c in rt.cameras if str(rt.superClassOf(c)) == "camera"),
                   "helpers": len(list(rt.helpers)),
                   "materials": len(list(rt.sceneMaterials))},
        "total_faces": total_faces,
        "heaviest_objects": [{"name": n.name, "faces": faces_by_name[n.name],
                              "share": round(100.0 * faces_by_name[n.name] / total_faces, 1) if total_faces else 0,
                              "hidden": n.isHidden} for n in heaviest],
        "instancing_candidates": [{"objects": [n.name for n in g][:20], "copies": len(g),
                                   "faces_each": faces_by_name[g[0].name]} for g in groups[:30]],
        "instancing_faces_saveable": saveable,
        "high_viewport_subdivision": [{"object": n.name, "modifier": m.name, "iterations": m.iterations}
                                      for n, m in subdivision[:30]],
        "heavy_objects": heavy[:30],
        "missing_textures": sorted(missing)[:30],
        "duplicate_material_names": [name for name, c in material_names.items() if c > 1][:30],
        "empty_objects": empty[:30],
        "empty_layers": empty_layers[:30],
        "recommendations": recommendations or ["Nothing obvious to optimise."],
    }


OPTIMIZE_OPERATIONS = ("instance_duplicates", "reduce_viewport_subdivision", "delete_empty_objects",
                       "remove_empty_layers")


def action_optimize_scene(params):
    operations = params.get("operations") or []
    unknown = [op for op in operations if op not in OPTIMIZE_OPERATIONS]
    if not operations or unknown:
        raise ValueError(f"operations must be a list drawn from {list(OPTIMIZE_OPERATIONS)}"
                         + (f" (unknown: {unknown})" if unknown else ""))
    dry_run = params.get("dry_run", True)
    names = params.get("object_names")
    geometry = _require_nodes(names) if names else _geometry_nodes()
    report = {"dry_run": dry_run}

    if "instance_duplicates" in operations:
        faces_by_name = {n.name: _face_count(n) for n in geometry}
        changes, rejected = [], []
        for group in _instance_groups(geometry, faces_by_name):
            keeper = group[0]
            for node in group[1:]:
                if not _same_geometry(keeper, node):
                    rejected.append(node.name)
                    continue
                changes.append({"object": node.name, "becomes_instance_of": keeper.name})
                if not dry_run:
                    rt.instanceReplace(node, keeper)
        report["instance_duplicates"] = {"changed": changes[:100], "count": len(changes),
                                         "skipped_not_identical": rejected[:30]}

    if "reduce_viewport_subdivision" in operations:
        viewport_iterations = int(params.get("viewport_iterations", 1))
        changes = []
        for node, m in _subdivision_modifiers(geometry, viewport_iterations + 1):
            render_iterations = m.renderIterations if m.useRenderIterations else m.iterations
            changes.append({"object": node.name, "modifier": m.name, "viewport": f"{m.iterations} -> "
                            f"{viewport_iterations}", "render_iterations": render_iterations})
            if not dry_run:
                m.renderIterations = render_iterations
                m.useRenderIterations = True
                m.iterations = viewport_iterations
        report["reduce_viewport_subdivision"] = {"changed": changes[:100], "count": len(changes)}

    if "delete_empty_objects" in operations:
        empty = [n for n in geometry if _face_count(n) == 0 and len(list(n.children)) == 0]
        report["delete_empty_objects"] = {"objects": [n.name for n in empty][:100], "count": len(empty)}
        if not dry_run and empty:
            rt.delete(empty)

    if "remove_empty_layers" in operations:
        layers = _empty_layers()
        report["remove_empty_layers"] = {"layers": layers[:100], "count": len(layers)}
        if not dry_run:
            for name in layers:
                rt.LayerManager.deleteLayerByName(name)

    if dry_run:
        report["note"] = "Nothing was changed. Call again with dry_run false to apply."
    return report


# ---------------------------------------------------------------------------
# View finder: cameras framed around subjects
# ---------------------------------------------------------------------------

FILM_WIDTH_MM = 36.0

# azimuth 0 = from the subject's front, positive = orbit to its right;
# elevation in degrees, or eye_height for a level, tilt-corrected (2-point) view.
CAMERA_SHOTS = {
    "three_quarter": {"azimuth": 35, "elevation": 15, "focal_length_mm": 35},
    "front": {"azimuth": 0, "elevation": 0, "focal_length_mm": 50},
    "side": {"azimuth": 90, "elevation": 0, "focal_length_mm": 50},
    "top_down": {"azimuth": 0, "elevation": 89, "focal_length_mm": 35},
    "aerial": {"azimuth": 40, "elevation": 40, "focal_length_mm": 50},
    "eye_level": {"azimuth": 30, "eye_height": "160cm", "focal_length_mm": 28},
    "low_hero": {"azimuth": 25, "eye_height": "40cm", "focal_length_mm": 24},
    "closeup": {"azimuth": 20, "elevation": 10, "focal_length_mm": 85, "fill": 0.95},
}
FRONT_DIRECTIONS = {"-y": 0.0, "x": 90.0, "y": 180.0, "-x": 270.0}


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _fits(camera, target, corners, tan_h, tan_v, level):
    """True if every corner projects inside the (fill-reduced) frame."""
    forward = [target[k] - camera[k] for k in range(3)]
    if level:  # tilt-corrected: verticals stay vertical, like a level camera with lens shift
        forward[2] = 0.0
    forward = _normalize(forward)
    right = _normalize(_cross(forward, [0.0, 0.0, 1.0]))
    up = _cross(right, forward)

    def project(p):
        v = [p[k] - camera[k] for k in range(3)]
        depth = _dot(v, forward)
        return depth, _dot(v, right) / max(depth, 1e-9), _dot(v, up) / max(depth, 1e-9)

    _, _, centre_y = project(target) if level else (0, 0, 0.0)
    for corner in corners:
        depth, x, y = project(corner)
        if depth <= 1e-6 or abs(x) > tan_h or abs(y - centre_y) > tan_v:
            return False
    return True


def action_create_camera_view(params):
    names = params.get("subject_names")
    subjects = _require_nodes(names) if names else list(rt.selection)
    if not subjects:
        raise ValueError("Select the subject(s) in 3ds Max or pass subject_names")
    shot_name = params.get("shot", "three_quarter")
    if shot_name not in CAMERA_SHOTS:
        raise ValueError("shot must be one of: " + ", ".join(CAMERA_SHOTS))
    shot = dict(CAMERA_SHOTS[shot_name])
    for key in ("azimuth", "elevation", "eye_height", "focal_length_mm", "fill"):
        if params.get(key) is not None:
            shot[key] = params[key]
    if params.get("elevation") is not None and params.get("eye_height") is None:
        shot.pop("eye_height", None)
    front = params.get("front_direction", "-y")
    if front not in FRONT_DIRECTIONS:
        raise ValueError("front_direction must be one of: " + ", ".join(FRONT_DIRECTIONS))

    lo = [min(getattr(n.min, a) for n in subjects) for a in "xyz"]
    hi = [max(getattr(n.max, a) for n in subjects) for a in "xyz"]
    centre = [(lo[k] + hi[k]) / 2.0 for k in range(3)]
    corners = [[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    radius = max(math.sqrt(sum((hi[k] - lo[k]) ** 2 for k in range(3))) / 2.0, 1e-3)

    resolution = params.get("resolution")
    if resolution:
        width_px, height_px = int(resolution[0]), int(resolution[1])
    else:
        width_px, height_px = int(rt.renderWidth), int(rt.renderHeight)
    aspect = width_px / float(height_px)
    focal = float(shot["focal_length_mm"])
    fill = float(shot.get("fill", 0.85))
    tan_h = (FILM_WIDTH_MM / 2.0 / focal) * fill
    tan_v = tan_h / aspect

    azimuth = math.radians(float(shot["azimuth"]) + FRONT_DIRECTIONS[front])
    level = "eye_height" in shot
    if level:
        camera_z = lo[2] + _length(shot["eye_height"], "eye_height")
        direction = [math.sin(azimuth), -math.cos(azimuth), 0.0]
    else:
        elevation = math.radians(float(shot["elevation"]))
        direction = [math.sin(azimuth) * math.cos(elevation), -math.cos(azimuth) * math.cos(elevation),
                     math.sin(elevation)]

    def camera_at(distance):
        p = [centre[k] + direction[k] * distance for k in range(3)]
        if level:
            p[2] = camera_z
        return p

    near, far = radius * 0.05, radius * 2000.0
    if not _fits(camera_at(far), centre, corners, tan_h, tan_v, level):
        raise ValueError("Could not frame the subject from this angle - try another shot or a wider lens")
    for _ in range(60):
        mid = (near + far) / 2.0
        if _fits(camera_at(mid), centre, corners, tan_h, tan_v, level):
            far = mid
        else:
            near = mid
    position = camera_at(far)

    camera = rt.Physical_Camera(pos=rt.Point3(*position), target=rt.Targetobject(pos=rt.Point3(*centre)))
    camera.targeted = True
    camera.film_width_mm = FILM_WIDTH_MM
    camera.specify_fov = False
    camera.focal_length_mm = focal
    camera.auto_vertical_tilt_correction = level
    camera.name = _unique_name(params.get("name") or "AI_Cam_" + shot_name)
    camera.target.name = camera.name + ".Target"

    if resolution and params.get("set_render_size", True):
        rt.renderSceneDialog.commit()
        rt.renderWidth, rt.renderHeight = width_px, height_px
        rt.renderSceneDialog.update()
    if params.get("make_active", True):
        rt.viewport.setCamera(camera)
        rt.displaySafeFrames = True
        rt.completeRedraw()

    return {
        "camera": camera.name,
        "shot": shot_name,
        "position": _round_list(position),
        "target": _round_list(centre),
        "distance": round(far, 3),
        "focal_length_mm": focal,
        "aspect": round(aspect, 4),
        "render_size": [int(rt.renderWidth), int(rt.renderHeight)],
        "vertical_tilt_correction": level,
        "subjects": [n.name for n in subjects],
        "next": "Call capture_viewport to check the framing.",
    }


ACTIONS = {
    "get_scene_state": action_get_scene_state,
    "list_selection": action_list_selection,
    "list_materials": action_list_materials,
    "apply_material": action_apply_material,
    "execute_maxscript": action_execute_maxscript,
    "create_object": action_create_object,
    "create_spline": action_create_spline,
    "transform_objects": action_transform_objects,
    "get_object_properties": action_get_object_properties,
    "set_object_properties": action_set_object_properties,
    "add_modifier": action_add_modifier,
    "clone_objects": action_clone_objects,
    "delete_objects": action_delete_objects,
    "capture_viewport": action_capture_viewport,
    "create_reference_plane": action_create_reference_plane,
    "copy_along_spline": action_copy_along_spline,
    "analyze_scene": action_analyze_scene,
    "optimize_scene": action_optimize_scene,
    "create_camera_view": action_create_camera_view,
}


# ---------------------------------------------------------------------------
# Tool permissions and activity feed
#
# The user turns tools on/off in the AI Bridge panel. The check happens here,
# inside Max, rather than in the MCP server: Max is the side the user
# controls, so a disabled tool stays disabled whatever client connects.
# Settings live in Max's plugcfg folder so they survive reinstalls.
# ---------------------------------------------------------------------------

_settings_lock = threading.Lock()
_tool_enabled = {name: meta["default_enabled"] for name, meta in TOOLS.items()}
_settings_path = None
_observers = []


def _get_settings_path():
    # rt.getDir must run on Max's main thread; callers (start_listener, the
    # panel) are on it, and the result is cached for everyone else.
    global _settings_path
    if _settings_path is None:
        _settings_path = os.path.join(str(rt.getDir(rt.Name("plugcfg"))), SETTINGS_FILE_NAME)
    return _settings_path


def load_settings():
    """Load saved tool on/off states. Unknown or missing tools keep their defaults."""
    try:
        with open(_get_settings_path(), encoding="utf-8") as f:
            saved = json.load(f).get("tools", {})
    except (OSError, ValueError, AttributeError):
        return
    with _settings_lock:
        for name, enabled in saved.items():
            if name in _tool_enabled and isinstance(enabled, bool):
                _tool_enabled[name] = enabled


def _save_settings():
    with _settings_lock:
        data = {"tools": dict(_tool_enabled)}
    path = _get_settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_tool_states():
    """Return {tool_name: enabled} for every tool."""
    with _settings_lock:
        return dict(_tool_enabled)


def set_tool_states(states):
    """Turn tools on/off ({tool_name: bool}) and save. Unknown names raise ValueError."""
    unknown = [name for name in states if name not in TOOLS]
    if unknown:
        raise ValueError(f"Unknown tool(s): {unknown}")
    with _settings_lock:
        for name, enabled in states.items():
            _tool_enabled[name] = bool(enabled)
    _save_settings()


def is_tool_enabled(name):
    with _settings_lock:
        return _tool_enabled.get(name, False)


def add_observer(callback):
    """
    Register callback(event) for every request the listener handles. event is
    a dict: {"time": float, "action": str, "status": "ok"|"error"|"blocked",
    "detail": str}. Called on the listener's worker thread - UI code must
    hand it over to its own thread (the panel uses a Qt signal).
    """
    if callback not in _observers:
        _observers.append(callback)


def remove_observer(callback):
    if callback in _observers:
        _observers.remove(callback)


def _notify(action, status, detail=""):
    event = {"time": time.time(), "action": action, "status": status, "detail": detail}
    for callback in list(_observers):
        try:
            callback(event)
        except Exception:
            pass  # a broken observer must never break a tool call


# ---------------------------------------------------------------------------
# Running tools on 3ds Max's main thread
#
# Requests arrive on socket worker threads. pymxs.mxstoken() would let a
# worker call into Max while the main thread waits, but anything touching
# graphics or UI from a worker (viewport textures, Qt windows) can crash or
# freeze Max. So every tool runs on the main thread instead: the worker posts
# the job to Qt's event loop (a queued signal to an object living on the main
# thread) and waits for it to finish.
# ---------------------------------------------------------------------------

if QtCore is not None:
    class _MainThreadDispatcher(QtCore.QObject):
        submit = QtCore.Signal(object)

        def __init__(self):
            super().__init__()
            self.submit.connect(self._run, QtCore.Qt.QueuedConnection)

        @QtCore.Slot(object)
        def _run(self, job):
            job()

_dispatcher = None


def _ensure_dispatcher():
    global _dispatcher
    if _dispatcher is not None or QtCore is None:
        return
    app = QtCore.QCoreApplication.instance()
    if app is None:
        return
    _dispatcher = _MainThreadDispatcher()
    _dispatcher.moveToThread(app.thread())


def _run_on_main_thread(fn):
    if _dispatcher is None:
        with pymxs.mxstoken():
            return fn()

    done = threading.Event()
    outcome = {}

    def job():
        try:
            outcome["result"] = fn()
        except BaseException as e:  # handed back to the waiting worker
            outcome["error"] = e
        finally:
            done.set()

    _dispatcher.submit.emit(job)
    if not done.wait(MAIN_THREAD_TIMEOUT_SECONDS):
        raise TimeoutError("3ds Max stayed busy (rendering, a modal dialog or a long operation) for "
                           f"{MAIN_THREAD_TIMEOUT_SECONDS}s. The request may still run when Max is free.")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def _call_handler(action_name, handler, params):
    if TOOLS[action_name]["risk"] == "read":
        return handler(params)
    # One Ctrl+Z in 3ds Max undoes the whole tool call. pymxs.undo swallows
    # exceptions, so carry them out of the block by hand.
    error, result = None, None
    with pymxs.undo(True, "AI Bridge: " + action_name):
        try:
            result = handler(params)
        except Exception as e:
            error = e
    if error is not None:
        raise error
    return result


# ---------------------------------------------------------------------------
# Socket server
# ---------------------------------------------------------------------------

def _handle_request(raw_line):
    try:
        request = json.loads(raw_line)
    except json.JSONDecodeError as e:
        return json.dumps({"id": None, "ok": False, "error": f"Invalid JSON: {e}"})

    req_id = request.get("id")
    action_name = request.get("action")
    params = request.get("params", {}) or {}

    handler = ACTIONS.get(action_name)
    if handler is None:
        return json.dumps({
            "id": req_id,
            "ok": False,
            "error": f"Unknown action '{action_name}'. Available: {list(ACTIONS.keys())}",
        })

    if not is_tool_enabled(action_name):
        label = TOOLS.get(action_name, {}).get("label", action_name)
        _notify(action_name, "blocked")
        return json.dumps({
            "id": req_id,
            "ok": False,
            "error": (
                f"The '{action_name}' tool is turned off by the user in 3ds Max "
                f"(AI Bridge panel > \"{label}\"). Do not work around this with other "
                "tools - tell the user, and they can turn it on in the panel "
                "(AI Bridge menu in 3ds Max) if they want you to use it."
            ),
        })

    try:
        # pymxs is not thread-safe and this runs on a socket worker thread:
        # hand the call to Max's main thread (see _run_on_main_thread).
        result = _run_on_main_thread(lambda: _call_handler(action_name, handler, params))
        _notify(action_name, "ok")
        return json.dumps({"id": req_id, "ok": True, "result": result})
    except Exception as e:
        _notify(action_name, "error", f"{type(e).__name__}: {e}")
        return json.dumps({
            "id": req_id,
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        })


def _client_thread(conn, addr):
    buffer = ""
    try:
        with conn:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    response = _handle_request(line)
                    conn.sendall((response + "\n").encode("utf-8"))
    except ConnectionResetError:
        pass


_server_socket = None
_server_thread = None
_stop_flag = False
_listening_port = None


def is_running():
    return _server_thread is not None and _server_thread.is_alive()


def listening_port():
    """Port the listener is bound to, or None when stopped."""
    return _listening_port if is_running() else None


def start_listener(host=HOST, port=PORT):
    """Start the listener in a background thread so it doesn't block Max's UI."""
    global _server_socket, _server_thread, _stop_flag, _listening_port
    if is_running():
        print(f"[max_ai_listener] Already running on {host}:{port}")
        return

    load_settings()
    _ensure_dispatcher()
    _stop_flag = False
    _listening_port = port
    _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        _server_socket.bind((host, port))
        _server_socket.listen(5)
    except OSError:
        _server_socket.close()
        raise
    _server_socket.settimeout(1.0)  # allows clean shutdown checks

    def _serve():
        print(f"[max_ai_listener] Listening on {host}:{port}")
        while not _stop_flag:
            try:
                conn, addr = _server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=_client_thread, args=(conn, addr), daemon=True).start()
        print("[max_ai_listener] Stopped.")

    _server_thread = threading.Thread(target=_serve, daemon=True)
    _server_thread.start()


def stop_listener(wait_seconds=0.0):
    global _stop_flag, _server_socket
    _stop_flag = True
    if _server_socket is not None:
        _server_socket.close()
    if wait_seconds and _server_thread is not None:
        _server_thread.join(timeout=wait_seconds)


if __name__ == "__main__":
    start_listener()
