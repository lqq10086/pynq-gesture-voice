"""轻量级 VAD (Voice Activity Detection) —— 纯 numpy 实现, 无需任何模型。

设计目标
--------
* **极轻量**: 只做帧级能量 + 过零率统计, 单帧计算量约几微秒, CPU 占用可忽略。
* **自适应**: 在线估计背景噪声底 (noise floor), 因此不需要用户手动设阈值。
* **带滞回**: 起始需要连续 N 帧超阈、结束需要连续 M 帧低于阈, 避免把一声咳嗽或
  单个爆音当成一段语音 (这是实时关键词识别最容易误触发的地方)。
* **可替换**: 提供统一接口 ``is_speech(frame) -> bool`` + 状态机 ``VadSegmenter``。
  若想换更强的 VAD (如 WebRTC VAD / Silero), 实现同一个 ``frame_is_speech`` 接口即可,
  主程序不用改。

算法
----
对每个 30 ms 帧:
    1. 去均值后算 RMS -> 转 dBFS: 20*log10(rms + 1e-10)
    2. 算过零率 ZCR = 符号变化次数 / 帧长
    3. 自适应噪声底: 安静帧用较快系数下压、语音帧用极慢系数上抬 (典型噪声跟踪做法)
    4. 判决: dBFS > max(noise_floor + ratio_db, min_dbfs) 且 ZCR < zcr_max
       —— ZCR 上界用来排除高频噪声/啸叫等"能量高但不是人声"的信号
    5. 状态机: 起始/结束都需要连续帧数满足条件 (滞回)

这样在没有语音时, 主程序完全不调用 ONNX 模型, 这正是"用 VAD 判断是否当前是关键词"
的前置门控。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

# Use the same spectral test without loading a second numerical runtime.
from numpy.fft import rfft


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass
class VadConfig:
    """VAD 参数 (全部可在配置文件的 realtime 段或命令行覆盖)。"""

    sample_rate: int = 16000
    frame_ms: float = 30.0            # 帧长 30 ms (16 kHz -> 480 点)
    ratio_db: float = 6.0             # 超过噪声底多少 dB 视为语音
    min_dbfs: float = -55.0            # 绝对能量下限 (防止噪声底过低导致全是语音)
    zcr_max: float = 0.55             # 过零率上界: 超过它则要求更高能量 (见 zcr_extra_db)
    zcr_extra_db: float = 6.0         # 高过零率帧额外需要的能量 (dB) —— 用来排除白噪声
    flatness_max: float = 0.50        # 谱平坦度上界: 纯噪声趋于 1.0, 人声(有共振峰)明显更低
    flatness_extra_db: float = 8.0    # 谱过于平坦时额外需要的能量 (dB)
    noise_adapt_noisy: float = 0.02   # "被判为语音但谱很平坦"时的噪声底上抬系数 (破死锁用)
    noise_floor_dbfs: float = -60.0   # 噪声底下限 (dBFS): 再安静的房间也不会低于此值
    noise_ceiling_dbfs: float = -30.0 # 噪声底上限 (dBFS): 很嘈杂的环境里允许抬到这里
    start_frames: int = 3             # 连续多少帧判为语音才认为"开始说话" (~90 ms)
    end_frames: int = 10              # 连续多少帧判为静音才认为"说完" (~300 ms)
    noise_init_dbfs: float = -50.0    # 噪声底初值
    noise_adapt_up: float = 0.02      # 语音期间噪声底上抬系数 (慢)
    noise_adapt_down: float = 0.25    # 静音期间噪声底下压系数 (快)
    noise_warmup_frames: int = 5      # 启动预热帧数: 用这几帧里"最安静"的一帧来定噪声底
    noise_warmup_margin_db: float = 3.0  # 预热后噪声底再留出多少 dB 余量
    settle_frames: int = 10           # 预热后再收敛多少帧才开始接受判定 (~300 ms)
    min_observe_frames: int = 20      # 至少观测这么多帧 (~600 ms) 才开始接受语音判定
    idle_ratchet: float = 0.08        # **空闲期**噪声底向真实环境电平爬升的系数 (见 _ratchet)
    idle_ratchet_window: int = 48     # 空闲期电平滑窗长度 (~1.44 s)
    idle_ratchet_pct: float = 40.0    # 用滑窗的哪个百分位作为环境电平估计 (比均值抗突发)
    pre_roll_frames: int = 6          # 起始时向前保留的帧数 (~180 ms), 避免切掉词头
    max_utterance_ms: float = 3000.0  # 单段语音最长时长, 超过则强制切段

    @property
    def frame_length(self) -> int:
        return int(round(self.frame_ms * self.sample_rate / 1000.0))


# ---------------------------------------------------------------------------
# 帧级判决
# ---------------------------------------------------------------------------
class EnergyVad:
    """基于能量 + 过零率的轻量 VAD (帧级判决 + 自适应噪声底)。"""

    name = "energy-zcr"

    def __init__(self, cfg: Optional[VadConfig] = None) -> None:
        self.cfg = cfg or VadConfig()
        self.noise_dbfs = float(self.cfg.noise_init_dbfs)
        self._frames_seen = 0
        self._warmup_levels: List[float] = []
        self._idle_levels: List[float] = []
        self.warmup_done = False
        # 是否已经被"显式标定"过 (见 calibrate / prime_from_waveform)。
        # 标定过就意味着噪声底已经对准了真实环境, 启动期的预热/收敛/最小观测三段
        # 就不再需要 —— 那三段是给"麦克风刚开、还不知道环境底噪"的流式场景用的。
        # 离线处理一个 1 秒的 wav 时, 三段加起来要 ~1 秒, 会把整个词吃掉。
        self.calibrated = False

    # -- 统计量 -------------------------------------------------------------
    @staticmethod
    def frame_dbfs(frame: np.ndarray) -> float:
        """帧能量 (dBFS)。"""
        x = np.asarray(frame, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return -100.0
        x = x - float(x.mean())
        rms = float(np.sqrt(np.mean(x * x)))
        return 20.0 * float(np.log10(rms + 1e-10))

    @staticmethod
    def frame_zcr(frame: np.ndarray) -> float:
        """过零率 (0~1)。"""
        x = np.asarray(frame, dtype=np.float32).reshape(-1)
        if x.size < 2:
            return 0.0
        s = np.signbit(x)
        return float(np.count_nonzero(s[1:] != s[:-1])) / float(x.size - 1)

    @staticmethod
    def frame_flatness(frame: np.ndarray, n_fft: int = 512) -> float:
        """谱平坦度 = 功率谱的几何平均 / 算术平均 (0~1)。

        白噪声/稳态底噪的频谱是"平"的 -> 接近 1; 人声有共振峰结构 -> 明显更低。
        这是区分"响但不像人声"的噪声与真正语音最有效的一个廉价特征
        (只需要一次 rFFT, 比 ZCR 更能反映频谱形状)。
        """
        x = np.asarray(frame, dtype=np.float32).reshape(-1)
        if x.size < 8:
            return 1.0
        x = x - float(x.mean())
        w = np.hanning(x.size).astype(np.float32)
        spec = rfft(x * w, n=n_fft)
        p = (spec.real ** 2 + spec.imag ** 2).astype(np.float64)
        p = p[1:]                      # 去掉直流分量
        if p.size == 0:
            return 1.0
        p = np.maximum(p, 1e-12)
        geo = float(np.exp(np.mean(np.log(p))))
        ari = float(np.mean(p))
        if ari <= 0:
            return 1.0
        return float(min(geo / ari, 1.0))

    # -- 判决 ---------------------------------------------------------------
    def is_speech(self, frame: np.ndarray, adapt: bool = True,
                  in_speech: bool = False) -> bool:
        """对单帧判决, 并在线更新噪声底。

        ``in_speech``: 段状态机当前是否处在一段语音内部。空闲期允许噪声底向上爬升
        (打破"阈值低于环境噪声"的自锁), 段内则不允许 (否则长口号的词尾会被削掉)。
        """
        cfg = self.cfg
        db = self.frame_dbfs(frame)
        zcr = self.frame_zcr(frame)
        self._frames_seen += 1

        # 判决阈值 = max(噪声底 + ratio, 绝对下限)
        threshold = max(self.noise_dbfs + cfg.ratio_db, cfg.min_dbfs)
        # 实测结论: 人声 ZCR 中位数约 0.05~0.15 (摩擦音可达 0.8), 白噪声/底噪约 0.34~0.50,
        # 两者重叠, 单靠一个 ZCR 硬门限切不开。因此这里用"惩罚"而不是"否决":
        # 谱平坦度高(像噪声)或过零率高(像噪声)的帧, 必须额外多出若干 dB 能量才算语音。
        # 白噪声能量恒定达不到; 人声即使出现高 ZCR 的摩擦音帧, 谱结构仍不平坦、能量也够。
        if zcr >= cfg.zcr_max:
            threshold += cfg.zcr_extra_db
        flatness = self.frame_flatness(frame)
        if cfg.flatness_extra_db and flatness >= cfg.flatness_max:
            threshold += cfg.flatness_extra_db
        speech = db > threshold

        if adapt:
            if self.calibrated:
                # 已显式标定: 噪声底已经对准环境, 直接进入正常判决与自适应。
                # 三种情况: 非语音帧快跟、空闲期爬升、段内平坦噪声慢抬。
                idle = not in_speech
                if idle:
                    self._push_idle_level(db)
                if not speech:
                    self.noise_dbfs += cfg.noise_adapt_down * (db - self.noise_dbfs)
                elif idle:
                    self._ratchet_idle_floor()
                elif flatness >= cfg.flatness_max:
                    self.noise_dbfs += cfg.noise_adapt_noisy * (db - self.noise_dbfs)
                self._clamp_noise_floor()
                return bool(speech)
            if self._frames_seen <= cfg.noise_warmup_frames:
                # 启动预热: 收集这几帧的电平, 用**最安静的一帧**来标定噪声底。
                # 为什么必须这样: 程序一启动可能正好撞上说话的中途 (或测试音频第一个样本
                # 就是语音), 若拿第一帧当噪声底, 阈值会直接高于信号本身, 于是永远判不出语音。
                # 取最小值则无论测到的是语音还是噪声, 都能得到一个偏保守(偏低)的底。
                self._warmup_levels.append(db)
                if self._frames_seen == cfg.noise_warmup_frames:
                    self._finish_warmup()
                self._clamp_noise_floor()
                return False
            if self._frames_seen <= cfg.noise_warmup_frames + cfg.settle_frames:
                # 收敛期: 此时噪声底可能还比真实噪声略低 (尤其刚开机就处在噪声环境里),
                # 若立刻接受判定, 开头几百毫秒会把稳态噪声当成一段语音。这里先用较快的
                # 系数把噪声底跟上去, 期间不做任何语音判定。
                self.noise_dbfs += cfg.noise_adapt_down * (db - self.noise_dbfs)
                self._clamp_noise_floor()
                return False
            if self._frames_seen <= cfg.min_observe_frames:
                # 最小观测期: 还没攒够可用于估计环境电平的帧, 先不收判定, 只把电平记进滑窗。
                self._push_idle_level(db)
                self._clamp_noise_floor()
                return False
            idle = not in_speech
            if idle:
                self._push_idle_level(db)
            if not speech:
                # 正常情况: 非语音帧直接跟随噪声底 (快)
                self.noise_dbfs += cfg.noise_adapt_down * (db - self.noise_dbfs)
            elif idle:
                # **空闲期爬升 (关键修复)**: 段状态机不在语音段里, 却把这一帧判成了语音 ——
                # 说明当前阈值低于真实环境噪声。必须让噪声底往上爬, 否则 VAD 会永久失锁。
                #
                # 这是实测踩到的真实故障: 环境噪声 -33 dBFS (风扇/空调), 而阈值被
                # max(噪声底 + ratio, min_dbfs) 钉在 -54 dBFS, 于是每一帧都是"语音",
                # 状态机只能靠 3 s 超长强切分段, 一段真实口令被判成 3 s 长, 全部输出 unknown。
                # 旧代码只在这帧"谱很平坦"时才抬噪声底, 而风扇噪声的谱平坦度只有 0.01~0.04
                # (比人声还低), 永远不触发 -> 噪声底卡死在 -60, 自锁无解。
                self._ratchet_idle_floor()
            elif flatness >= cfg.flatness_max:
                # 保底 (段内): 这一帧被判成语音但谱是平的 —— 极可能是持续的稳态噪声/白噪声
                # 而不是人声。此时若完全不动噪声底就会出现自锁; 用很慢的系数上抬。
                # 注意段内不做"空闲爬升": 正在说一段话时抬高阈值会把词尾削掉。
                self.noise_dbfs += cfg.noise_adapt_noisy * (db - self.noise_dbfs)
            # 其余(段内、被判为语音、谱不像噪声)的帧完全不更新噪声底

            self._clamp_noise_floor()
        return bool(speech)

    def _push_idle_level(self, db: float) -> None:
        self._idle_levels.append(float(db))
        if len(self._idle_levels) > int(self.cfg.idle_ratchet_window):
            del self._idle_levels[0]

    def _ratchet_idle_floor(self) -> None:
        """空闲期让噪声底朝实测环境电平缓慢爬升 (只上不下)。

        为什么用百分位而不是均值: 空闲滑窗里可能混进一次很短的响动 (咳嗽/敲键盘),
        取 40 分位比均值更能代表"稳态环境"。
        为什么只上不下: 下压由非语音帧的快速跟踪负责; 这里只负责打破"噪声底被钉在
        下限、阈值低于环境噪声"的自锁。
        """
        cfg = self.cfg
        if not self._idle_levels:
            return
        ambient = float(np.percentile(np.asarray(self._idle_levels, dtype=np.float32),
                                      cfg.idle_ratchet_pct))
        # 目标: 环境电平之上留 ratio_db 的余量, 但不超过 noise_ceiling_dbfs + ratio_db
        target = min(ambient, cfg.noise_ceiling_dbfs)
        if target > self.noise_dbfs:
            self.noise_dbfs += cfg.idle_ratchet * (target - self.noise_dbfs)

    def calibrate(self, frames) -> float:
        """用一段"应该没人说话"的音频显式标定噪声底, 返回标定后的噪声底。

        用途: 环境噪声高的房间里, 启动时先安静收 ~1 s, 能把噪声底一次定到环境电平,
        不必等空闲爬升慢慢逼近。标定期间不做语音判定。
        """
        cfg = self.cfg
        levels = [self.frame_dbfs(f) for f in frames]
        if not levels:
            return self.noise_dbfs
        ambient = float(np.percentile(np.asarray(levels, dtype=np.float32), cfg.idle_ratchet_pct))
        self.noise_dbfs = min(ambient, cfg.noise_ceiling_dbfs)
        self._idle_levels = list(levels)[-int(cfg.idle_ratchet_window):]
        self.warmup_done = True
        self.calibrated = True
        self._clamp_noise_floor()
        return self.noise_dbfs

    # -- 噪声底标定 ---------------------------------------------------------
    def _finish_warmup(self) -> None:
        """预热结束: 用观测到的最安静电平标定噪声底。"""
        cfg = self.cfg
        quietest = min(self._warmup_levels) if self._warmup_levels else cfg.noise_init_dbfs
        # 预热的这几帧里可能全是语音 (例如程序刚启动就开始说话, 或测试音频开头就是词),
        # 此时"最安静的一帧"其实是语音电平, 减掉余量后门限仍可能高于信号 -> 漏检。
        # 因此这里同时参考配置的初值, 取两者中更低的一个。
        floor = min(quietest, cfg.noise_init_dbfs) - cfg.noise_warmup_margin_db
        self.noise_dbfs = floor
        self._clamp_noise_floor()
        self.warmup_done = True

    def threshold_dbfs(self) -> float:
        """当前判决阈值 (dBFS), 便于调试/显示。"""
        cfg = self.cfg
        return max(self.noise_dbfs + cfg.ratio_db, cfg.min_dbfs)

    def _clamp_noise_floor(self) -> None:
        """把噪声底限制在合理区间。

        * 下限 noise_floor_dbfs: 防止数字静音(全零, -200 dBFS)把阈值拖到不合理的低位
          (否则安静房间里一点风声都会被判成语音)。
        * 上限 noise_ceiling_dbfs: 允许噪声底在嘈杂环境里抬到较高水平。这很关键 ——
          如果噪声底被卡在很低的固定值, 那么"风扇/空调很响"的环境里噪声本身就超过阈值,
          每一帧都会被判成语音, 整个 VAD 直接失效。
        """
        cfg = self.cfg
        lo = cfg.noise_floor_dbfs
        hi = max(cfg.noise_ceiling_dbfs, lo)
        if self.noise_dbfs < lo:
            self.noise_dbfs = lo
        elif self.noise_dbfs > hi:
            self.noise_dbfs = hi


# ---------------------------------------------------------------------------
# 可选: WebRTC VAD (若安装了 webrtcvad, 可用它替代能量法)
# ---------------------------------------------------------------------------
class WebRtcVad:
    """WebRTC VAD 封装 (需要 ``pip install webrtcvad``; 没装则不会被使用)。

    注意: WebRTC VAD 只接受 8/16/32/48 kHz 且帧长必须是 10/20/30 ms,
    因此配置里的 frame_ms 只能是 10/20/30。
    """

    name = "webrtc"

    def __init__(self, cfg: Optional[VadConfig] = None, aggressiveness: int = 2) -> None:
        import webrtcvad  # 延迟导入

        self.cfg = cfg or VadConfig()
        if int(self.cfg.frame_ms) not in (10, 20, 30):
            raise ValueError("WebRTC VAD 的帧长必须是 10/20/30 ms")
        if self.cfg.sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError("WebRTC VAD 采样率必须是 8/16/32/48 kHz")
        self._vad = webrtcvad.Vad(int(aggressiveness))

    def is_speech(self, frame: np.ndarray, adapt: bool = True,
                  in_speech: bool = False) -> bool:
        pcm = np.clip(np.asarray(frame, dtype=np.float32).reshape(-1), -1.0, 1.0)
        pcm16 = (pcm * 32767.0).astype("<i2").tobytes()
        return bool(self._vad.is_speech(pcm16, self.cfg.sample_rate))


def build_vad(cfg: Optional[VadConfig] = None, backend: str = "auto"):
    """按名字构造 VAD。

    backend: ``auto`` | ``energy`` | ``webrtc``
        auto 会优先尝试 webrtcvad, 不可用时自动回退到内置能量法。
    """
    cfg = cfg or VadConfig()
    backend = (backend or "auto").lower()
    if backend in ("auto", "webrtc"):
        try:
            return WebRtcVad(cfg)
        except Exception:
            if backend == "webrtc":
                raise
    return EnergyVad(cfg)


# ---------------------------------------------------------------------------
# 段级状态机: 把帧流切成"一段语音"
# ---------------------------------------------------------------------------
@dataclass
class SegmentResult:
    """一次"说完一段话"的结果。"""

    audio: np.ndarray                 # 该段波形 (16 kHz float32, 已含 pre-roll)
    start_time: float                 # 相对程序启动的开始时间 (秒)
    end_time: float
    duration: float
    forced: bool = False              # True = 因超长被强制切段, 而非检测到静音
    frames: int = 0


class VadSegmenter:
    """把连续帧流切成语音段 (带起始/结束滞回 + pre-roll + 超长强切)。

    典型用法::

        seg = VadSegmenter(vad)
        for frame in frames:
            done = seg.push(frame)       # 返回 SegmentResult 或 None
            if done: handle(done.audio)
        tail = seg.flush()               # 结束时收尾
    """

    def __init__(self, vad, cfg: Optional[VadConfig] = None) -> None:
        self.vad = vad
        self.cfg = cfg or getattr(vad, "cfg", VadConfig())
        self._ring: List[np.ndarray] = []     # 最近若干帧 (用于 pre-roll)
        self._ring_cap = max(self.cfg.pre_roll_frames, 1)
        # 启动观测期内的 pre-roll 需要更长: 前 min_observe_frames 帧不接受判定,
        # 若这段时间里用户已经在说第一个口令, 短的 pre-roll 会把这个词头整个丢掉。
        self._ring_cap_startup = max(self._ring_cap, int(getattr(self.cfg, "min_observe_frames", 0)))
        self._active = False
        self._buf: List[np.ndarray] = []
        self._speech_run = 0
        self._silence_run = 0
        self._frames_pushed = 0
        self._seg_start_frame = 0

    # -- 主接口 -------------------------------------------------------------
    def push(self, frame: np.ndarray) -> Optional[SegmentResult]:
        """送入一帧 (长度应为 cfg.frame_length), 返回完成的一段或 None。"""
        frame = np.asarray(frame, dtype=np.float32).reshape(-1)
        cfg = self.cfg
        self._frames_pushed += 1
        speech = self.vad.is_speech(frame, in_speech=self._active)

        if not self._active:
            # 维护 pre-roll 环形缓冲: 一旦开始说话, 把之前的帧一起纳入
            self._ring.append(frame)
            cap = self._ring_cap_startup if self._frames_pushed <= self._ring_cap_startup else self._ring_cap
            if len(self._ring) > cap:
                self._ring.pop(0)
            if speech:
                self._speech_run += 1
                if self._speech_run >= cfg.start_frames:
                    self._active = True
                    self._buf = list(self._ring)
                    self._silence_run = 0
                    self._seg_start_frame = self._frames_pushed - len(self._buf)
                    self._ring = []
            else:
                self._speech_run = 0
            return None

        # 已经在语音段中
        self._buf.append(frame)
        if speech:
            self._silence_run = 0
        else:
            self._silence_run += 1

        cur_ms = len(self._buf) * cfg.frame_ms
        if self._silence_run >= cfg.end_frames:
            return self._close(forced=False)
        if cur_ms >= cfg.max_utterance_ms:
            return self._close(forced=True)
        return None

    def flush(self) -> Optional[SegmentResult]:
        """音频流结束/程序退出时收尾, 返回未结束的语音段。"""
        if self._active and self._buf:
            return self._close(forced=True)
        return None

    # -- 内部 ---------------------------------------------------------------
    def _close(self, forced: bool) -> SegmentResult:
        audio = np.concatenate(self._buf).astype(np.float32) if self._buf else np.zeros(0, np.float32)
        fms = self.cfg.frame_ms
        # 段的绝对起点 = 已推送帧数 - 本段包含的帧数 (含 pre-roll)
        # 段的终点 = 当前已推送到的位置 (即收到这一帧的时刻), 而不是 buf 长度推算,
        # 否则 pre-roll 会让 end_time 偏早。
        start_frame = self._frames_pushed - len(self._buf)
        start_t = start_frame * fms / 1000.0
        end_t = self._frames_pushed * fms / 1000.0
        res = SegmentResult(
            audio=audio,
            start_time=start_t,
            end_time=end_t,
            duration=len(audio) / float(self.cfg.sample_rate),
            forced=forced,
            frames=len(self._buf),
        )
        # 复位状态机 (保留噪声底继续自适应)
        self._buf = []
        self._active = False
        self._speech_run = 0
        self._silence_run = 0
        self._ring = []
        return res

    @property
    def in_speech(self) -> bool:
        return self._active


# ---------------------------------------------------------------------------
# 离线 (文件) 输入的显式标定
# ---------------------------------------------------------------------------
def prime_from_waveform(vad, audio: np.ndarray, quiet_pct: float = 30.0) -> float:
    """用**整段离线音频**先给 VAD 标定噪声底, 并跳过启动观测期。

    为什么需要它
    ------------
    `min_observe_frames` / `settle_frames` / `noise_warmup_frames` 这三段启动期
    (默认加起来约 1 秒) 是为**流式麦克风**设计的: 刚开麦时还不知道环境底噪, 先听
    一会儿再判定, 可以避免把稳态噪声当成一段语音。但离线处理一个 1 秒的 wav 时,
    这三段会把整个词吃掉 —— 实测 TTS 生成的 1 秒单词音频有一半切出 0 段。

    做法: 把整段音频按帧长切开, 取电平的 `quiet_pct` 分位数当环境电平 (与在线
    空闲爬升 `_ratchet_idle_floor` 用的是同一个分位数, 语义一致), 交给
    `EnergyVad.calibrate`。之后 `is_speech` 直接进入正常判决, 不再有启动盲区。

    返回标定后的噪声底 (dBFS); 若 VAD 后端不支持则返回 -100.0 表示未标定。
    """
    cal = getattr(vad, "calibrate", None)
    if cal is None:
        return -100.0
    cfg = getattr(vad, "cfg", None)
    if cfg is None:
        return -100.0
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    fl = int(cfg.frame_length)
    frames = [x[i:i + fl] for i in range(0, x.size - fl + 1, fl)]
    if not frames:
        return -100.0
    if abs(float(quiet_pct) - float(cfg.idle_ratchet_pct)) > 1e-6:
        # 尊重调用方给的分位数: 直接在这里算, 不依赖 calibrate 的固定分位数
        levels = [float(vad.frame_dbfs(f)) for f in frames]
        ambient = float(np.percentile(np.asarray(levels, dtype=np.float32), float(quiet_pct)))
        vad.noise_dbfs = min(ambient, float(cfg.noise_ceiling_dbfs))
        vad._idle_levels = levels[-int(cfg.idle_ratchet_window):]
        vad.warmup_done = True
        vad.calibrated = True
        vad._clamp_noise_floor()
        return float(vad.noise_dbfs)
    return float(cal(frames))


# ---------------------------------------------------------------------------
# 重采样 (设备原生采样率 -> 16 kHz)
# ---------------------------------------------------------------------------
def resample_to_16k(audio: np.ndarray, src_sr: int, dst_sr: int = 16000) -> np.ndarray:
    """多相滤波重采样。设备不支持 16 kHz 时必须走这里, 否则 MFCC 全错。"""
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if src_sr == dst_sr:
        return audio
    try:
        from scipy.signal import resample_poly

        from math import gcd

        g = gcd(int(src_sr), int(dst_sr))
        return resample_poly(audio, dst_sr // g, src_sr // g).astype(np.float32)
    except ImportError:  # pragma: no cover
        # 退化为线性插值 (质量略差, 但保证能跑)
        n_out = int(round(audio.size * dst_sr / float(src_sr)))
        xp = np.linspace(0.0, 1.0, audio.size, endpoint=False)
        x = np.linspace(0.0, 1.0, n_out, endpoint=False)
        return np.interp(x, xp, audio).astype(np.float32)


__all__ = [
    "VadConfig", "EnergyVad", "WebRtcVad", "build_vad",
    "VadSegmenter", "SegmentResult", "resample_to_16k", "prime_from_waveform",
]
