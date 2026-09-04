"""
Experiment 3: does the checker require the scan to actually reach cell
(10,10) - i.e. exactly 121 enabled clocks - or would it accept fewer/more?
Uses a length-parameterized testbench variant (probe_tool.py's harness is
fixed at 121 cycles by construction).
"""
import os
import subprocess

from probe_tool import SRC_DIR, NETLIST, SKY130_ROOT, WORK_DIR, CELL_TYPES_USED
from star_battle import WINNING_BITS

os.makedirs(WORK_DIR, exist_ok=True)


def gen_tb(path, n_cycles, mem_depth=121):
    lines = [
        "`timescale 1ns/1ps",
        "module probe_len_tb;",
        "  reg clk = 0, rst_n = 0, enable = 0, I = 0;",
        "  wire success;",
        "  wire [7:0] O;",
        f"  reg bits_mem [0:{mem_depth - 1}];",
        "  integer i;",
        "  integer outfile;",
        "",
        "  puzzle dut (.clk(clk), .rst_n(rst_n), .enable(enable), .I(I), .success(success), .O(O));",
        "",
        "  always #5 clk = ~clk;",
        "",
        "  initial begin",
        '    $readmemb("board.txt", bits_mem);',
        "    rst_n = 0; enable = 0; I = 0;",
        "    repeat (3) @(posedge clk);",
        "    rst_n = 1;",
        "    @(posedge clk);",
        "    enable = 1;",
        f"    for (i = 0; i < {n_cycles}; i = i + 1) begin",
        "      @(negedge clk);",
        "      I = bits_mem[i];",
        "      @(posedge clk);",
        "    end",
        "    enable = 0;",
        "    I = 0;",
        "    @(posedge clk);",
        "    #1;",
        '    outfile = $fopen("result.txt", "w");',
        '    $fdisplay(outfile, "success=%b", success);',
        '    $fdisplay(outfile, "O=%02x", O);',
        "    $fclose(outfile);",
        "    $finish;",
        "  end",
        "endmodule",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def compile_and_run(n_cycles, bits, mem_depth=121):
    tb_path = os.path.join(WORK_DIR, f"probe_len_{n_cycles}_{mem_depth}.v")
    vvp_path = os.path.join(WORK_DIR, f"probe_len_{n_cycles}_{mem_depth}.vvp")
    gen_tb(tb_path, n_cycles, mem_depth)
    iflags = [f"-I{SKY130_ROOT}/cells/{t}" for t in CELL_TYPES_USED]
    r = subprocess.run(
        ["iverilog", "-g2005", "-o", vvp_path] + iflags + [NETLIST, tb_path],
        cwd=WORK_DIR, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)
    board_path = os.path.join(WORK_DIR, "board.txt")
    with open(board_path, "w") as f:
        f.write("\n".join(bits) + "\n")
    result_path = os.path.join(WORK_DIR, "result.txt")
    if os.path.exists(result_path):
        os.remove(result_path)
    r = subprocess.run(["vvp", vvp_path], cwd=WORK_DIR, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)
    success, O = None, None
    for line in open(result_path):
        line = line.strip()
        if line.startswith("success="):
            success = int(line.split("=")[1], 2)
        elif line.startswith("O="):
            O = int(line.split("=")[1], 16)
    return success, O


def main():
    # exact length, sanity re-check with this alternate testbench generator
    s, o = compile_and_run(121, list(WINNING_BITS), mem_depth=121)
    print(f"121 enabled cycles (full board, sanity check): success={s} O=0x{o:02x}")

    # one cycle short: cells 0..119 of the winning board loaded, cell 120
    # (the (10,10) star) never sampled - position counter should be at
    # (10,9), not (10,10), when acceptance is checked. bits_mem[120] is
    # unused (loop only runs i=0..119) but $readmemb still needs a value.
    bits_short = list(WINNING_BITS[:120]) + ["0"]
    s, o = compile_and_run(120, bits_short, mem_depth=121)
    print(f"120 enabled cycles (one short of a full board): success={s} O=0x{o:02x}")

    # one cycle long: 121 real board bits plus one extra (arbitrary) bit
    # shifted in afterward, still with enable high for that 122nd cycle -
    # tests whether success is sampled exactly at (10,10) or would also
    # accept "passed through (10,10) and kept going".
    bits_long = list(WINNING_BITS) + ["0"]
    s, o = compile_and_run(122, bits_long, mem_depth=122)
    print(f"122 enabled cycles (one past a full board): success={s} O=0x{o:02x}")


if __name__ == "__main__":
    main()
