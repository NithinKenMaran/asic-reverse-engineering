"""
Shared GDS connectivity-extraction helpers built on klayout.db.

Standard cells are treated as black boxes (no transistor-level device
extraction) - see CLAUDE.md's "GDS layer map" / "Connectivity extraction
approach" sections for the reasoning and the empirically-derived layer roles
this relies on. Layer numbers below are specific to what we found in
warmup/04_final.gds (sky130_fd_sc_hd); re-derive per-GDS if the source
changes rather than assuming these hold universally.
"""
from collections import defaultdict
import klayout.db as db

CONDUCTOR_LAYERS = [67, 68, 69, 70, 71, 72]  # li1, met1, met2, met3, met4, met5
LAYER_NAME = {67: "li1", 68: "met1", 69: "met2", 70: "met3", 71: "met4", 72: "met5"}
PIN_DATATYPE = 16
LABEL_DATATYPE = 5


def classify(cell_name):
    if cell_name.startswith("VIA_"):
        return "via"
    if "tapvpwrvgnd" in cell_name:
        return "tap"
    if "decap" in cell_name:
        return "decap"
    return "logic"


class Netlist:
    def __init__(self, layout, top, insts, inst_kind, pin_net, io_net, net_endpoints, ambiguous_pins=frozenset()):
        self.layout = layout
        self.top = top
        self.insts = insts                  # list[db.Instance], index = the id used everywhere below
        self.inst_kind = inst_kind          # list[str], parallel to insts
        self.pin_net = pin_net              # {(inst_idx, pin_name): net_root}
        self.io_net = io_net                # {io_pin_name: net_root}
        self.net_endpoints = net_endpoints  # {net_root: [(kind, inst_idx_or_None, pin_name)]}
        self.ambiguous_pins = ambiguous_pins  # {(inst_idx, pin_name)}: >1 candidate blob, sorted()[0] picked unverified


def extract(path):
    layout = db.Layout()
    layout.read(path)
    top = layout.top_cells()[0]

    layer_nums_present = defaultdict(list)  # gds layer number -> [klayout layer indexes, any datatype]
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        layer_nums_present[info.layer].append(li)

    insts = list(top.each_inst())
    inst_kind = [classify(i.cell.name) for i in insts]

    # which two conductor layers does each via cell type bridge? (derived from
    # its own geometry, not hardcoded - see CLAUDE.md)
    via_bridge = {}
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

    # per-conductor-layer copper: top cell's own shapes + every instance's
    # leaf shapes, transformed into top coordinates
    region_by_layer = {n: db.Region() for n in CONDUCTOR_LAYERS}
    for n in CONDUCTOR_LAYERS:
        for li in layer_nums_present[n]:
            if layout.get_info(li).datatype == LABEL_DATATYPE:
                continue
            for shape in top.shapes(li).each():
                if not shape.is_text():
                    region_by_layer[n].insert(shape.polygon)
    for inst in insts:
        t = inst.cplx_trans
        for n in CONDUCTOR_LAYERS:
            for li in layer_nums_present[n]:
                if layout.get_info(li).datatype == LABEL_DATATYPE:
                    continue
                for shape in inst.cell.shapes(li).each():
                    if not shape.is_text():
                        region_by_layer[n].insert(shape.polygon.transformed(t))

    merged_by_layer = {n: list(region_by_layer[n].merged().each()) for n in CONDUCTOR_LAYERS}

    def find_blob(layer_num, polygon):
        box = polygon.bbox()
        q = db.Region(polygon)
        for idx, poly in enumerate(merged_by_layer[layer_num]):
            if not poly.bbox().overlaps(box) and not poly.bbox().touches(box):
                continue
            if not db.Region(poly).interacting(q).is_empty():
                return idx
        return None

    # union-find over (layer_num, blob_idx), bridged by via instances
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

    for inst in insts:
        name = inst.cell.name
        if name not in via_bridge:
            continue
        lo, hi = via_bridge[name]
        t = inst.cplx_trans
        lo_blobs = set()
        hi_blobs = set()
        for li in layer_nums_present[lo]:
            if layout.get_info(li).datatype == LABEL_DATATYPE:
                continue
            for shape in inst.cell.shapes(li).each():
                if shape.is_text():
                    continue
                b = find_blob(lo, shape.polygon.transformed(t))
                if b is not None:
                    lo_blobs.add(b)
        for li in layer_nums_present[hi]:
            if layout.get_info(li).datatype == LABEL_DATATYPE:
                continue
            for shape in inst.cell.shapes(li).each():
                if shape.is_text():
                    continue
                b = find_blob(hi, shape.polygon.transformed(t))
                if b is not None:
                    hi_blobs.add(b)
        # A VIA_* instance is one electrical object even when its cell uses
        # several polygons/cuts.  Union every touched lower blob with every
        # touched upper blob; the old scalar variables retained only the last
        # shape encountered and silently dropped routed pin access.
        for lo_blob in lo_blobs:
            for hi_blob in hi_blobs:
                union((lo, lo_blob), (hi, hi_blob))

    # per-cell-type local pin-shape -> pin-name (via nearest text label), cached once per type
    def local_pin_map(cell):
        labels = []
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
                        if best_d is None or (d, text) < (best_d, best):
                            best, best_d = text, d
                    result.append((n, shape.polygon, best or "?"))
        return result

    type_pin_cache = {}

    def pins_of(cell):
        if cell.name not in type_pin_cache:
            type_pin_cache[cell.name] = local_pin_map(cell)
        return type_pin_cache[cell.name]

    # resolve every instance pin (logic + tap) to a net root
    pin_net = {}
    net_endpoints = defaultdict(list)
    ambiguous_pins = set()  # {(idx, pin_name)} where >1 candidate blob existed
    for idx, inst in enumerate(insts):
        if inst_kind[idx] not in ("logic", "tap"):
            continue
        t = inst.cplx_trans
        per_pin_roots = defaultdict(set)
        for (layer_num, local_poly, pin_name) in pins_of(inst.cell):
            b = find_blob(layer_num, local_poly.transformed(t))
            if b is not None:
                per_pin_roots[pin_name].add(find((layer_num, b)))
        for pin_name, roots in per_pin_roots.items():
            if len(roots) > 1:
                # not necessarily wrong (see note below - picking a specific
                # side by heuristic was tried and reverted), but this marks
                # the pick as *unproven* rather than trusted, for any caller
                # that wants to route only-verified-by-cross-reference
                # instances through a stronger check (see
                # emit_verilog.find_self_colliding / patch_from_recovered.py).
                ambiguous_pins.add((idx, pin_name))
            # known artifact: wide pins occasionally split across >1 blob
            # (see CLAUDE.md); take one deterministically rather than drop it.
            #
            # Tried (2026-09-03, puzzle.gds session): on a handful of puzzle
            # instances (nand2b) a fragment of one pin's marker lands on the
            # same blob as a *different* pin's fragment on the same instance
            # (nand2b's own Y and B), which this sorted()[0] pick can resolve
            # to the wrong one of the two - see CLAUDE.md known rough edges.
            # Attempted fix: prefer a candidate blob no sibling pin on the
            # instance also claims, else leave unresolved. Reverted - it
            # broke a previously-100%-validated warmup pin (AND4_2N#659.D),
            # i.e. "contested by a sibling pin" is not actually a reliable
            # signal of a wrong pick. Left as a known, explicitly-surfaced
            # gap (multi-driver ValueError / Unknown leaf) rather than a
            # plausible-looking but unproven heuristic.
            root = sorted(roots)[0]
            pin_net[(idx, pin_name)] = root
            net_endpoints[root].append(("inst", idx, pin_name))

    # A few signal pins are reached through an in-cell li1->met1 access
    # contact rather than a separately-instantiated VIA_* object.  Restrict
    # this repair to roots that currently contain exactly one logical pin;
    # already-connected nets must not be altered by internal cell geometry.
    singleton_pin_roots = {
        root
        for root, endpoints in net_endpoints.items()
        if len(endpoints) == 1 and endpoints[0][2] not in ("VPWR", "VGND")
    }

    def blobs_with_area_overlap(layer_num, polygon):
        query = db.Region(polygon)
        result = []
        for blob_idx, blob in enumerate(merged_by_layer[layer_num]):
            if not blob.bbox().overlaps(polygon.bbox()):
                continue
            if not (db.Region(blob) & query).is_empty():
                result.append(blob_idx)
        return result

    li1 = CONDUCTOR_LAYERS[0]
    met1 = CONDUCTOR_LAYERS[1]
    for idx, inst in enumerate(insts):
        if inst_kind[idx] != "logic":
            continue
        for li in layer_nums_present[li1]:
            if layout.get_info(li).datatype != 44:
                continue
            for shape in inst.cell.shapes(li).each():
                if shape.is_text():
                    continue
                cut = shape.polygon.transformed(inst.cplx_trans)
                lo_blobs = blobs_with_area_overlap(li1, cut)
                hi_blobs = blobs_with_area_overlap(met1, cut)
                for lo_blob in lo_blobs:
                    lo_root = find((li1, lo_blob))
                    if lo_root not in singleton_pin_roots:
                        continue
                    for hi_blob in hi_blobs:
                        union(lo_root, (met1, hi_blob))

    # The repair may have changed union-find representatives; canonicalize
    # every stored endpoint before top-level I/O endpoints are added.
    net_endpoints = defaultdict(list)
    for (idx, pin_name), root in list(pin_net.items()):
        root = find(root)
        pin_net[(idx, pin_name)] = root
        net_endpoints[root].append(("inst", idx, pin_name))

    # resolve top-level IO pins the same way
    io_net = {}
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
                    root = find((n, b))
                    io_net[best] = root
                    net_endpoints[root].append(("io", None, best))

    return Netlist(layout, top, insts, inst_kind, pin_net, io_net, dict(net_endpoints), ambiguous_pins)
