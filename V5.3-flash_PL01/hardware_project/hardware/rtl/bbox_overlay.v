`timescale 1ns/1ps

// Both box triples are held stable until ack_toggle matches shadow3[0].
// The pixel domain samples the bus after synchronizing the commit toggle and
// changes both displayed boxes atomically at the next frame_start.
module bbox_overlay (
    input wire ctrl_clk, input wire ctrl_rst_n,
    input wire pix_clk, input wire pix_rst,
    input wire [31:0] shadow0, shadow1, shadow2, shadow3,
    input wire [31:0] shadow4, shadow5, shadow6,
    (* ASYNC_REG = "TRUE" *) output reg ack_toggle,
    input wire de, input wire frame_start,
    input wire [7:0] red_in, green_in, blue_in,
    output wire [7:0] red_out, green_out, blue_out
);
    (* ASYNC_REG = "TRUE" *) reg [31:0] meta0,meta1,meta2;
    (* ASYNC_REG = "TRUE" *) reg [31:0] sync0,sync1,sync2;
    (* ASYNC_REG = "TRUE" *) reg [31:0] meta4,meta5,meta6;
    (* ASYNC_REG = "TRUE" *) reg [31:0] sync4,sync5,sync6;
    (* ASYNC_REG = "TRUE" *) reg commit_meta,commit_sync;
    reg seen_commit;
    reg [31:0] pending0,pending1,pending2;
    reg [31:0] pending4,pending5,pending6;
    reg pending_valid,pending_toggle;
    reg [31:0] active0,active1,active2;
    reg [31:0] active4,active5,active6;
    reg ack_pix;
    (* ASYNC_REG = "TRUE" *) reg ack_meta;
    reg [10:0] x;
    reg [9:0] y;
    reg prev_de;

    always @(posedge pix_clk) begin
        if (pix_rst) begin
            meta0<=0;meta1<=0;meta2<=0;
            sync0<=0;sync1<=0;sync2<=0;
            meta4<=0;meta5<=0;meta6<=0;
            sync4<=0;sync5<=0;sync6<=0;
            commit_meta<=0;commit_sync<=0;seen_commit<=0;
            pending0<=0;pending1<=0;pending2<=0;
            pending4<=0;pending5<=0;pending6<=0;
            pending_valid<=0;pending_toggle<=0;
            active0<=0;active1<=0;active2<=0;ack_pix<=0;
            active4<=0;active5<=0;active6<=0;
            x<=0;y<=0;prev_de<=0;
        end else begin
            meta0<=shadow0;meta1<=shadow1;meta2<=shadow2;
            sync0<=meta0;sync1<=meta1;sync2<=meta2;
            meta4<=shadow4;meta5<=shadow5;meta6<=shadow6;
            sync4<=meta4;sync5<=meta5;sync6<=meta6;
            commit_meta<=shadow3[0];commit_sync<=commit_meta;
            if (commit_sync != seen_commit) begin
                pending0<=sync0;pending1<=sync1;pending2<=sync2;
                pending4<=sync4;pending5<=sync5;pending6<=sync6;
                pending_toggle<=commit_sync;pending_valid<=1;
                seen_commit<=commit_sync;
            end
            if (frame_start && pending_valid) begin
                active0<=pending0;active1<=pending1;active2<=pending2;
                active4<=pending4;active5<=pending5;active6<=pending6;
                pending_valid<=0;ack_pix<=pending_toggle;
            end
            prev_de<=de;
            if (frame_start) begin
                x<=de?11'd1:11'd0;y<=0;
            end else if (de) begin
                x<=x+1'b1;
            end else if (prev_de) begin
                x<=0;y<=y+1'b1;
            end
        end
    end

    always @(posedge ctrl_clk) begin
        if (!ctrl_rst_n) begin ack_meta<=0;ack_toggle<=0;end
        else begin ack_meta<=ack_pix;ack_toggle<=ack_meta;end
    end

    wire [31:0] box0=(frame_start && pending_valid)?pending0:active0;
    wire [31:0] box1=(frame_start && pending_valid)?pending1:active1;
    wire [31:0] box2=(frame_start && pending_valid)?pending2:active2;
    wire [31:0] box4=(frame_start && pending_valid)?pending4:active4;
    wire [31:0] box5=(frame_start && pending_valid)?pending5:active5;
    wire [31:0] box6=(frame_start && pending_valid)?pending6:active6;
    wire [10:0] px=frame_start?11'd0:x;
    wire [9:0] py=frame_start?10'd0:y;
    wire [10:0] x1=box1[10:0],x2=box2[10:0];
    wire [10:0] y1=box1[21:11],y2=box2[21:11];
    wire in_box=de && box0[0] && x1<=x2 && y1<=y2 &&
                px>=x1 && px<=x2 && py>=y1 && py<=y2;
    wire border0=in_box && (px<x1+3 || px+3>x2 || py<y1+3 || py+3>y2);
    wire [10:0] bx1=box5[10:0],bx2=box6[10:0];
    wire [10:0] by1=box5[21:11],by2=box6[21:11];
    wire in_box1=de && box4[0] && bx1<=bx2 && by1<=by2 &&
                 px>=bx1 && px<=bx2 && py>=by1 && py<=by2;
    wire border1=in_box1 && (px<bx1+3 || px+3>bx2 || py<by1+3 || py+3>by2);
    wire border=border0 || border1;
    // Slot0 takes priority only where the two borders intersect.
    wire [1:0] class_id=border0?box0[2:1]:box4[2:1];
    assign red_out=border?((class_id==2'd2)?8'hff:8'h00):red_in;
    assign green_out=border?((class_id==2'd0 || class_id==2'd1)?8'hff:8'h00):green_in;
    assign blue_out=border?((class_id==2'd1 || class_id==2'd2)?8'hff:8'h00):blue_in;
endmodule
