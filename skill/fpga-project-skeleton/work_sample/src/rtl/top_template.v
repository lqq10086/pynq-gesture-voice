// 用途：模块骨架。端口按用途分组，时序逻辑只用一个 always 块。
// 输入：时钟、复位、上游握手。
// 输出：下游握手、状态位。
// 工具版本：Vivado 2020.1 及以上。
// 未验证项：未做跨时钟域处理，若端口分属两个时钟域，必须另加同步器。

module top_template #(
    parameter integer DATA_W = 16
) (
    input  wire                 clk,
    input  wire                 rst,        // 同步复位，高有效

    // 上游接口：valid 与 ready 成对，valid 一旦拉高必须保持到握手
    input  wire                 s_valid,
    output wire                 s_ready,
    input  wire [DATA_W-1:0]    s_data,

    // 下游接口
    output reg                  m_valid,
    input  wire                 m_ready,
    output reg  [DATA_W-1:0]    m_data,

    // 状态输出：给主机侧读，不参与控制
    output reg                  busy,
    output reg  [7:0]           err_code
);

    // ---------------------------------------------------------------------
    // 组合逻辑：只写表达式，不写 if 块里的赋值
    // ---------------------------------------------------------------------
    assign s_ready = ~busy;

    // ---------------------------------------------------------------------
    // 时序逻辑：一个 always 块管一个状态机，复位值全部显式给出
    // ---------------------------------------------------------------------
    always @(posedge clk) begin
        if (rst) begin
            m_valid  <= 1'b0;
            m_data   <= {DATA_W{1'b0}};
            busy     <= 1'b0;
            err_code <= 8'd0;
        end else begin
            if (s_valid && s_ready) begin
                m_valid <= 1'b1;
                m_data  <= s_data;
                busy    <= 1'b1;
            end else if (m_valid && m_ready) begin
                m_valid <= 1'b0;
                busy    <= 1'b0;
            end

            // 错误路径必须自己回到可重试状态，不能依赖上位机复位
        end
    end

endmodule
