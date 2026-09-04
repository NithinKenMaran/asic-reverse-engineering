"""
Reusable black-box/gray-box probing harness for out/puzzle_netlist_patched.v:
compile the real, patched, gate-level netlist ONCE against iverilog, then
drive it with an arbitrary 121-bit board (loaded at *runtime* via
$readmemb, so no recompile per experiment) and read back `success`, `O`,
and every one of the 92 state elements' final register value.

This is the instrument the rest of this session's derivation (grid shape,
region map, rule confirmation, final board) is built on: it treats
puzzle_netlist_patched.v purely as a black box we can feed inputs and read
state out of - the same kind of experiment CHECKER.md's authors describe
("injecting one star into each of the 121 cells"), just run independently
against our own reconstructed netlist rather than theirs.

Usage as a library:
    from probe_tool import Prober
    p = Prober()
    result = p.run("000...121 chars...000")
    result.success       # 0 or 1
    result.O              # int 0-255, the byte on O right after acceptance
    result.regs           # {226: 0, 227: 1, ...} - every register's Q, keyed by g<idx>
"""
import os
import re
import subprocess
import sys
from dataclasses import dataclass

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
NETLIST = os.path.join(SRC_DIR, "out", "puzzle_netlist_patched.v")
SKY130_ROOT = "/tmp/sky130_fd_sc_hd"
WORK_DIR = os.path.join(SRC_DIR, "out", "probe_work")  # pure build scratch (compiled .vvp, temp board/result files) - gitignored

CELL_TYPES_USED = [
    "a2111oi", "a211o", "a211oi", "a21bo", "a21boi", "a21o", "a21oi", "a221o", "a22o",
    "a22oi", "a311o", "a31o", "a31oi", "a32o", "a41oi", "and2", "and2b", "and3", "and3b",
    "and4", "and4b", "and4bb", "buf", "clkbuf", "conb", "dfrtp", "dfstp", "dfxtp", "inv",
    "mux2", "nand2", "nand2b", "nand3", "nand3b", "nand4", "nor2", "nor3", "nor3b", "nor4",
    "nor4b", "o211a", "o211ai", "o21a", "o21ai", "o21ba", "o21bai", "o221a", "o22a", "o22ai",
    "o2bb2a", "o311a", "o31a", "o31ai", "o32a", "o32ai", "or2", "or3", "or3b", "or4", "or4b",
    "or4bb", "xnor2", "xor2",
]


def register_indices():
    text = open(NETLIST).read()
    idxs = sorted(
        int(m) for m in re.findall(r"sky130_fd_sc_hd__(?:dfrtp|dfxtp|dfstp) g(\d+)", text)
    )
    return idxs


def gen_probe_tb(path, reg_idxs):
    lines = [
        "`timescale 1ns/1ps",
        "module probe_tb;",
        "  reg clk = 0, rst_n = 0, enable = 0, I = 0;",
        "  wire success;",
        "  wire [7:0] O;",
        "  reg bits_mem [0:120];",
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
        "    for (i = 0; i <= 120; i = i + 1) begin",
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
    ]
    for idx in reg_idxs:
        lines.append(f'    $fdisplay(outfile, "g{idx}=%b", dut.g{idx}.Q);')
    lines += [
        "    $fclose(outfile);",
        "    $finish;",
        "  end",
        "endmodule",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


@dataclass
class ProbeResult:
    success: int
    O: int
    regs: dict


class Prober:
    def __init__(self):
        os.makedirs(WORK_DIR, exist_ok=True)
        self.reg_idxs = register_indices()
        assert len(self.reg_idxs) == 92, len(self.reg_idxs)
        self.tb_path = os.path.join(WORK_DIR, "probe_tb.v")
        gen_probe_tb(self.tb_path, self.reg_idxs)
        self.vvp_path = os.path.join(WORK_DIR, "probe.vvp")
        self._compile()

    def _compile(self):
        iflags = [f"-I{SKY130_ROOT}/cells/{t}" for t in CELL_TYPES_USED]
        cmd = ["iverilog", "-g2005", "-o", self.vvp_path] + iflags + [NETLIST, self.tb_path]
        r = subprocess.run(cmd, cwd=WORK_DIR, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"compile failed:\n{r.stdout}\n{r.stderr}")

    def run(self, bits):
        assert len(bits) == 121 and set(bits) <= {"0", "1"}, bits
        board_path = os.path.join(WORK_DIR, "board.txt")
        with open(board_path, "w") as f:
            f.write("\n".join(bits) + "\n")
        result_path = os.path.join(WORK_DIR, "result.txt")
        if os.path.exists(result_path):
            os.remove(result_path)
        r = subprocess.run(["vvp", self.vvp_path], cwd=WORK_DIR, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"sim failed:\n{r.stdout}\n{r.stderr}")
        success, O, regs = None, None, {}
        for line in open(result_path):
            line = line.strip()
            if line.startswith("success="):
                success = int(line.split("=")[1], 2)
            elif line.startswith("O="):
                O = int(line.split("=")[1], 16)
            elif line.startswith("g"):
                k, v = line.split("=")
                regs[int(k[1:])] = int(v, 2)
        return ProbeResult(success, O, regs)


if __name__ == "__main__":
    p = Prober()
    bits = sys.argv[1] if len(sys.argv) > 1 else "0" * 121
    res = p.run(bits)
    print(f"success={res.success} O=0x{res.O:02x}")
    print(f"{len(res.regs)} registers read")
