`timescale 1ns/1ps
module probe_len_tb;
  reg clk = 0, rst_n = 0, enable = 0, I = 0;
  wire success;
  wire [7:0] O;
  reg bits_mem [0:120];
  integer i;
  integer outfile;

  puzzle dut (.clk(clk), .rst_n(rst_n), .enable(enable), .I(I), .success(success), .O(O));

  always #5 clk = ~clk;

  initial begin
    $readmemb("board.txt", bits_mem);
    rst_n = 0; enable = 0; I = 0;
    repeat (3) @(posedge clk);
    rst_n = 1;
    @(posedge clk);
    enable = 1;
    for (i = 0; i < 121; i = i + 1) begin
      @(negedge clk);
      I = bits_mem[i];
      @(posedge clk);
    end
    enable = 0;
    I = 0;
    @(posedge clk);
    #1;
    outfile = $fopen("result.txt", "w");
    $fdisplay(outfile, "success=%b", success);
    $fdisplay(outfile, "O=%02x", O);
    $fclose(outfile);
    $finish;
  end
endmodule
