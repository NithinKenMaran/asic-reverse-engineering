import os
import subprocess

from probe import NETLIST, WORK_DIR
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
    r = subprocess.run(
        ["iverilog", "-g2005", "-o", vvp_path, NETLIST, tb_path],
        cwd=WORK_DIR, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)
    with open(os.path.join(WORK_DIR, "board.txt"), "w") as f:
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
    s, o = compile_and_run(121, list(WINNING_BITS), mem_depth=121)
    print(f"121 enabled cycles (full board, sanity check): success={s} O=0x{o:02x}")

    bits_short = list(WINNING_BITS[:120]) + ["0"]
    s, o = compile_and_run(120, bits_short, mem_depth=121)
    print(f"120 enabled cycles (one short of a full board): success={s} O=0x{o:02x}")

    bits_long = list(WINNING_BITS) + ["0"]
    s, o = compile_and_run(122, bits_long, mem_depth=122)
    print(f"122 enabled cycles (one past a full board): success={s} O=0x{o:02x}")


if __name__ == "__main__":
    main()
