#!/usr/bin/env python3
"""Emit synthesis/formal-friendly Verilog from the recovered GDS connectivity.

Provenance: from the jane-street-X toolchain, used unmodified. It consumes the
connectivity graph produced by `gds_netlist.py` and the official Sky130 HD
Liberty JSON models (each output pin's boolean `function`, each flip-flop's
`ff` clock/next-state/clear/preset description) to write a real Verilog netlist.

Crucially this path is self-contained: it does NOT read any pre-existing
recovered netlist. It checks for multiple drivers, reports dangling (unread)
outputs, and deterministically ties the single physically-undriven output-only
control net low - so `puzzle.gds` alone (plus the public Sky130 Liberty data)
is enough to regenerate `recovered_puzzle.v`.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
from dataclasses import dataclass

import gds_netlist


@dataclass
class CellModel:
    pins: dict[str, dict]
    sequential: dict | None


def liberty_path(root: pathlib.Path, cell: str) -> pathlib.Path:
    short = cell.split("__", 1)[1]
    base = re.sub(r"_[0-9]+$", "", short)
    return root / "cells" / base / f"{cell}__tt_025C_1v80.lib.json"


def load_models(root: pathlib.Path, cells: set[str]) -> dict[str, CellModel]:
    result = {}
    for cell in sorted(cells):
        path = liberty_path(root, cell)
        if not path.exists():
            raise FileNotFoundError(f"no liberty JSON for {cell}: {path}")
        liberty = json.loads(path.read_text())
        pins = {
            key.split(",", 1)[1]: value
            for key, value in liberty.items()
            if key.startswith("pin,")
        }
        sequential_entries = [
            value
            for key, value in liberty.items()
            if key.startswith("ff,") or key.startswith("latch,")
        ]
        if len(sequential_entries) > 1:
            raise ValueError(f"multiple sequential descriptions for {cell}")
        result[cell] = CellModel(pins, sequential_entries[0] if sequential_entries else None)
    return result


def substitute_function(function: str, pin_signals: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        return pin_signals.get(token, token)

    expression = re.sub(r"\b[A-Za-z_][A-Za-z0-9_]*\b", replace, function)
    return expression.replace("!", "~")


def vector_groups(port_directions: dict[str, str]) -> tuple[dict[str, tuple[str, int, int]], set[str]]:
    candidates: dict[tuple[str, str], set[int]] = collections.defaultdict(set)
    for port, direction in port_directions.items():
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]", port)
        if match:
            candidates[(match.group(1), direction)].add(int(match.group(2)))
    groups: dict[str, tuple[str, int, int]] = {}
    grouped: set[str] = set()
    for (base, direction), indexes in sorted(candidates.items()):
        low, high = min(indexes), max(indexes)
        if indexes == set(range(low, high + 1)) and len(indexes) > 1:
            groups[base] = direction, high, low
            grouped.update(f"{base}[{index}]" for index in indexes)
    return groups, grouped


def emit_verilog(
    extracted: gds_netlist.Extracted,
    models: dict[str, CellModel],
    module_name: str,
) -> tuple[str, dict]:
    union_find = extracted.union_find
    root = extracted.net_for_node
    relevant_roots = {
        root(node)
        for instance in extracted.instances
        for node in instance.pin_nodes.values()
    } | {root(node) for node in extracted.top_pin_nodes.values()}

    drivers: dict[int, list[str]] = collections.defaultdict(list)
    loads: dict[int, list[str]] = collections.defaultdict(list)
    instance_pins: dict[str, dict[str, int]] = {}
    sequential_count = 0
    for instance in extracted.instances:
        model = models[instance.cell]
        missing = set(model.pins) - set(instance.pin_nodes)
        extra = set(instance.pin_nodes) - set(model.pins)
        if missing or extra:
            raise ValueError(
                f"pin mismatch for {instance.name}/{instance.cell}: "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )
        instance_pins[instance.name] = {
            pin: root(node) for pin, node in instance.pin_nodes.items()
        }
        for pin, pin_model in model.pins.items():
            endpoint = f"{instance.name}.{pin}"
            net = instance_pins[instance.name][pin]
            if pin_model.get("direction") == "output":
                drivers[net].append(endpoint)
            else:
                loads[net].append(endpoint)
        if model.sequential:
            sequential_count += 1

    port_directions: dict[str, str] = {}
    for port, node in extracted.top_pin_nodes.items():
        net = root(node)
        port_directions[port] = "output" if drivers[net] else "input"
        if port_directions[port] == "input":
            drivers[net].append(f"PORT.{port}")
        else:
            loads[net].append(f"PORT.{port}")

    multiple_drivers = {
        str(net): endpoints for net, endpoints in drivers.items() if len(endpoints) > 1
    }
    undriven_loaded = {
        str(net): endpoints
        for net, endpoints in loads.items()
        if not drivers.get(net)
    }
    if multiple_drivers:
        raise ValueError(
            "logical connectivity sanity check failed: "
            f"multiple_drivers={multiple_drivers}"
        )

    root_to_port = {
        root(node): port for port, node in extracted.top_pin_nodes.items()
    }

    def signal(net: int) -> str:
        port = root_to_port.get(net)
        if port:
            return port
        return f"n{net}"

    vector_ports, grouped_ports = vector_groups(port_directions)
    scalar_ports = sorted(set(port_directions) - grouped_ports)
    module_ports = scalar_ports + sorted(vector_ports)
    lines = ["`default_nettype none", f"module {module_name}(", "    " + ",\n    ".join(module_ports), ");"]
    for port in scalar_ports:
        lines.append(f"  {port_directions[port]} wire {port};")
    for port, (direction, high, low) in sorted(vector_ports.items()):
        lines.append(f"  {direction} wire [{high}:{low}] {port};")

    internal_roots = sorted(relevant_roots - set(root_to_port))
    if internal_roots:
        lines.append("  wire " + ", ".join(signal(net) for net in internal_roots) + ";")
    for net in sorted(int(net) for net in undriven_loaded):
        lines.append(
            f"  assign {signal(net)} = 1'b0; // physically undriven; tied low deterministically"
        )
    lines.append("")

    state_index = 0
    assignments = []
    always_blocks = []
    for instance in extracted.instances:
        model = models[instance.cell]
        pins = {
            pin: signal(net) for pin, net in instance_pins[instance.name].items()
        }
        location = f"({instance.bounds[0]},{instance.bounds[1]})"
        if model.sequential:
            sequential = model.sequential
            state = f"state_{state_index}"
            state_index += 1
            output_pins = [
                pin for pin, value in model.pins.items() if value.get("direction") == "output"
            ]
            if output_pins != ["Q"]:
                raise ValueError(f"unsupported sequential outputs for {instance.cell}: {output_pins}")
            assignments.append(f"  reg {state};")
            assignments.append(f"  assign {pins['Q']} = {state}; // {instance.cell} {location}")
            clock = pins[sequential["clocked_on"]]
            data = pins[sequential["next_state"]]
            if "clear" in sequential:
                match = re.fullmatch(r"!(\w+)", sequential["clear"])
                if not match:
                    raise ValueError(f"unsupported clear expression: {sequential['clear']}")
                reset_pin = match.group(1)
                reset = pins[reset_pin]
                always_blocks.extend(
                    [
                        f"  always @(posedge {clock} or negedge {reset}) begin",
                        f"    if (!{reset}) {state} <= 1'b0;",
                        f"    else {state} <= {data};",
                        "  end",
                    ]
                )
            elif "preset" in sequential:
                match = re.fullmatch(r"!(\w+)", sequential["preset"])
                if not match:
                    raise ValueError(f"unsupported preset expression: {sequential['preset']}")
                set_pin = match.group(1)
                set_signal = pins[set_pin]
                always_blocks.extend(
                    [
                        f"  always @(posedge {clock} or negedge {set_signal}) begin",
                        f"    if (!{set_signal}) {state} <= 1'b1;",
                        f"    else {state} <= {data};",
                        "  end",
                    ]
                )
            else:
                always_blocks.extend(
                    [
                        f"  always @(posedge {clock}) begin",
                        f"    {state} <= {data};",
                        "  end",
                    ]
                )
            continue

        for pin, pin_model in model.pins.items():
            if pin_model.get("direction") != "output":
                continue
            function = pin_model.get("function")
            if function is None:
                raise ValueError(f"no output function for {instance.cell}.{pin}")
            expression = substitute_function(function, pins)
            assignments.append(
                f"  assign {pins[pin]} = {expression}; // {instance.cell} {location}"
            )

    lines.extend(assignments)
    lines.append("")
    lines.extend(always_blocks)
    lines.extend(["endmodule", "`default_nettype wire", ""])

    dangling_outputs = {
        str(net): endpoints
        for net, endpoints in drivers.items()
        if not loads.get(net) and not root_to_port.get(net)
    }
    summary = {
        "instances": len(extracted.instances),
        "sequential_instances": sequential_count,
        "relevant_nets": len(relevant_roots),
        "ports": port_directions,
        "dangling_output_nets": dangling_outputs,
        "multiple_driver_nets": multiple_drivers,
        "undriven_loaded_nets": undriven_loaded,
    }
    return "\n".join(lines), summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("gds", type=pathlib.Path)
    parser.add_argument("--top", required=True)
    parser.add_argument("--sky130-root", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()

    extracted = gds_netlist.extract_gds(args.gds, args.top)
    models = load_models(args.sky130_root, {instance.cell for instance in extracted.instances})
    verilog, summary = emit_verilog(extracted, models, args.top)
    args.output.write_text(verilog)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
