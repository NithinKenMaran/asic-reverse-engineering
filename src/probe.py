"""Black-box probing harness for the recovered puzzle netlist.

This is the instrument the whole functional-recovery flow is built on. It
treats `../out/recovered_puzzle.v` purely as a black box: compile it ONCE with
iverilog, then drive it with an arbitrary 121-bit board (loaded at simulation
*runtime* via $readmemb, so no recompile per experiment) and read back
`success`, `O`, and every one of the 92 flip-flops' final value.

Unlike the earlier harness in ../../src, the recovered netlist here is
*behavioral* Verilog (assign + always blocks, registers named state_0..state_91)
- it is fully self-contained, so no Sky130 vendor cell models and no -I include
flags are needed. Plain `iverilog recovered_puzzle.v tb.v` is enough.

Usage as a library:
    from probe import Prober
    p = Prober()
    r = p.run("000...121 chars...000")
    r.success   # 0 or 1
    r.O          # int 0-255, the byte on O right after acceptance
    r.regs       # {0: 0, 1: 1, ...} - every state register's Q, keyed by index
"""
import os
import re
import subprocess
import sys
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
NETLIST = os.path.join(HERE, "..", "out", "recovered_puzzle.v")
WORK_DIR = os.path.join(HERE, "work")  # build scratch (compiled .vvp, temp board/result files)


def register_indices():
    """Every `reg state_<n>;` in the recovered netlist, sorted."""
    text = open(NETLIST).read()
    idxs = sorted(int(m) for m in re.findall(r"\breg\s+state_(\d+)\s*;", text))
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
        # update stimulus mid-cycle (negedge) so it is fully settled before the
        # next sampling edge - avoids racing the D-input at posedge.
        "      @(negedge clk);",
        "      I = bits_mem[i];",
        "      @(posedge clk);",
        "    end",
        "    enable = 0;",
        "    I = 0;",
        "    @(posedge clk);",  # disabled clock that registers acceptance
        "    #1;",
        '    outfile = $fopen("result.txt", "w");',
        '    $fdisplay(outfile, "success=%b", success);',
        '    $fdisplay(outfile, "O=%02x", O);',
    ]
    for idx in reg_idxs:
        lines.append(f'    $fdisplay(outfile, "s{idx}=%b", dut.state_{idx});')
    lines += [
        "    $fclose(outfile);",
        "    $finish;",
        "  end",
        "endmodule",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def gen_trace_tb(path, reg_idxs):
    """Testbench that dumps every state register after EACH of the 121 enabled
    scan clocks (one line per clock), so a single run yields the machine's
    internal state after every prefix of the board. Used by the black-box
    search to read a sticky-error latch after any partial scan without an
    acceptance clock."""
    dumps = "".join(
        f'      $fwrite(outfile, " s{idx}=%b", dut.state_{idx});\n' for idx in reg_idxs
    )
    body = f"""`timescale 1ns/1ps
module trace_tb;
  reg clk = 0, rst_n = 0, enable = 0, I = 0;
  wire success; wire [7:0] O;
  reg bits_mem [0:120];
  integer i, outfile;
  puzzle dut (.clk(clk), .rst_n(rst_n), .enable(enable), .I(I), .success(success), .O(O));
  always #5 clk = ~clk;
  initial begin
    $readmemb("board.txt", bits_mem);
    rst_n = 0; enable = 0; I = 0;
    repeat (3) @(posedge clk);
    rst_n = 1;
    @(posedge clk);
    enable = 1;
    outfile = $fopen("trace.txt", "w");
    for (i = 0; i <= 120; i = i + 1) begin
      @(negedge clk);
      I = bits_mem[i];
      @(posedge clk);
      #1;
      $fwrite(outfile, "clk%0d", i);
{dumps}      $fwrite(outfile, "\\n");
    end
    $fclose(outfile);
    $finish;
  end
endmodule
"""
    with open(path, "w") as f:
        f.write(body)


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
        cmd = ["iverilog", "-g2005", "-o", self.vvp_path, NETLIST, self.tb_path]
        r = subprocess.run(cmd, cwd=WORK_DIR, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"compile failed:\n{r.stdout}\n{r.stderr}")

    def run(self, bits):
        assert len(bits) == 121 and set(bits) <= {"0", "1"}, bits
        with open(os.path.join(WORK_DIR, "board.txt"), "w") as f:
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
            elif line.startswith("s"):
                k, v = line.split("=")
                regs[int(k[1:])] = int(v, 2)
        return ProbeResult(success, O, regs)


class Tracer:
    """Compile-once harness that returns per-clock internal state for a board:
    trace[k] is the dict of all 92 registers right after scan clock k (the cell
    at position k has just been shifted in). Lets the black-box search read a
    sticky-error latch after any prefix from a single simulation run."""

    def __init__(self):
        os.makedirs(WORK_DIR, exist_ok=True)
        self.reg_idxs = register_indices()
        self.tb_path = os.path.join(WORK_DIR, "trace_tb.v")
        gen_trace_tb(self.tb_path, self.reg_idxs)
        self.vvp_path = os.path.join(WORK_DIR, "trace.vvp")
        cmd = ["iverilog", "-g2005", "-o", self.vvp_path, NETLIST, self.tb_path]
        r = subprocess.run(cmd, cwd=WORK_DIR, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"compile failed:\n{r.stdout}\n{r.stderr}")

    def trace(self, bits):
        assert len(bits) == 121 and set(bits) <= {"0", "1"}, bits
        with open(os.path.join(WORK_DIR, "board.txt"), "w") as f:
            f.write("\n".join(bits) + "\n")
        trace_path = os.path.join(WORK_DIR, "trace.txt")
        if os.path.exists(trace_path):
            os.remove(trace_path)
        r = subprocess.run(["vvp", self.vvp_path], cwd=WORK_DIR, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"sim failed:\n{r.stdout}\n{r.stderr}")
        rows = [None] * 121
        for line in open(trace_path):
            toks = line.split()
            k = int(toks[0][3:])  # "clk<k>"
            rows[k] = {int(t.split("=")[0][1:]): int(t.split("=")[1], 2) for t in toks[1:]}
        return rows


if __name__ == "__main__":
    p = Prober()
    bits = sys.argv[1] if len(sys.argv) > 1 else "0" * 121
    res = p.run(bits)
    print(f"success={res.success} O=0x{res.O:02x} ('{chr(res.O) if 32 <= res.O < 127 else '?'}')")
    print(f"{len(res.regs)} state registers read")
