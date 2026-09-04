
# Verilog Netlist from GDS:

```bash
python3 emit_recovered.py ../gds/puzzle.gds \
  --top puzzle \
  --sky130-root ../sky130-root/sky130_fd_sc_hd \
  --output ../out/recovered_puzzle.v
```

# Derive Region Map

```bash
python3 experiment_single_star.py
python3 derive_region_map.py
```

# Solve

```bash
python3 experiment_perturbations.py   
python3 experiment_length.py

python3 solver.py
```