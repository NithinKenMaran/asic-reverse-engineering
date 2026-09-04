#!/usr/bin/env python3
"""Recover a standard-cell connectivity graph from the Jane Street puzzle GDS.

The layout keeps standard cells hierarchical.  We therefore flatten only top-level
routing and via references, then attach transformed local pin labels to the resulting
conductive components.  This deliberately avoids flattening transistor geometry.

Provenance: this is the self-contained, pure-stdlib GDS reader (no external
geometry library) from the jane-street-X toolchain. It is the primary netlist
extractor in this submission because it needs no reference netlist to complete
- it parses the GDS records directly, unions conductive rectangles, and attaches
labeled pins to the resulting nets. `emit_recovered.py` turns its output into
Verilog. `cross_check.py` compares its instance/port inventory against the
independent KLayout-based reader in `gdsconn.py`.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import re
import struct
from dataclasses import dataclass, field
from typing import Iterable


CONDUCTIVE_LAYERS = frozenset(range(67, 73))
POWER_PINS = frozenset({"VPWR", "VGND", "VPB", "VNB"})
PHYSICAL_CELL_PREFIXES = (
    "sky130_fd_sc_hd__tapvpwrvgnd_",
    "sky130_fd_sc_hd__decap_",
    "sky130_fd_sc_hd__diode_",
)


def _real8(payload: bytes) -> float:
    if payload == b"\0" * 8:
        return 0.0
    sign = -1 if payload[0] & 0x80 else 1
    exponent = (payload[0] & 0x7F) - 64
    mantissa = int.from_bytes(payload[1:], "big") / (1 << 56)
    return sign * mantissa * (16**exponent)


def _string(payload: bytes) -> str:
    return payload.rstrip(b"\0").decode("ascii", errors="replace")


def parse_gds(path: pathlib.Path) -> dict[str, list[dict]]:
    """Parse the small GDSII record subset used by these layouts."""
    data = path.read_bytes()
    position = 0
    structures: dict[str, list[dict]] = {}
    elements: list[dict] | None = None
    element: dict | None = None
    starts = {
        0x08: "boundary",
        0x09: "path",
        0x0A: "sref",
        0x0B: "aref",
        0x0C: "text",
        0x15: "node",
        0x2D: "box",
    }

    while position < len(data):
        size, record_type, _data_type = struct.unpack_from(">HBB", data, position)
        if size < 4 or position + size > len(data):
            raise ValueError(f"invalid GDS record at byte {position}: length={size}")
        payload = data[position + 4 : position + size]
        position += size

        if record_type == 0x05:  # BGNSTR
            elements = []
        elif record_type == 0x06:  # STRNAME
            if elements is None:
                raise ValueError("STRNAME outside BGNSTR")
            structures[_string(payload)] = elements
        elif record_type in starts:
            if elements is None:
                raise ValueError("element outside structure")
            element = {"kind": starts[record_type]}
            elements.append(element)
        elif element is not None:
            if record_type == 0x0D:  # LAYER
                element["layer"] = struct.unpack(">h", payload)[0]
            elif record_type == 0x0E:  # DATATYPE
                element["datatype"] = struct.unpack(">h", payload)[0]
            elif record_type == 0x0F:  # WIDTH
                element["width"] = struct.unpack(">i", payload)[0]
            elif record_type == 0x10:  # XY
                element["xy"] = list(struct.iter_unpack(">ii", payload))
            elif record_type == 0x11:  # ENDEL
                element = None
            elif record_type == 0x12:  # SNAME
                element["sname"] = _string(payload)
            elif record_type == 0x16:  # TEXTTYPE
                element["texttype"] = struct.unpack(">h", payload)[0]
            elif record_type == 0x19:  # STRING
                element["string"] = _string(payload)
            elif record_type == 0x1A:  # STRANS
                element["strans"] = struct.unpack(">H", payload)[0]
            elif record_type == 0x1B:  # MAG
                element["mag"] = _real8(payload)
            elif record_type == 0x1C:  # ANGLE
                element["angle"] = _real8(payload)
            elif record_type == 0x21:  # PATHTYPE
                element["pathtype"] = struct.unpack(">h", payload)[0]
            elif record_type == 0x30:  # BGNEXTN
                element["bgnextn"] = struct.unpack(">i", payload)[0]
            elif record_type == 0x31:  # ENDEXTN
                element["endextn"] = struct.unpack(">i", payload)[0]

    return structures


def transform_point(point: tuple[int, int], reference: dict) -> tuple[int, int]:
    """Apply GDS reflection, rotation, magnification, and translation."""
    x, y = point
    if reference.get("strans", 0) & 0x8000:
        y = -y
    magnification = reference.get("mag", 1.0)
    x *= magnification
    y *= magnification
    angle = math.radians(reference.get("angle", 0.0))
    cosine = round(math.cos(angle))
    sine = round(math.sin(angle))
    x, y = x * cosine - y * sine, x * sine + y * cosine
    origin_x, origin_y = reference["xy"][0]
    return round(x + origin_x), round(y + origin_y)


def _bounds(points: Iterable[tuple[int, int]]) -> tuple[int, int, int, int]:
    points = list(points)
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def cell_bounds(elements: list[dict]) -> tuple[int, int, int, int]:
    outlines = [
        element
        for element in elements
        if element["kind"] == "boundary"
        and element.get("layer") == 236
        and element.get("datatype") == 0
    ]
    if len(outlines) != 1:
        raise ValueError(f"expected one layer-236 cell outline, found {len(outlines)}")
    return _bounds(outlines[0]["xy"])


def transformed_cell_bounds(elements: list[dict], reference: dict) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = cell_bounds(elements)
    corners = [(x1, y1), (x1, y2), (x2, y1), (x2, y2)]
    return _bounds(transform_point(corner, reference) for corner in corners)


def path_bounds(element: dict) -> tuple[int, int, int, int]:
    points = element.get("xy", [])
    if len(points) != 2:
        raise ValueError(f"only two-point paths are supported, got {len(points)}")
    (x1, y1), (x2, y2) = points
    if x1 != x2 and y1 != y2:
        raise ValueError(f"non-Manhattan path: {points}")
    width = element.get("width", 0)
    half = width // 2
    path_type = element.get("pathtype", 0)
    if path_type == 2:
        begin_extension = end_extension = half
    elif path_type == 4:
        begin_extension = element.get("bgnextn", 0)
        end_extension = element.get("endextn", 0)
    else:
        begin_extension = end_extension = 0

    if y1 == y2:
        direction = 1 if x2 >= x1 else -1
        start = x1 - direction * begin_extension
        end = x2 + direction * end_extension
        return min(start, end), y1 - half, max(start, end), y1 + half
    direction = 1 if y2 >= y1 else -1
    start = y1 - direction * begin_extension
    end = y2 + direction * end_extension
    return x1 - half, min(start, end), x1 + half, max(start, end)


def orthogonal_polygon_rectangles(points: list[tuple[int, int]]) -> list[tuple[int, int, int, int]]:
    """Decompose a simple Manhattan polygon into exact vertical-slab rectangles."""
    if len(points) < 4 or points[0] != points[-1]:
        raise ValueError("GDS boundary is not a closed polygon")
    for start, end in zip(points, points[1:]):
        if start[0] != end[0] and start[1] != end[1]:
            raise ValueError(f"non-Manhattan polygon edge: {start} -> {end}")
    xs = sorted({point[0] for point in points})
    rectangles: list[tuple[int, int, int, int]] = []
    for left, right in zip(xs, xs[1:]):
        middle = (left + right) / 2
        crossings: list[int] = []
        for start, end in zip(points, points[1:]):
            if start[1] != end[1]:
                continue
            edge_left, edge_right = sorted((start[0], end[0]))
            if edge_left < middle < edge_right:
                crossings.append(start[1])
        crossings.sort()
        if len(crossings) % 2:
            raise ValueError(f"odd polygon crossings in slab {left}..{right}: {crossings}")
        for bottom, top in zip(crossings[0::2], crossings[1::2]):
            rectangles.append((left, bottom, right, top))
    return rectangles


class UnionFind:
    def __init__(self) -> None:
        self.parent: list[int] = []
        self.rank: list[int] = []

    def add(self) -> int:
        index = len(self.parent)
        self.parent.append(index)
        self.rank.append(0)
        return index

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        left = self.find(left)
        right = self.find(right)
        if left == right:
            return
        if self.rank[left] < self.rank[right]:
            left, right = right, left
        self.parent[right] = left
        if self.rank[left] == self.rank[right]:
            self.rank[left] += 1


@dataclass
class Shape:
    layer: int
    bounds: tuple[int, int, int, int]
    node: int
    description: str


@dataclass
class Instance:
    name: str
    cell: str
    bounds: tuple[int, int, int, int]
    reference: dict
    pin_nodes: dict[str, int] = field(default_factory=dict)
    pin_points: dict[str, list[tuple[int, int]]] = field(default_factory=dict)


@dataclass
class Extracted:
    instances: list[Instance]
    top_pin_nodes: dict[str, int]
    union_find: UnionFind
    shapes: list[Shape]

    def net_for_node(self, node: int) -> int:
        return self.union_find.find(node)

    def as_dict(self) -> dict:
        roots = sorted({self.net_for_node(node) for node in range(len(self.union_find.parent))})
        root_names = {root: f"n{index}" for index, root in enumerate(roots)}
        for pin, node in sorted(self.top_pin_nodes.items()):
            root_names[self.net_for_node(node)] = pin
        return {
            "instances": [
                {
                    "name": instance.name,
                    "cell": instance.cell,
                    "bounds": instance.bounds,
                    "pins": {
                        pin: root_names[self.net_for_node(node)]
                        for pin, node in sorted(instance.pin_nodes.items())
                    },
                }
                for instance in self.instances
            ],
            "ports": {
                pin: root_names[self.net_for_node(node)]
                for pin, node in sorted(self.top_pin_nodes.items())
            },
        }


def _logical_cell(name: str) -> bool:
    return name.startswith("sky130_fd_sc_hd__") and not name.startswith(PHYSICAL_CELL_PREFIXES)


def _pin_labels(elements: list[dict]) -> dict[str, list[tuple[int, int]]]:
    labels: dict[str, list[tuple[int, int]]] = collections.defaultdict(list)
    for element in elements:
        if (
            element["kind"] == "text"
            and element.get("layer") == 67
            and element.get("texttype") == 5
            and element.get("string") not in POWER_PINS
        ):
            labels[element["string"]].extend(element.get("xy", []))
    return dict(labels)


def _pin_geometries(elements: list[dict]) -> dict[str, list[tuple[int, tuple[int, int, int, int]]]]:
    """Return labeled local-interconnect rectangles grouped by logical pin.

    Pin text is often some distance from the router's chosen access via.  The
    layer-67 conductor joins them, so using only text points would report opens.
    We recover just the labeled conductor components, not arbitrary cell internals.
    """
    rectangles: list[tuple[int, tuple[int, int, int, int]]] = []
    for element in elements:
        layer = element.get("layer")
        if layer not in (67, 68) or element.get("datatype") != 20:
            continue
        if element["kind"] == "boundary":
            rectangles.extend(
                (layer, rectangle)
                for rectangle in orthogonal_polygon_rectangles(element["xy"])
            )
        elif element["kind"] == "path":
            rectangles.append((layer, path_bounds(element)))

    union_find = UnionFind()
    nodes = [union_find.add() for _rectangle in rectangles]
    for left, (left_layer, left_rectangle) in enumerate(rectangles):
        lx1, ly1, lx2, ly2 = left_rectangle
        for right in range(left):
            right_layer, right_rectangle = rectangles[right]
            rx1, ry1, rx2, ry2 = right_rectangle
            if left_layer == right_layer and lx2 >= rx1 and rx2 >= lx1 and ly2 >= ry1 and ry2 >= ly1:
                union_find.union(nodes[left], nodes[right])

    # Local mcon cuts join li1 (67) to met1 (68).
    for element in elements:
        if (
            element["kind"] != "boundary"
            or element.get("layer") != 67
            or element.get("datatype") != 44
        ):
            continue
        cut = _bounds(element["xy"])
        cx1, cy1, cx2, cy2 = cut
        matches = []
        for index, (_layer, (x1, y1, x2, y2)) in enumerate(rectangles):
            if cx2 >= x1 and x2 >= cx1 and cy2 >= y1 and y2 >= cy1:
                matches.append(index)
        if matches:
            first = matches[0]
            for index in matches[1:]:
                union_find.union(nodes[first], nodes[index])

    root_to_pins: dict[int, set[str]] = collections.defaultdict(set)
    labels = _pin_labels(elements)
    for pin, points in labels.items():
        for x, y in points:
            matches = [
                index
                for index, (layer, (x1, y1, x2, y2)) in enumerate(rectangles)
                if layer == 67 and x1 <= x <= x2 and y1 <= y <= y2
            ]
            if not matches:
                raise ValueError(f"pin label {pin}@({x},{y}) is outside layer-67 pin geometry")
            for index in matches:
                root_to_pins[union_find.find(nodes[index])].add(pin)

    result: dict[str, list[tuple[int, tuple[int, int, int, int]]]] = collections.defaultdict(list)
    for index, rectangle in enumerate(rectangles):
        pins = root_to_pins.get(union_find.find(nodes[index]), set())
        if len(pins) > 1:
            raise ValueError(f"local conductor connects distinct labeled pins: {sorted(pins)}")
        for pin in pins:
            result[pin].append(rectangle)
    for pin in labels:
        if pin not in result:
            raise ValueError(f"no local conductor recovered for pin {pin}")
    return dict(result)


def extract_gds(path: pathlib.Path, top: str) -> Extracted:
    structures = parse_gds(path)
    if top not in structures:
        raise ValueError(f"top structure {top!r} not found")
    top_elements = structures[top]
    union_find = UnionFind()
    shapes: list[Shape] = []
    by_layer: dict[int, list[Shape]] = collections.defaultdict(list)

    def add_shape(layer: int, bounds: tuple[int, int, int, int], description: str, node: int | None = None) -> int:
        if node is None:
            node = union_find.add()
        shape = Shape(layer, bounds, node, description)
        shapes.append(shape)
        by_layer[layer].append(shape)
        return node

    # Top-level route drawing and IO pin rectangles.
    for index, element in enumerate(top_elements):
        layer = element.get("layer")
        if layer not in CONDUCTIVE_LAYERS:
            continue
        if element["kind"] == "path" and element.get("datatype") == 20:
            add_shape(layer, path_bounds(element), f"top path {index}")
        elif element["kind"] == "boundary" and element.get("datatype") in (16, 20):
            add_shape(layer, _bounds(element["xy"]), f"top boundary {index}")

    # Flatten only conductive pads in via-like references.  Standard-cell
    # transistor/interconnect geometry intentionally remains hierarchical.
    for index, reference in enumerate(top_elements):
        if reference["kind"] != "sref" or _logical_cell(reference.get("sname", "")):
            continue
        child_name = reference.get("sname")
        child = structures.get(child_name, [])
        via_node: int | None = None
        for child_element in child:
            layer = child_element.get("layer")
            if (
                child_element["kind"] == "boundary"
                and layer in CONDUCTIVE_LAYERS
                and child_element.get("datatype") == 20
            ):
                if via_node is None:
                    via_node = union_find.add()
                transformed = [transform_point(point, reference) for point in child_element["xy"]]
                add_shape(layer, _bounds(transformed), f"{child_name} ref {index}", node=via_node)

    instances: list[Instance] = []
    for index, reference in enumerate(top_elements):
        if reference["kind"] != "sref" or not _logical_cell(reference.get("sname", "")):
            continue
        cell = reference["sname"]
        instance = Instance(
            name=f"u{index}",
            cell=cell,
            bounds=transformed_cell_bounds(structures[cell], reference),
            reference=reference,
        )
        local_pin_points = _pin_labels(structures[cell])
        for pin, local_rectangles in _pin_geometries(structures[cell]).items():
            pin_node = union_find.add()
            instance.pin_nodes[pin] = pin_node
            instance.pin_points[pin] = [
                transform_point(point, reference) for point in local_pin_points[pin]
            ]
            for layer, rectangle in local_rectangles:
                x1, y1, x2, y2 = rectangle
                transformed = [
                    transform_point(point, reference)
                    for point in ((x1, y1), (x1, y2), (x2, y1), (x2, y2))
                ]
                add_shape(layer, _bounds(transformed), f"{instance.name}.{pin} conductor", node=pin_node)
            for point in instance.pin_points[pin]:
                add_shape(67, (*point, *point), f"{instance.name}.{pin}", node=pin_node)
        instances.append(instance)

    top_pin_nodes: dict[str, int] = {}
    for index, element in enumerate(top_elements):
        if (
            element["kind"] != "text"
            or element.get("texttype") != 5
            or element.get("layer") not in CONDUCTIVE_LAYERS
            or element.get("string") in POWER_PINS
        ):
            continue
        pin = element["string"]
        if pin not in top_pin_nodes:
            top_pin_nodes[pin] = union_find.add()
        node = top_pin_nodes[pin]
        for point in element.get("xy", []):
            add_shape(element["layer"], (*point, *point), f"top pin {pin} text {index}", node=node)

    # Exact rectangle intersection sweep.  Coordinates already include path
    # extensions and via enclosures, so touching rectangles are conductive.
    for layer, layer_shapes in sorted(by_layer.items()):
        ordered = sorted(layer_shapes, key=lambda shape: (shape.bounds[0], shape.bounds[1]))
        active: list[Shape] = []
        for current in ordered:
            x1, y1, x2, y2 = current.bounds
            active = [candidate for candidate in active if candidate.bounds[2] >= x1]
            for candidate in active:
                cx1, cy1, cx2, cy2 = candidate.bounds
                if cy2 >= y1 and y2 >= cy1 and cx2 >= x1 and x2 >= cx1:
                    union_find.union(current.node, candidate.node)
            active.append(current)

    return Extracted(instances, top_pin_nodes, union_find, shapes)


def parse_def_components(path: pathlib.Path) -> dict[tuple[str, int, int], str]:
    text = path.read_text()
    section = text.split("COMPONENTS", 1)[1].split("END COMPONENTS", 1)[0]
    result: dict[tuple[str, int, int], str] = {}
    pattern = re.compile(
        r"^\s*-\s+(\S+)\s+(\S+).*?\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+(\S+)\s*;",
        re.MULTILINE | re.DOTALL,
    )
    for match in pattern.finditer(section):
        name, cell, x, y, _orientation = match.groups()
        key = cell, int(x), int(y)
        if key in result:
            raise ValueError(f"duplicate DEF placement key {key}")
        result[key] = name
    return result


def assign_def_names(extracted: Extracted, def_path: pathlib.Path) -> list[str]:
    components = parse_def_components(def_path)
    errors: list[str] = []
    for instance in extracted.instances:
        x1, y1, _x2, _y2 = instance.bounds
        name = components.get((instance.cell, x1, y1))
        if name is None:
            errors.append(f"no DEF component for {instance.cell} at ({x1},{y1})")
        else:
            instance.name = name
    return errors


def parse_def_nets(path: pathlib.Path) -> dict[str, set[tuple[str, str]]]:
    text = path.read_text()
    section = text.split("NETS", 1)[1].split("END NETS", 1)[0]
    result: dict[str, set[tuple[str, str]]] = {}
    for match in re.finditer(r"^\s*-\s+(\S+)\s+(.*?)\s*;", section, re.MULTILINE | re.DOTALL):
        net_name, body = match.groups()
        terminals = body.split("+", 1)[0]
        endpoints = set(re.findall(r"\(\s*(\S+)\s+(\S+)\s*\)", terminals))
        result[net_name] = endpoints
    return result


def validate_def(extracted: Extracted, def_path: pathlib.Path) -> dict:
    naming_errors = assign_def_names(extracted, def_path)
    expected_nets = parse_def_nets(def_path)
    actual_nodes: dict[tuple[str, str], int] = {}
    for instance in extracted.instances:
        for pin, node in instance.pin_nodes.items():
            actual_nodes[(instance.name, pin)] = extracted.net_for_node(node)
    for pin, node in extracted.top_pin_nodes.items():
        actual_nodes[("PIN", pin)] = extracted.net_for_node(node)

    opens: dict[str, list[int]] = {}
    missing: list[tuple[str, str, str]] = []
    root_to_expected: dict[int, set[str]] = collections.defaultdict(set)
    compared_endpoints = 0
    for net, endpoints in expected_nets.items():
        roots: set[int] = set()
        for endpoint in endpoints:
            if endpoint not in actual_nodes:
                missing.append((net, *endpoint))
                continue
            compared_endpoints += 1
            root = actual_nodes[endpoint]
            roots.add(root)
            root_to_expected[root].add(net)
        if len(roots) > 1:
            opens[net] = sorted(roots)
    shorts = {
        str(root): sorted(nets)
        for root, nets in root_to_expected.items()
        if len(nets) > 1
    }
    return {
        "instances": len(extracted.instances),
        "expected_nets": len(expected_nets),
        "compared_endpoints": compared_endpoints,
        "naming_errors": naming_errors,
        "missing_endpoints": missing,
        "open_nets": opens,
        "shorted_nets": shorts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("gds", type=pathlib.Path)
    parser.add_argument("--top", required=True)
    parser.add_argument("--def-file", type=pathlib.Path)
    parser.add_argument("--json", action="store_true", help="include recovered netlist JSON")
    args = parser.parse_args()

    extracted = extract_gds(args.gds, args.top)
    result: dict = {
        "gds": str(args.gds),
        "top": args.top,
        "instances": len(extracted.instances),
        "ports": sorted(extracted.top_pin_nodes),
        "shape_count": len(extracted.shapes),
        "net_count_with_geometry_fragments": len(
            {extracted.net_for_node(node) for node in range(len(extracted.union_find.parent))}
        ),
    }
    if args.def_file:
        result["def_validation"] = validate_def(extracted, args.def_file)
    if args.json:
        result["netlist"] = extracted.as_dict()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
