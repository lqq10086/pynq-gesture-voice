# 第三方材料与来源

根目录 MIT 许可用于团队有权授权的自有代码。不同来源的模型、字体、硬件 IP 和生成文件按相应许可或授权使用。

| 材料 | 来源与说明 |
| --- | --- |
| 手势模型 | `V5.3-flash_PL01/models/gesture_best.onnx` 为团队训练的 YOLO-FastestV2 模型；训练代码、初始权重和数据的来源及分发授权待核实。上游项目与版本记录在 `models/model_contract.json`，上游源码及相关模型材料的授权仍需确认。 |
| 语音模型 | `V5.3-flash_PL01/voice_assets/` 包含语音模型及运行参数；模型训练来源与分发授权待核实。 |
| 小镇场景素材 | `V5.3-flash_PL01/living_town/assets/town/` 中的树、房屋、汽车、云和烟雾等素材由团队制作。 |
| 字体 | 字体及其来源说明、许可分别位于 `assets/`、`licenses/` 和 `living_town/assets/menu_font_OFL.txt`。 |
| ALINX 板级配置 | `hardware_project/scripts/ps_config_ax7020_official_2023_1.tcl` 和 `hardware_camera_only/scripts/ps_config_ax7020_official_2023_1.tcl` 保留了 ALINX 版权及使用声明；应遵守原文件中的条件，并核对所提及的相关声明。 |
| AMD IP 和 HLS 生成文件 | Q1 硬件设计调用 AMD IP，`hls/build_csim/solution1/impl/ip/` 含 Vitis 生成文件；保留原版权标识，适用条款和分发形式需按对应工具及 IP 许可确认。Q1 bit/HWH 位于 `reports/hardware_recovery_435f97f3_20261026_170641_ead3b9fa/`。 |
| 布线检查点 | `final_routed.dcp` 未放入本公开候选包。原构建的完整检查会核验该文件；公开版缺少该文件时单独标记校验范围。持有匹配原工程的使用者可通过 `tools/restore_q1_dcp.py` 恢复。 |
| OV5640 自动对焦固件 | 工程含 OV5640 自动对焦固件，相关字节与 OpenMV 的 OV5640 驱动中固件数组一致；应核对实际来源及适用许可。[OpenMV 驱动及许可声明](https://github.com/openmv/openmv/blob/master/drivers/sensors/ov5640.c)；该文件许可保存在 `licenses/OpenMV_MIT.txt`。 |
| 系统音频 | 语音采集使用目标系统提供的 ALSA、`arecord` 和 USB 音频驱动，公开候选包不携带原先单独构建的对应二进制和内核模块。 |

除特别指出外，表中路径位于 `V5.3-flash_PL01/` 下。
