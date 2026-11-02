// 用途：测试平台骨架。时钟、复位、超时、断言四段结构固定，内容按被测模块替换。
// 输入：被测模块 top。
// 输出：仿真日志，含 PASS 或 FAIL。
// 工具版本：任意 Verilog-2001 仿真器。
// 未验证项：未覆盖门级仿真。

`timescale 1ns / 1ps

module tb_template;

    localparam integer CLK_PERIOD = 10;      // 100 MHz
    localparam integer TIMEOUT_NS = 200000;  // 全局超时

    reg clk = 1'b0;
    reg rst = 1'b1;

    // 被测模块实例：端口名与被测模块保持一致
    // top dut (.clk(clk), .rst(rst), ...);

    always #(CLK_PERIOD/2) clk = ~clk;

    // 全局超时：任何挂死都要以 FAIL 结束，而不是让仿真器一直跑
    initial begin
        #(TIMEOUT_NS);
        $display("FAIL: global timeout at %0t", $time);
        $fatal(1, "timeout");
    end

    // 断言：把期望写成可执行判断，不要只打印数值
    task expect_eq(input [31:0] got, input [31:0] exp, input [127:0] name);
        begin
            if (got !== exp) begin
                $display("FAIL: %0s got=%0d exp=%0d at %0t", name, got, exp, $time);
                $fatal(1, "assertion failed");
            end else begin
                $display("PASS: %0s = %0d", name, got);
            end
        end
    endtask

    initial begin
        rst = 1'b1;
        repeat (8) @(posedge clk);
        rst = 1'b0;
        repeat (4) @(posedge clk);

        // 用例区：每个用例一个 begin/end，用例之间互相独立
        // expect_eq(dut.some_output, 32'd1, "some_output");

        $display("PASS: all cases done");
        $finish;
    end

endmodule
