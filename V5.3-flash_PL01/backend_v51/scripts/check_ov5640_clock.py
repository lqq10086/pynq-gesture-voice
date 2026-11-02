"""Recompute the final OV5640 ROM state and check the 720p30 design contract.

XVCLK and automatic DVP PCLK remain assumptions until measured. This script
checks source consistency and conservative limits; it does not approve a bitfile.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re


def audit(root, xvclk_hz):
    rtl = root / "hardware" / "rtl"
    rom_path = rtl / "ov5640_720p_rgb565.mem"
    config_path = rtl / "camera_config_v02.v"
    xdc_path = root / "hardware/constraints/ax7020_ddr_vdma_ov5640_hdmi.xdc"
    entries = []
    history = {}
    for line_no, raw in enumerate(rom_path.read_text().splitlines(), 1):
        token = raw.split("//", 1)[0].strip()
        if not token:
            continue
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", token):
            raise ValueError("Malformed ROM entry at line %d" % line_no)
        addr, value = int(token[:4], 16), int(token[4:], 16)
        entries.append((addr, value))
        history.setdefault(addr, []).append({"line": line_no, "value": value})
    regs = dict(entries)
    config = config_path.read_text()
    configured_count = int(re.search(r"ROM_ENTRY_COUNT\s*=\s*(\d+)", config).group(1))
    word = lambda addr: (regs[addr] << 8) | regs[addr + 1]
    # OV5640 v2.33 figure 2-3 and parallel application-note get_sysclk().
    prediv_map = (1, 1, 2, 3, 4, 1.5, 6, 2.5, 8, 1, 1, 1, 1, 1, 1, 1)
    prediv = prediv_map[regs[0x3037] & 15]
    multiplier = ((regs[0x3036] & 127) * 2 if regs[0x3037] & 128
                  else regs[0x3036] & 127)
    sysdiv = (regs[0x3035] >> 4) or 16
    pll_root = 1 + ((regs[0x3037] >> 4) & 1)
    bit_mode = regs[0x3034] & 15
    bit_div = {8: 2, 10: 2.5}.get(bit_mode)
    if bit_div is None:
        raise ValueError("Unsupported BIT divider mode: %d" % bit_mode)
    sclk_root = (1, 2, 4, 8)[regs[0x3108] & 3]
    pll_input_hz = xvclk_hz / prediv
    vco_hz = pll_input_hz * multiplier
    sclk_hz = vco_hz / sysdiv / pll_root / bit_div / sclk_root
    width, height, hts, vts = (word(a) for a in (0x3808, 0x380A, 0x380C, 0x380E))
    fps = sclk_hz / hts / vts
    pclk_hz = 2 * sclk_hz  # RGB565 automatic-DVP prediction, not measured.
    exp_limit = vts - 4
    band50 = math.floor(sclk_hz / hts / 100)
    band60 = math.floor(sclk_hz / hts / 120)
    xdc = xdc_path.read_text()
    xdc_period = float(re.search(r"create_clock\s+-name\s+cam_pclk\s+-period\s+([0-9.]+)", xdc).group(1))
    delay_max = float(re.search(r"set_input_delay[^\n]*-clock_fall\s+-max\s+([0-9.-]+)", xdc).group(1))
    delay_min = float(re.search(r"set_input_delay[^\n]*-clock_fall\s+-min\s+([0-9.-]+)", xdc).group(1))
    checks = {}
    def check(name, passed):
        checks[name] = bool(passed)
    check("rom_count_matches_controller", len(entries) == configured_count)
    check("rom_address_width_fits", len(entries) <= 512)
    check("xvclk_input_range_6_to_27_mhz", 6e6 <= xvclk_hz <= 27e6)
    check("pll1_input_range_4_to_27_mhz", 4e6 <= pll_input_hz <= 27e6)
    check("pll1_vco_conservative_intersection_500_to_800_mhz", 500e6 <= vco_hz <= 800e6)
    check("pll_multiplier_documented_range", 4 <= multiplier <= 252 and (multiplier < 128 or multiplier % 2 == 0))
    check("root_clock_relationship_preserved", regs[0x3108] == 0x01)
    check("native_1280x720_output", (width, height) == (1280, 720))
    check("positive_horizontal_and_vertical_blanking", hts > width and vts > height)
    check("nominal_rate_user_acceptance_29_to_31", 29 <= fps <= 31)
    check("rgb565_8bit_bus_format", regs[0x4300] == 0x61 and regs[0x501F] == 1)
    check("jpeg_disabled", not (regs[0x3821] & 0x20))
    check("automatic_dvp_divider", not (regs[0x460C] & 2))
    check("pclk_predicted_below_84mhz", pclk_hz < 84e6)
    check("dvp_byte_capacity_exceeds_active_image_rate", width * height * 2 * fps < pclk_hz)
    check("pclk_not_gated_during_blanking", not (regs[0x4740] & 12))
    check("dvp_data_updates_falling_edge", bool(regs[0x4740] & 32))
    check("night_mode_disabled", 0x3A00 in regs and not (regs[0x3A00] & 4))
    check("automatic_exposure_and_gain_enabled", 0x3503 in regs and not (regs[0x3503] & 3))
    check("no_initial_extra_exposure_lines", 0x350C in regs and 0x350D in regs and word(0x350C) == 0)
    check("exposure_50hz_bounded_by_vts_minus_four", 0 < word(0x3A14) <= exp_limit)
    check("exposure_60hz_bounded_by_vts_minus_four", 0 < word(0x3A02) <= exp_limit)
    check("band50_matches_line_time", word(0x3A08) == band50)
    check("band60_matches_line_time", word(0x3A0A) == band60)
    check("band50_max_matches_exposure_limit", regs[0x3A0E] == word(0x3A14) // band50)
    check("band60_max_matches_exposure_limit", regs[0x3A0D] == word(0x3A02) // band60)
    wake = entries.index((0x3008, 0x02))
    check("fixed_framerate_policy_set_before_sensor_wake", all(
        any(a == address for a, _ in entries[:wake])
        for address in (0x3A00, 0x3503, 0x350C, 0x350D, 0x3A02, 0x3A03, 0x3A14, 0x3A15)))
    check("xdc_period_no_slower_than_expected_pclk", xdc_period <= 1e9 / pclk_hz)
    check("inherited_input_max_not_weakened", delay_max >= 8.0)
    check("inherited_input_min_not_weakened", delay_min <= 0.0)
    return {
        "status": "PASS_STATIC_ONLY" if all(checks.values()) else "FAIL_STATIC",
        "assumptions": {"xvclk_hz": xvclk_hz, "xvclk_measured": False,
                        "pclk_relation": "automatic RGB565 DVP predicted at 2*SCLK",
                        "sensor_readback": False, "routed_timing_validated": False},
        "checks": checks, "rom_entries": len(entries), "controller_rom_entries": configured_count,
        "clocks": {"pll1_input_hz": pll_input_hz, "pll1_vco_hz": vco_hz,
                   "sclk_hz": sclk_hz, "predicted_pclk_hz": pclk_hz,
                   "xvclk_max_hz_for_84mhz_pclk": xvclk_hz * 84e6 / pclk_hz},
        "video": {"width": width, "height": height, "hts": hts, "vts": vts,
                  "nominal_fps": fps, "frame_period_ms": 1000 / fps,
                  "active_bytes_per_second": width * height * 2 * fps,
                  "horizontal_blank_byte_cycles": 2 * (hts - width), "vertical_blank_lines": vts - height},
        "aec": {"max_50hz_lines": word(0x3A14), "max_60hz_lines": word(0x3A02),
                "band50_lines": band50, "band60_lines": band60,
                "max_exposure_ms": max(word(0x3A14), word(0x3A02)) * hts / sclk_hz * 1000},
        "xdc": {"period_ns": xdc_period, "input_min_ns": delay_min, "input_max_ns": delay_max},
        "final_registers": {"%04X" % addr: "%02X" % value for addr, value in sorted(regs.items())},
        "ordered_overrides": {"%04X" % addr: values for addr, values in history.items() if len(values) > 1},
        "sha256": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in (rom_path, config_path, xdc_path)},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--xvclk-hz", type=float, default=24_000_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.root, args.xvclk_hz)
    output = args.output or args.root / "reports/clock/static_checks.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "checks": len(result["checks"]),
                      "failed": [k for k, v in result["checks"].items() if not v],
                      "nominal_fps": result["video"]["nominal_fps"], "output": str(output)}, indent=2))
    raise SystemExit(0 if result["status"] == "PASS_STATIC_ONLY" else 1)
