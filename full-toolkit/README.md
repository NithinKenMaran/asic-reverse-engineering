This folder contains the entire repository with all the tools I used as I solved this puzzle. 

I haven't documented all the tools here since many of them weren't relevant towards arriving at the solution. However, I've referenced the outputs of some of these scripts in my writeup, so I'm including this in my solution repository. 

# Prereqs

```bash
pip install klayout pyyaml
brew install icarus-verilog graphviz
```

# Inspect GDS

```bash
python3 inspect_gds.py
```

Without args, this takes the warmup gds by default. The puzzle gds can be given to this script as an argument. 

