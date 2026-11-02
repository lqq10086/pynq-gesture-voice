# 系统音频与语音运行

语音采集使用目标系统提供的 ALSA、`arecord`（一般由 `alsa-utils` 提供）和与当前内核匹配的 USB 音频驱动。工程保留语音识别代码和模型；启动时检查系统中的 `arecord` 和声卡，未检测到时关闭语音并继续运行手势与视频功能。

连接接收器后，可用 `arecord -l` 和 `cat /proc/asound/cards` 查看采集设备。当前 `board_config.json` 的设备为 `plughw:CARD=WirelessMic,DEV=0`，启动检查也要求声卡名包含 `WirelessMic`；请按照目标系统的 USB 音频驱动配置确保该名称存在，再根据实际设备调整采集参数。设备枚举成功表示可以开始尝试采集，语音识别效果需在目标板上实测。

## 接收器 HID 配置

当前接收器的 USB HID 保护脚本只处理 VID:PID 为 `3634:4155`、HID 接口号为 2 且序列号完全匹配的设备。需要临时释放接收器 HID 带宽时，在运行程序的环境中设置 `V53_USB_AUDIO_SERIAL` 为本机接收器的实际序列号；不要将序列号写入公开仓库。启动后该接口可临时解绑，程序关闭时按保存的设备身份恢复，音频接口不参与解绑。

如果没有设置序列号，脚本不会解绑任何 HID 设备，语音采集仍会按系统声卡状态尝试；若先前存在尚未恢复的解绑记录，则保留该记录以便恢复。运行目录中的 `audio_hid.log` 和 `audio_restore.log` 可查看处理状态；`UNAVAILABLE_ARECORD_NOT_FOUND`、`UNAVAILABLE_WIRELESS_MIC_NOT_ENUMERATED` 分别表示缺少采集工具或未找到指定声卡。
