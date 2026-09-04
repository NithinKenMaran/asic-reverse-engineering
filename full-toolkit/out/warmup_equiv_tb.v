`timescale 1ns/1ps
module warmup_equiv_tb;
  reg clk = 0, rst_n = 0, en = 0, A = 0, B = 0;
  wire S_mine, S_ref;
  integer i, vec, mismatches, checked;

  adder_demo     dut_mine (.A(A), .B(B), .clk(clk), .en(en), .rst_n(rst_n), .S(S_mine));
  adder_demo_ref dut_ref  (.A(A), .B(B), .clk(clk), .en(en), .rst_n(rst_n), .S(S_ref));

  always #5 clk = ~clk;

  task shift_bit;
    input a_bit, b_bit;
    begin
      A = a_bit; B = b_bit; en = 1;
      @(posedge clk);
    end
  endtask

  initial begin
    mismatches = 0;
    checked = 0;
    rst_n = 0; en = 0; A = 0; B = 0;
    repeat (3) @(posedge clk);
    rst_n = 1;
    @(posedge clk);
    for (vec = 0; vec < 65536; vec = vec + 1) begin
      for (i = 7; i >= 0; i = i - 1) begin
        shift_bit(vec[8 + i], vec[i]);
      end
      en = 0;
      #1;
      checked = checked + 1;
      if (S_mine !== S_ref) begin
        mismatches = mismatches + 1;
        if (mismatches <= 20)
          $display("MISMATCH vec=%0d A=%0d B=%0d mine=%b ref=%b", vec, vec[15:8], vec[7:0], S_mine, S_ref);
      end
    end
    $display("checked=%0d mismatches=%0d", checked, mismatches);
    if (mismatches == 0)
      $display("EQUIVALENCE: PASS");
    else
      $display("EQUIVALENCE: FAIL");
    $finish;
  end
endmodule
