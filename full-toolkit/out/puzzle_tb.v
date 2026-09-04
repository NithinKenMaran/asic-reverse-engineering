`timescale 1ns/1ps
module puzzle_tb;
  reg clk = 0, rst_n = 0, enable = 0, I = 0;
  wire success;
  wire [7:0] O;
  integer i;
  reg [120:0] bits = 121'b0000000101010000100000000000010101010000000000001010000001000001000000100000101000010000000100000010000010010001010000000;

  puzzle dut (.clk(clk), .rst_n(rst_n), .enable(enable), .I(I), .success(success), .O(O));

  always #5 clk = ~clk;

  initial begin
    rst_n = 0; enable = 0; I = 0;
    repeat (3) @(posedge clk);
    rst_n = 1;
    @(posedge clk);
    enable = 1;
    for (i = 120; i >= 0; i = i - 1) begin
      @(negedge clk);  // change I mid-cycle, away from the sampling edge - avoids a same-delta-cycle race against the flop's own D-sampling
      I = bits[i];
      @(posedge clk);
    end
    enable = 0;
    I = 0;
    @(posedge clk);
    #1;
    $display("SUCCESS = %b", success);
    for (i = 0; i < 20; i = i + 1) begin
      $display("cycle %0d: O = 0x%02x (%s)", i, O, (O >= 32 && O < 127) ? {"'", O, "'"} : "n/a");
      @(posedge clk);
      #1;
    end
    $finish;
  end
endmodule
