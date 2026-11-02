# PYNQ 跨项目复用案例：贪吃蛇

本次参赛作品是基于 PYNQ 的手势与语音交互系统。贪吃蛇是另一项独立工程，在这里作为跨项目复用案例，展示 PYNQ 接入方法在不同应用中的使用。两个工程共用处理器系统配置及 Overlay、寄存器访问方法，并根据各自功能分别设计 PL 逻辑和 PS 应用。

手势交互工程的 PL 负责摄像头和显示通路，PS 运行手势模型和交互程序。贪吃蛇工程使用独立的 Overlay，PL 负责按键和数码管，PS 运行游戏逻辑，再通过串口将状态传至电脑界面。

## 相同的处理器系统配置

两个工程中的处理器系统配置 Tcl 文件 SHA-256 均为：

`1c5d71cbda919256d857d4bfaffa822062a8a77a42129400c53f4974b4ddc9bc`

贪吃蛇的 `scripts/create_project.tcl` 和手势交互工程的 `hardware_camera_only/hardware/bd/system_bd.tcl` 均会加载这一配置并调用 `set_ps_config`。文件位置、哈希和调用语句见 [ps_config_identity.json](ps_config_identity.json)。两个应用各自使用独立的硬件设计和位流。

## 贪吃蛇项目验证

项目记录显示，PYNQ 环境下已加载项目 Overlay，时钟、复位、门控与 PL 状态签名检查通过；六键与数码管完成校准，用户确认显示及分数、时间一致。原始记录中的相关字段及文件哈希见 [snake_board_excerpt.json](snake_board_excerpt.json)。

RTL 仿真记录显示 21 项检查通过，结果与原始日志哈希见 [snake_rtl_excerpt.txt](snake_rtl_excerpt.txt)。两份摘录对应各自的检查过程，可与原工程归档中的文件对照。

## 技能提炼

结合两个已有工程的开发经验，团队整理出 Overlay 加载、接口核对、寄存器访问和验证记录的通用步骤，形成 `pynq-overlay-workflow/`。使用时按新项目填写接口契约、硬件文件和运行参数；应用逻辑与 PL 设计按实际需求实现。

原始材料分别位于 `Task4_pynq_snake.zip` 和 `v53_competition_complete_record.zip`。下列摘录保留了可核对的源文件相对路径及 SHA-256；`pynq-overlay-workflow/verification/source-manifest.json` 还记录了技能提炼时读取的其他工程文件。
