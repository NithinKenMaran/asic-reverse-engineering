# Recover Verilog Netlist from gds

Run this from the repo root:

```bash
python3 emit_recovered.py /gds/puzzle.gds \
  --top puzzle \
  --sky130-root /sky130-root/sky130_fd_sc_hd \
  --output /out/recovered_puzzle.v
```