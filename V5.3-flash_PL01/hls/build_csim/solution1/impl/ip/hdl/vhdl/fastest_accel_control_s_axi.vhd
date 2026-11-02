-- ==============================================================
-- Vitis HLS - High-Level Synthesis from C, C++ and OpenCL v2023.1 (64-bit)
-- Tool Version Limit: 2023.05
-- Copyright 1986-2022 Xilinx, Inc. All Rights Reserved.
-- Copyright 2022-2023 Advanced Micro Devices, Inc. All Rights Reserved.
-- 
-- ==============================================================
library IEEE;
use IEEE.STD_LOGIC_1164.all;
use IEEE.NUMERIC_STD.all;

entity fastest_accel_control_s_axi is
generic (
    C_S_AXI_ADDR_WIDTH    : INTEGER := 8;
    C_S_AXI_DATA_WIDTH    : INTEGER := 32);
port (
    ACLK                  :in   STD_LOGIC;
    ARESET                :in   STD_LOGIC;
    ACLK_EN               :in   STD_LOGIC;
    AWADDR                :in   STD_LOGIC_VECTOR(C_S_AXI_ADDR_WIDTH-1 downto 0);
    AWVALID               :in   STD_LOGIC;
    AWREADY               :out  STD_LOGIC;
    WDATA                 :in   STD_LOGIC_VECTOR(C_S_AXI_DATA_WIDTH-1 downto 0);
    WSTRB                 :in   STD_LOGIC_VECTOR(C_S_AXI_DATA_WIDTH/8-1 downto 0);
    WVALID                :in   STD_LOGIC;
    WREADY                :out  STD_LOGIC;
    BRESP                 :out  STD_LOGIC_VECTOR(1 downto 0);
    BVALID                :out  STD_LOGIC;
    BREADY                :in   STD_LOGIC;
    ARADDR                :in   STD_LOGIC_VECTOR(C_S_AXI_ADDR_WIDTH-1 downto 0);
    ARVALID               :in   STD_LOGIC;
    ARREADY               :out  STD_LOGIC;
    RDATA                 :out  STD_LOGIC_VECTOR(C_S_AXI_DATA_WIDTH-1 downto 0);
    RRESP                 :out  STD_LOGIC_VECTOR(1 downto 0);
    RVALID                :out  STD_LOGIC;
    RREADY                :in   STD_LOGIC;
    interrupt             :out  STD_LOGIC;
    ap_start              :out  STD_LOGIC;
    ap_done               :in   STD_LOGIC;
    ap_ready              :in   STD_LOGIC;
    ap_idle               :in   STD_LOGIC;
    ap_return             :in   STD_LOGIC_VECTOR(31 downto 0);
    input_r               :out  STD_LOGIC_VECTOR(31 downto 0);
    weights               :out  STD_LOGIC_VECTOR(31 downto 0);
    bias                  :out  STD_LOGIC_VECTOR(31 downto 0);
    shift                 :out  STD_LOGIC_VECTOR(31 downto 0);
    output_r              :out  STD_LOGIC_VECTOR(31 downto 0);
    channel_offsets       :out  STD_LOGIC_VECTOR(31 downto 0);
    channel_shifts        :out  STD_LOGIC_VECTOR(31 downto 0);
    op                    :out  STD_LOGIC_VECTOR(31 downto 0);
    ci                    :out  STD_LOGIC_VECTOR(31 downto 0);
    co                    :out  STD_LOGIC_VECTOR(31 downto 0);
    ih                    :out  STD_LOGIC_VECTOR(31 downto 0);
    iw                    :out  STD_LOGIC_VECTOR(31 downto 0);
    kernel                :out  STD_LOGIC_VECTOR(31 downto 0);
    stride                :out  STD_LOGIC_VECTOR(31 downto 0);
    pad                   :out  STD_LOGIC_VECTOR(31 downto 0);
    groups                :out  STD_LOGIC_VECTOR(31 downto 0);
    relu                  :out  STD_LOGIC_VECTOR(31 downto 0);
    use_channel_offsets   :out  STD_LOGIC_VECTOR(31 downto 0);
    input_span_elements   :out  STD_LOGIC_VECTOR(31 downto 0)
);
end entity fastest_accel_control_s_axi;

-- ------------------------Address Info-------------------
-- 0x00 : Control signals
--        bit 0  - ap_start (Read/Write/COH)
--        bit 1  - ap_done (Read/COR)
--        bit 2  - ap_idle (Read)
--        bit 3  - ap_ready (Read/COR)
--        bit 7  - auto_restart (Read/Write)
--        bit 9  - interrupt (Read)
--        others - reserved
-- 0x04 : Global Interrupt Enable Register
--        bit 0  - Global Interrupt Enable (Read/Write)
--        others - reserved
-- 0x08 : IP Interrupt Enable Register (Read/Write)
--        bit 0 - enable ap_done interrupt (Read/Write)
--        bit 1 - enable ap_ready interrupt (Read/Write)
--        others - reserved
-- 0x0c : IP Interrupt Status Register (Read/TOW)
--        bit 0 - ap_done (Read/TOW)
--        bit 1 - ap_ready (Read/TOW)
--        others - reserved
-- 0x10 : Data signal of ap_return
--        bit 31~0 - ap_return[31:0] (Read)
-- 0x18 : Data signal of input_r
--        bit 31~0 - input_r[31:0] (Read/Write)
-- 0x1c : reserved
-- 0x20 : Data signal of weights
--        bit 31~0 - weights[31:0] (Read/Write)
-- 0x24 : reserved
-- 0x28 : Data signal of bias
--        bit 31~0 - bias[31:0] (Read/Write)
-- 0x2c : reserved
-- 0x30 : Data signal of shift
--        bit 31~0 - shift[31:0] (Read/Write)
-- 0x34 : reserved
-- 0x38 : Data signal of output_r
--        bit 31~0 - output_r[31:0] (Read/Write)
-- 0x3c : reserved
-- 0x40 : Data signal of channel_offsets
--        bit 31~0 - channel_offsets[31:0] (Read/Write)
-- 0x44 : reserved
-- 0x48 : Data signal of channel_shifts
--        bit 31~0 - channel_shifts[31:0] (Read/Write)
-- 0x4c : reserved
-- 0x50 : Data signal of op
--        bit 31~0 - op[31:0] (Read/Write)
-- 0x54 : reserved
-- 0x58 : Data signal of ci
--        bit 31~0 - ci[31:0] (Read/Write)
-- 0x5c : reserved
-- 0x60 : Data signal of co
--        bit 31~0 - co[31:0] (Read/Write)
-- 0x64 : reserved
-- 0x68 : Data signal of ih
--        bit 31~0 - ih[31:0] (Read/Write)
-- 0x6c : reserved
-- 0x70 : Data signal of iw
--        bit 31~0 - iw[31:0] (Read/Write)
-- 0x74 : reserved
-- 0x78 : Data signal of kernel
--        bit 31~0 - kernel[31:0] (Read/Write)
-- 0x7c : reserved
-- 0x80 : Data signal of stride
--        bit 31~0 - stride[31:0] (Read/Write)
-- 0x84 : reserved
-- 0x88 : Data signal of pad
--        bit 31~0 - pad[31:0] (Read/Write)
-- 0x8c : reserved
-- 0x90 : Data signal of groups
--        bit 31~0 - groups[31:0] (Read/Write)
-- 0x94 : reserved
-- 0x98 : Data signal of relu
--        bit 31~0 - relu[31:0] (Read/Write)
-- 0x9c : reserved
-- 0xa0 : Data signal of use_channel_offsets
--        bit 31~0 - use_channel_offsets[31:0] (Read/Write)
-- 0xa4 : reserved
-- 0xa8 : Data signal of input_span_elements
--        bit 31~0 - input_span_elements[31:0] (Read/Write)
-- 0xac : reserved
-- (SC = Self Clear, COR = Clear on Read, TOW = Toggle on Write, COH = Clear on Handshake)

architecture behave of fastest_accel_control_s_axi is
    type states is (wridle, wrdata, wrresp, wrreset, rdidle, rddata, rdreset);  -- read and write fsm states
    signal wstate  : states := wrreset;
    signal rstate  : states := rdreset;
    signal wnext, rnext: states;
    constant ADDR_AP_CTRL                    : INTEGER := 16#00#;
    constant ADDR_GIE                        : INTEGER := 16#04#;
    constant ADDR_IER                        : INTEGER := 16#08#;
    constant ADDR_ISR                        : INTEGER := 16#0c#;
    constant ADDR_AP_RETURN_0                : INTEGER := 16#10#;
    constant ADDR_INPUT_R_DATA_0             : INTEGER := 16#18#;
    constant ADDR_INPUT_R_CTRL               : INTEGER := 16#1c#;
    constant ADDR_WEIGHTS_DATA_0             : INTEGER := 16#20#;
    constant ADDR_WEIGHTS_CTRL               : INTEGER := 16#24#;
    constant ADDR_BIAS_DATA_0                : INTEGER := 16#28#;
    constant ADDR_BIAS_CTRL                  : INTEGER := 16#2c#;
    constant ADDR_SHIFT_DATA_0               : INTEGER := 16#30#;
    constant ADDR_SHIFT_CTRL                 : INTEGER := 16#34#;
    constant ADDR_OUTPUT_R_DATA_0            : INTEGER := 16#38#;
    constant ADDR_OUTPUT_R_CTRL              : INTEGER := 16#3c#;
    constant ADDR_CHANNEL_OFFSETS_DATA_0     : INTEGER := 16#40#;
    constant ADDR_CHANNEL_OFFSETS_CTRL       : INTEGER := 16#44#;
    constant ADDR_CHANNEL_SHIFTS_DATA_0      : INTEGER := 16#48#;
    constant ADDR_CHANNEL_SHIFTS_CTRL        : INTEGER := 16#4c#;
    constant ADDR_OP_DATA_0                  : INTEGER := 16#50#;
    constant ADDR_OP_CTRL                    : INTEGER := 16#54#;
    constant ADDR_CI_DATA_0                  : INTEGER := 16#58#;
    constant ADDR_CI_CTRL                    : INTEGER := 16#5c#;
    constant ADDR_CO_DATA_0                  : INTEGER := 16#60#;
    constant ADDR_CO_CTRL                    : INTEGER := 16#64#;
    constant ADDR_IH_DATA_0                  : INTEGER := 16#68#;
    constant ADDR_IH_CTRL                    : INTEGER := 16#6c#;
    constant ADDR_IW_DATA_0                  : INTEGER := 16#70#;
    constant ADDR_IW_CTRL                    : INTEGER := 16#74#;
    constant ADDR_KERNEL_DATA_0              : INTEGER := 16#78#;
    constant ADDR_KERNEL_CTRL                : INTEGER := 16#7c#;
    constant ADDR_STRIDE_DATA_0              : INTEGER := 16#80#;
    constant ADDR_STRIDE_CTRL                : INTEGER := 16#84#;
    constant ADDR_PAD_DATA_0                 : INTEGER := 16#88#;
    constant ADDR_PAD_CTRL                   : INTEGER := 16#8c#;
    constant ADDR_GROUPS_DATA_0              : INTEGER := 16#90#;
    constant ADDR_GROUPS_CTRL                : INTEGER := 16#94#;
    constant ADDR_RELU_DATA_0                : INTEGER := 16#98#;
    constant ADDR_RELU_CTRL                  : INTEGER := 16#9c#;
    constant ADDR_USE_CHANNEL_OFFSETS_DATA_0 : INTEGER := 16#a0#;
    constant ADDR_USE_CHANNEL_OFFSETS_CTRL   : INTEGER := 16#a4#;
    constant ADDR_INPUT_SPAN_ELEMENTS_DATA_0 : INTEGER := 16#a8#;
    constant ADDR_INPUT_SPAN_ELEMENTS_CTRL   : INTEGER := 16#ac#;
    constant ADDR_BITS         : INTEGER := 8;

    signal waddr               : UNSIGNED(ADDR_BITS-1 downto 0);
    signal wmask               : UNSIGNED(C_S_AXI_DATA_WIDTH-1 downto 0);
    signal aw_hs               : STD_LOGIC;
    signal w_hs                : STD_LOGIC;
    signal rdata_data          : UNSIGNED(C_S_AXI_DATA_WIDTH-1 downto 0);
    signal ar_hs               : STD_LOGIC;
    signal raddr               : UNSIGNED(ADDR_BITS-1 downto 0);
    signal AWREADY_t           : STD_LOGIC;
    signal WREADY_t            : STD_LOGIC;
    signal ARREADY_t           : STD_LOGIC;
    signal RVALID_t            : STD_LOGIC;
    -- internal registers
    signal int_ap_idle         : STD_LOGIC := '0';
    signal int_ap_ready        : STD_LOGIC := '0';
    signal task_ap_ready       : STD_LOGIC;
    signal int_ap_done         : STD_LOGIC := '0';
    signal task_ap_done        : STD_LOGIC;
    signal int_task_ap_done    : STD_LOGIC := '0';
    signal int_ap_start        : STD_LOGIC := '0';
    signal int_interrupt       : STD_LOGIC := '0';
    signal int_auto_restart    : STD_LOGIC := '0';
    signal auto_restart_status : STD_LOGIC := '0';
    signal auto_restart_done   : STD_LOGIC;
    signal int_gie             : STD_LOGIC := '0';
    signal int_ier             : UNSIGNED(1 downto 0) := (others => '0');
    signal int_isr             : UNSIGNED(1 downto 0) := (others => '0');
    signal int_ap_return       : UNSIGNED(31 downto 0);
    signal int_input_r         : UNSIGNED(31 downto 0) := (others => '0');
    signal int_weights         : UNSIGNED(31 downto 0) := (others => '0');
    signal int_bias            : UNSIGNED(31 downto 0) := (others => '0');
    signal int_shift           : UNSIGNED(31 downto 0) := (others => '0');
    signal int_output_r        : UNSIGNED(31 downto 0) := (others => '0');
    signal int_channel_offsets : UNSIGNED(31 downto 0) := (others => '0');
    signal int_channel_shifts  : UNSIGNED(31 downto 0) := (others => '0');
    signal int_op              : UNSIGNED(31 downto 0) := (others => '0');
    signal int_ci              : UNSIGNED(31 downto 0) := (others => '0');
    signal int_co              : UNSIGNED(31 downto 0) := (others => '0');
    signal int_ih              : UNSIGNED(31 downto 0) := (others => '0');
    signal int_iw              : UNSIGNED(31 downto 0) := (others => '0');
    signal int_kernel          : UNSIGNED(31 downto 0) := (others => '0');
    signal int_stride          : UNSIGNED(31 downto 0) := (others => '0');
    signal int_pad             : UNSIGNED(31 downto 0) := (others => '0');
    signal int_groups          : UNSIGNED(31 downto 0) := (others => '0');
    signal int_relu            : UNSIGNED(31 downto 0) := (others => '0');
    signal int_use_channel_offsets : UNSIGNED(31 downto 0) := (others => '0');
    signal int_input_span_elements : UNSIGNED(31 downto 0) := (others => '0');


begin
-- ----------------------- Instantiation------------------


-- ----------------------- AXI WRITE ---------------------
    AWREADY_t <=  '1' when wstate = wridle else '0';
    AWREADY   <=  AWREADY_t;
    WREADY_t  <=  '1' when wstate = wrdata else '0';
    WREADY    <=  WREADY_t;
    BRESP     <=  "00";  -- OKAY
    BVALID    <=  '1' when wstate = wrresp else '0';
    wmask     <=  (31 downto 24 => WSTRB(3), 23 downto 16 => WSTRB(2), 15 downto 8 => WSTRB(1), 7 downto 0 => WSTRB(0));
    aw_hs     <=  AWVALID and AWREADY_t;
    w_hs      <=  WVALID and WREADY_t;

    -- write FSM
    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                wstate <= wrreset;
            elsif (ACLK_EN = '1') then
                wstate <= wnext;
            end if;
        end if;
    end process;

    process (wstate, AWVALID, WVALID, BREADY)
    begin
        case (wstate) is
        when wridle =>
            if (AWVALID = '1') then
                wnext <= wrdata;
            else
                wnext <= wridle;
            end if;
        when wrdata =>
            if (WVALID = '1') then
                wnext <= wrresp;
            else
                wnext <= wrdata;
            end if;
        when wrresp =>
            if (BREADY = '1') then
                wnext <= wridle;
            else
                wnext <= wrresp;
            end if;
        when others =>
            wnext <= wridle;
        end case;
    end process;

    waddr_proc : process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (aw_hs = '1') then
                    waddr <= UNSIGNED(AWADDR(ADDR_BITS-1 downto 0));
                end if;
            end if;
        end if;
    end process;

-- ----------------------- AXI READ ----------------------
    ARREADY_t <= '1' when (rstate = rdidle) else '0';
    ARREADY <= ARREADY_t;
    RDATA   <= STD_LOGIC_VECTOR(rdata_data);
    RRESP   <= "00";  -- OKAY
    RVALID_t  <= '1' when (rstate = rddata) else '0';
    RVALID    <= RVALID_t;
    ar_hs   <= ARVALID and ARREADY_t;
    raddr   <= UNSIGNED(ARADDR(ADDR_BITS-1 downto 0));

    -- read FSM
    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                rstate <= rdreset;
            elsif (ACLK_EN = '1') then
                rstate <= rnext;
            end if;
        end if;
    end process;

    process (rstate, ARVALID, RREADY, RVALID_t)
    begin
        case (rstate) is
        when rdidle =>
            if (ARVALID = '1') then
                rnext <= rddata;
            else
                rnext <= rdidle;
            end if;
        when rddata =>
            if (RREADY = '1' and RVALID_t = '1') then
                rnext <= rdidle;
            else
                rnext <= rddata;
            end if;
        when others =>
            rnext <= rdidle;
        end case;
    end process;

    rdata_proc : process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (ar_hs = '1') then
                    rdata_data <= (others => '0');
                    case (TO_INTEGER(raddr)) is
                    when ADDR_AP_CTRL =>
                        rdata_data(9) <= int_interrupt;
                        rdata_data(7) <= int_auto_restart;
                        rdata_data(3) <= int_ap_ready;
                        rdata_data(2) <= int_ap_idle;
                        rdata_data(1) <= int_task_ap_done;
                        rdata_data(0) <= int_ap_start;
                    when ADDR_GIE =>
                        rdata_data(0) <= int_gie;
                    when ADDR_IER =>
                        rdata_data(1 downto 0) <= int_ier;
                    when ADDR_ISR =>
                        rdata_data(1 downto 0) <= int_isr;
                    when ADDR_AP_RETURN_0 =>
                        rdata_data <= RESIZE(int_ap_return(31 downto 0), 32);
                    when ADDR_INPUT_R_DATA_0 =>
                        rdata_data <= RESIZE(int_input_r(31 downto 0), 32);
                    when ADDR_WEIGHTS_DATA_0 =>
                        rdata_data <= RESIZE(int_weights(31 downto 0), 32);
                    when ADDR_BIAS_DATA_0 =>
                        rdata_data <= RESIZE(int_bias(31 downto 0), 32);
                    when ADDR_SHIFT_DATA_0 =>
                        rdata_data <= RESIZE(int_shift(31 downto 0), 32);
                    when ADDR_OUTPUT_R_DATA_0 =>
                        rdata_data <= RESIZE(int_output_r(31 downto 0), 32);
                    when ADDR_CHANNEL_OFFSETS_DATA_0 =>
                        rdata_data <= RESIZE(int_channel_offsets(31 downto 0), 32);
                    when ADDR_CHANNEL_SHIFTS_DATA_0 =>
                        rdata_data <= RESIZE(int_channel_shifts(31 downto 0), 32);
                    when ADDR_OP_DATA_0 =>
                        rdata_data <= RESIZE(int_op(31 downto 0), 32);
                    when ADDR_CI_DATA_0 =>
                        rdata_data <= RESIZE(int_ci(31 downto 0), 32);
                    when ADDR_CO_DATA_0 =>
                        rdata_data <= RESIZE(int_co(31 downto 0), 32);
                    when ADDR_IH_DATA_0 =>
                        rdata_data <= RESIZE(int_ih(31 downto 0), 32);
                    when ADDR_IW_DATA_0 =>
                        rdata_data <= RESIZE(int_iw(31 downto 0), 32);
                    when ADDR_KERNEL_DATA_0 =>
                        rdata_data <= RESIZE(int_kernel(31 downto 0), 32);
                    when ADDR_STRIDE_DATA_0 =>
                        rdata_data <= RESIZE(int_stride(31 downto 0), 32);
                    when ADDR_PAD_DATA_0 =>
                        rdata_data <= RESIZE(int_pad(31 downto 0), 32);
                    when ADDR_GROUPS_DATA_0 =>
                        rdata_data <= RESIZE(int_groups(31 downto 0), 32);
                    when ADDR_RELU_DATA_0 =>
                        rdata_data <= RESIZE(int_relu(31 downto 0), 32);
                    when ADDR_USE_CHANNEL_OFFSETS_DATA_0 =>
                        rdata_data <= RESIZE(int_use_channel_offsets(31 downto 0), 32);
                    when ADDR_INPUT_SPAN_ELEMENTS_DATA_0 =>
                        rdata_data <= RESIZE(int_input_span_elements(31 downto 0), 32);
                    when others =>
                        NULL;
                    end case;
                end if;
            end if;
        end if;
    end process;

-- ----------------------- Register logic ----------------
    interrupt            <= int_interrupt;
    ap_start             <= int_ap_start;
    task_ap_done         <= (ap_done and not auto_restart_status) or auto_restart_done;
    task_ap_ready        <= ap_ready and not int_auto_restart;
    auto_restart_done    <= auto_restart_status and (ap_idle and not int_ap_idle);
    input_r              <= STD_LOGIC_VECTOR(int_input_r);
    weights              <= STD_LOGIC_VECTOR(int_weights);
    bias                 <= STD_LOGIC_VECTOR(int_bias);
    shift                <= STD_LOGIC_VECTOR(int_shift);
    output_r             <= STD_LOGIC_VECTOR(int_output_r);
    channel_offsets      <= STD_LOGIC_VECTOR(int_channel_offsets);
    channel_shifts       <= STD_LOGIC_VECTOR(int_channel_shifts);
    op                   <= STD_LOGIC_VECTOR(int_op);
    ci                   <= STD_LOGIC_VECTOR(int_ci);
    co                   <= STD_LOGIC_VECTOR(int_co);
    ih                   <= STD_LOGIC_VECTOR(int_ih);
    iw                   <= STD_LOGIC_VECTOR(int_iw);
    kernel               <= STD_LOGIC_VECTOR(int_kernel);
    stride               <= STD_LOGIC_VECTOR(int_stride);
    pad                  <= STD_LOGIC_VECTOR(int_pad);
    groups               <= STD_LOGIC_VECTOR(int_groups);
    relu                 <= STD_LOGIC_VECTOR(int_relu);
    use_channel_offsets  <= STD_LOGIC_VECTOR(int_use_channel_offsets);
    input_span_elements  <= STD_LOGIC_VECTOR(int_input_span_elements);

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                int_interrupt <= '0';
            elsif (ACLK_EN = '1') then
                if (int_gie = '1' and (int_isr(0) or int_isr(1)) = '1') then
                    int_interrupt <= '1';
                else
                    int_interrupt <= '0';
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                int_ap_start <= '0';
            elsif (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_AP_CTRL and WSTRB(0) = '1' and WDATA(0) = '1') then
                    int_ap_start <= '1';
                elsif (ap_ready = '1') then
                    int_ap_start <= int_auto_restart; -- clear on handshake/auto restart
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                int_ap_done <= '0';
            elsif (ACLK_EN = '1') then
                if (true) then
                    int_ap_done <= ap_done;
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                int_task_ap_done <= '0';
            elsif (ACLK_EN = '1') then
                if (task_ap_done = '1') then
                    int_task_ap_done <= '1';
                elsif (ar_hs = '1' and raddr = ADDR_AP_CTRL) then
                    int_task_ap_done <= '0'; -- clear on read
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                int_ap_idle <= '0';
            elsif (ACLK_EN = '1') then
                if (true) then
                    int_ap_idle <= ap_idle;
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                int_ap_ready <= '0';
            elsif (ACLK_EN = '1') then
                if (task_ap_ready = '1') then
                    int_ap_ready <= '1';
                elsif (ar_hs = '1' and raddr = ADDR_AP_CTRL) then
                    int_ap_ready <= '0';
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                int_auto_restart <= '0';
            elsif (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_AP_CTRL and WSTRB(0) = '1') then
                    int_auto_restart <= WDATA(7);
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                auto_restart_status <= '0';
            elsif (ACLK_EN = '1') then
                if (int_auto_restart = '1') then
                    auto_restart_status <= '1';
                elsif (ap_idle = '1') then
                    auto_restart_status <= '0';
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                int_gie <= '0';
            elsif (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_GIE and WSTRB(0) = '1') then
                    int_gie <= WDATA(0);
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                int_ier <= (others=>'0');
            elsif (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_IER and WSTRB(0) = '1') then
                    int_ier <= UNSIGNED(WDATA(1 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                int_isr(0) <= '0';
            elsif (ACLK_EN = '1') then
                if (int_ier(0) = '1' and ap_done = '1') then
                    int_isr(0) <= '1';
                elsif (w_hs = '1' and waddr = ADDR_ISR and WSTRB(0) = '1') then
                    int_isr(0) <= int_isr(0) xor WDATA(0); -- toggle on write
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                int_isr(1) <= '0';
            elsif (ACLK_EN = '1') then
                if (int_ier(1) = '1' and ap_ready = '1') then
                    int_isr(1) <= '1';
                elsif (w_hs = '1' and waddr = ADDR_ISR and WSTRB(0) = '1') then
                    int_isr(1) <= int_isr(1) xor WDATA(1); -- toggle on write
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ARESET = '1') then
                int_ap_return <= (others => '0');
            elsif (ACLK_EN = '1') then
                if (ap_done = '1') then
                    int_ap_return <= UNSIGNED(ap_return);
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_INPUT_R_DATA_0) then
                    int_input_r(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_input_r(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_WEIGHTS_DATA_0) then
                    int_weights(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_weights(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_BIAS_DATA_0) then
                    int_bias(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_bias(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_SHIFT_DATA_0) then
                    int_shift(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_shift(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_OUTPUT_R_DATA_0) then
                    int_output_r(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_output_r(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_CHANNEL_OFFSETS_DATA_0) then
                    int_channel_offsets(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_channel_offsets(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_CHANNEL_SHIFTS_DATA_0) then
                    int_channel_shifts(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_channel_shifts(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_OP_DATA_0) then
                    int_op(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_op(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_CI_DATA_0) then
                    int_ci(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_ci(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_CO_DATA_0) then
                    int_co(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_co(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_IH_DATA_0) then
                    int_ih(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_ih(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_IW_DATA_0) then
                    int_iw(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_iw(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_KERNEL_DATA_0) then
                    int_kernel(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_kernel(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_STRIDE_DATA_0) then
                    int_stride(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_stride(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_PAD_DATA_0) then
                    int_pad(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_pad(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_GROUPS_DATA_0) then
                    int_groups(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_groups(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_RELU_DATA_0) then
                    int_relu(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_relu(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_USE_CHANNEL_OFFSETS_DATA_0) then
                    int_use_channel_offsets(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_use_channel_offsets(31 downto 0));
                end if;
            end if;
        end if;
    end process;

    process (ACLK)
    begin
        if (ACLK'event and ACLK = '1') then
            if (ACLK_EN = '1') then
                if (w_hs = '1' and waddr = ADDR_INPUT_SPAN_ELEMENTS_DATA_0) then
                    int_input_span_elements(31 downto 0) <= (UNSIGNED(WDATA(31 downto 0)) and wmask(31 downto 0)) or ((not wmask(31 downto 0)) and int_input_span_elements(31 downto 0));
                end if;
            end if;
        end if;
    end process;


-- ----------------------- Memory logic ------------------

end architecture behave;
