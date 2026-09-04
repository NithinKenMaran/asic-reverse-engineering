"""
Basic connectivity extraction for a standard-cell GDS layout.

Approach (all empirically derived from 04_final.gds in a prior inspection pass,
not from PDK docs we don't have):
  - conductor layers are numeric GDS layers 67..72 = li1, met1, met2, met3, met4, met5
  - each VIA_* cell type bridges exactly two of those conductor layers; which two
    is derived from the via cell's own shapes (own_layers_of_via), not hardcoded
  - per-layer copper is a klayout Region unioning: the top cell's own shapes on
    that layer, plus every instance's leaf-cell shapes on that layer (transformed
    into top coordinates). Region.merged() gives touching-polygon groups ("blobs").
  - a union-find joins blobs across layers wherever a via instance's footprint
    overlaps a blob on its lower layer and a blob on its upper layer.
  - instance pins (li1/met1 datatype 16 shapes, named via nearby datatype 5 text
    labels) and top-level I/O pins (met3/met4/met5 datatype 16, named the same way)
    are then looked up against the final union-find to get a net id per pin.

Output: for every net with >=2 distinct endpoints, the list of endpoints
(IO pin name, or "<cell_type>#<instance_index>.<pin_name>").
"""
import sys
from collections import defaultdict
import klayout.db as db

PATH = sys.argv[1] if len(sys.argv) > 1 else "gds/04_final.gds"
CONDUCTOR_LAYERS = [67, 68, 69, 70, 71, 72]  # li1, met1, met2, met3, met4, met5
PIN_DATATYPE = 16
LABEL_DATATYPE = 5

layout = db.Layout()
layout.read(PATH)
top = layout.top_cells()[0]

li_by_tag = {}          # (gds_layer, datatype) -> layer index
layer_nums_present = defaultdict(list)  # gds_layer -> [layer indexes, any datatype]
for li in layout.layer_indexes():
    info = layout.get_info(li)
    li_by_tag[(info.layer, info.datatype)] = li
    layer_nums_present[info.layer].append(li)


def classify(cell_name):
    if cell_name.startswith("VIA_"):
        return "via"
    if "tapvpwrvgnd" in cell_name:
        return "tap"
    if "decap" in cell_name:
        return "decap"
    return "logic"


insts = list(top.each_inst())
print(f"[info] top-level instances: {len(insts)}")
kind_counts = defaultdict(int)
for inst in insts:
    kind_counts[classify(inst.cell.name)] += 1
print(f"[info] by kind: {dict(kind_counts)}")

# --- which two conductor layers does each via cell type bridge? ---
via_bridge = {}  # cell_name -> (layer_lo, layer_hi)
for inst in insts:
    name = inst.cell.name
    if not name.startswith("VIA_") or name in via_bridge:
        continue
    own = set()
    for li in layout.layer_indexes():
        if inst.cell.shapes(li).size():
            own.add(layout.get_info(li).layer)
    bridged = sorted(own & set(CONDUCTOR_LAYERS))
    if len(bridged) == 2:
        via_bridge[name] = tuple(bridged)
    else:
        print(f"[warn] via type {name}: expected 2 conductor layers, got {bridged}")

print("[info] via bridges (empirically derived):")
for name, pair in via_bridge.items():
    print(f"    {name}: met/li layer {pair[0]} <-> {pair[1]}")

# --- build per-layer copper regions ---
print("\n[info] building per-layer copper regions...")
region_by_layer = {n: db.Region() for n in CONDUCTOR_LAYERS}

for n in CONDUCTOR_LAYERS:
    for li in layer_nums_present[n]:
        if layout.get_info(li).datatype == LABEL_DATATYPE:
            continue  # text, not copper
        for shape in top.shapes(li).each():
            if shape.is_text():
                continue
            region_by_layer[n].insert(shape.polygon)

for inst in insts:
    t = inst.cplx_trans
    for n in CONDUCTOR_LAYERS:
        for li in layer_nums_present[n]:
            if layout.get_info(li).datatype == LABEL_DATATYPE:
                continue
            for shape in inst.cell.shapes(li).each():
                if shape.is_text():
                    continue
                region_by_layer[n].insert(shape.polygon.transformed(t))

merged_by_layer = {}
for n in CONDUCTOR_LAYERS:
    merged = region_by_layer[n].merged()
    merged_by_layer[n] = list(merged.each())
    print(f"    layer {n}: {len(merged_by_layer[n])} merged blobs")


def find_blob(layer_num, polygon):
    """Return index of the merged blob on layer_num touching this polygon, or None."""
    box = polygon.bbox()
    q = db.Region(polygon)
    for idx, poly in enumerate(merged_by_layer[layer_num]):
        if not poly.bbox().overlaps(box) and not poly.bbox().touches(box):
            continue
        if not db.Region(poly).interacting(q).is_empty():
            return idx
    return None


# --- union-find over (layer_num, blob_idx) ---
parent = {}


def find(x):
    parent.setdefault(x, x)
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[ra] = rb


print("\n[info] bridging layers through vias...")
unresolved_vias = 0
for inst in insts:
    name = inst.cell.name
    if name not in via_bridge:
        continue
    lo, hi = via_bridge[name]
    t = inst.cplx_trans
    # union across every shape the via itself carries, on both bridged layers
    lo_blob = hi_blob = None
    for li in layer_nums_present[lo]:
        if layout.get_info(li).datatype == LABEL_DATATYPE:
            continue
        for shape in inst.cell.shapes(li).each():
            if shape.is_text():
                continue
            b = find_blob(lo, shape.polygon.transformed(t))
            if b is not None:
                lo_blob = b
    for li in layer_nums_present[hi]:
        if layout.get_info(li).datatype == LABEL_DATATYPE:
            continue
        for shape in inst.cell.shapes(li).each():
            if shape.is_text():
                continue
            b = find_blob(hi, shape.polygon.transformed(t))
            if b is not None:
                hi_blob = b
    if lo_blob is not None and hi_blob is not None:
        union((lo, lo_blob), (hi, hi_blob))
    else:
        unresolved_vias += 1
print(f"[info] vias with an unresolved endpoint: {unresolved_vias} / {sum(1 for i in insts if i.cell.name in via_bridge)}")


# --- per-cell-type local pin-shape -> pin-name map (computed once per type) ---
def local_pin_map(cell):
    """list of (gds_layer_num, polygon, pin_name) in the cell's own local coords."""
    labels = []  # (layer_num, x, y, text)
    for n in CONDUCTOR_LAYERS:
        for li in layer_nums_present[n]:
            if layout.get_info(li).datatype != LABEL_DATATYPE:
                continue
            for shape in cell.shapes(li).each():
                if shape.is_text():
                    labels.append((n, shape.text.x, shape.text.y, shape.text.string))

    result = []
    for n in CONDUCTOR_LAYERS:
        for li in layer_nums_present[n]:
            if layout.get_info(li).datatype != PIN_DATATYPE:
                continue
            for shape in cell.shapes(li).each():
                box = shape.polygon.bbox()
                best, best_d = None, None
                for (ln, lx, ly, text) in labels:
                    if ln != n:
                        continue
                    cx, cy = (box.left + box.right) / 2, (box.bottom + box.top) / 2
                    d = (lx - cx) ** 2 + (ly - cy) ** 2
                    if best_d is None or d < best_d:
                        best, best_d = text, d
                result.append((n, shape.polygon, best or "?"))
    return result


type_pin_cache = {}


def pins_of(cell):
    if cell.name not in type_pin_cache:
        type_pin_cache[cell.name] = local_pin_map(cell)
    return type_pin_cache[cell.name]


# --- resolve net id for every instance pin ---
print("\n[info] resolving instance pins to nets...")
endpoints_by_net = defaultdict(list)  # net_root -> [(label, layer_num)]
mismatches = 0

for idx, inst in enumerate(insts):
    kind = classify(inst.cell.name)
    if kind not in ("logic", "tap"):
        continue
    t = inst.cplx_trans
    per_pin_roots = defaultdict(set)
    for (layer_num, local_poly, pin_name) in pins_of(inst.cell):
        b = find_blob(layer_num, local_poly.transformed(t))
        if b is None:
            continue
        per_pin_roots[pin_name].add(find((layer_num, b)))
    for pin_name, roots in per_pin_roots.items():
        if len(roots) > 1:
            mismatches += 1
        for root in roots:
            label = f"{inst.cell.name}#{idx}.{pin_name}"
            endpoints_by_net[root].append((label, kind))

print(f"[warn] pins whose fragments landed on >1 blob (possible split-pin artifact): {mismatches}")

# --- resolve top-level IO pins the same way ---
print("[info] resolving top-level IO pins...")
io_labels = []
for n in CONDUCTOR_LAYERS:
    for li in layer_nums_present[n]:
        if layout.get_info(li).datatype != LABEL_DATATYPE:
            continue
        for shape in top.shapes(li).each():
            if shape.is_text():
                io_labels.append((n, shape.text.x, shape.text.y, shape.text.string))

for n in CONDUCTOR_LAYERS:
    for li in layer_nums_present[n]:
        if layout.get_info(li).datatype != PIN_DATATYPE:
            continue
        for shape in top.shapes(li).each():
            box = shape.polygon.bbox()
            best, best_d = None, None
            for (ln, lx, ly, text) in io_labels:
                if ln != n:
                    continue
                cx, cy = (box.left + box.right) / 2, (box.bottom + box.top) / 2
                d = (lx - cx) ** 2 + (ly - cy) ** 2
                if best_d is None or d < best_d:
                    best, best_d = text, d
            b = find_blob(n, shape.polygon)
            if b is not None and best is not None:
                endpoints_by_net[find((n, b))].append((f"IO.{best}", "io"))

# --- report ---
print(f"\n[info] total nets with >=1 endpoint resolved: {len(endpoints_by_net)}")

signal_nets = []
power_nets = []
for root, eps in endpoints_by_net.items():
    names = [e[0] for e in eps]
    if any(n.endswith(".VPWR") or n.endswith(".VGND") or n == "IO.VPWR" or n == "IO.VGND" for n in names):
        power_nets.append((root, eps))
    else:
        signal_nets.append((root, eps))

print(f"\n=== SIGNAL nets ({len(signal_nets)}) ===")
for root, eps in sorted(signal_nets, key=lambda x: -len(x[1])):
    if len(eps) < 2:
        continue
    names = sorted(set(e[0] for e in eps))
    print(f"  net {root}: {names}")

print(f"\n=== POWER nets ({len(power_nets)}) — endpoint counts only ===")
for root, eps in power_nets:
    print(f"  net {root}: {len(eps)} endpoints")

print(f"\n=== nets with exactly 1 endpoint (dangling / unresolved) ===")
single = [(r, e) for r, e in endpoints_by_net.items() if len(e) == 1]
print(f"  count: {len(single)}")
for r, e in single[:20]:
    print(f"    {e[0][0]}")
