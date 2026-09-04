import sys
import klayout.db as db

path = sys.argv[1] if len(sys.argv) > 1 else "gds/04_final.gds"

layout = db.Layout()
layout.read(path)

print(f"file: {path}")
print(f"dbu (database unit, microns/unit): {layout.dbu}")
print(f"top cell(s): {[c.name for c in layout.top_cells()]}")
print(f"total cells: {layout.cells()}")

print("\nlayers:")
for li in layout.layer_indexes():
    info = layout.get_info(li)
    print(f"  layer {li}: layer={info.layer}, datatype={info.datatype}, name={info.name!r}")

print("\ncell hierarchy (top cell + immediate children):")
for top in layout.top_cells():
    print(f"  top: {top.name}  (bbox: {top.bbox()})")
    seen = {}
    for inst in top.each_inst():
        cname = inst.cell.name
        seen[cname] = seen.get(cname, 0) + 1
    for cname, count in sorted(seen.items(), key=lambda kv: -kv[1]):
        print(f"    {cname}: {count} instances")

print(f"\ntotal distinct cell definitions in file: {layout.cells()}")
all_cells = [layout.cell(i).name for i in range(layout.cells())]
print(f"cell names: {all_cells}")
