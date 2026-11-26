#!/usr/bin/env python3
# -*- encoding: utf-8 -*-

"""Auto-configure dm-kw45-secure-boot-scripts for a RAM-linked .bin image.

Parses Initial SP and Reset_Handler PC from the input Cortex-M .bin,
automatically infers the RAM load address (4 KB aligned, e.g. 0x30005000 or 0x30008000),
and updates:
  - config/signed_config_app_maincore.yaml
  - config/sb3_config_maincore.yaml
  - script/run_signed_ram.jlink
before running scripts 4 -> 5 -> 6 (or run_signed_ram_jlink.cmd).
"""

import argparse
import os
from pathlib import Path
import re
import struct
import sys


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_BIN = REPO_ROOT / "freertos_hello.bin"
SIGNED_CFG = REPO_ROOT / "config" / "signed_config_app_maincore.yaml"
SB3_CFG = REPO_ROOT / "config" / "sb3_config_maincore.yaml"
JLINK_SCRIPT = REPO_ROOT / "script" / "run_signed_ram.jlink"
RAW_CAN_CMD = REPO_ROOT / "script" / "run_raw_ram_via_can.cmd"


def parse_vectors(bin_path: Path):
    raw = bin_path.read_bytes()
    if len(raw) < 8:
        raise ValueError(f"Binary too small ({len(raw)} bytes), need at least 8 bytes.")
    sp, pc = struct.unpack("<II", raw[:8])
    return len(raw), sp, pc


def read_text_preserve_eol(path: Path) -> tuple[str, str]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    eol = "\r\n" if "\r\n" in text else "\n"
    return text, eol


def write_text_preserve_eol(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def update_signed_yaml(yaml_path: Path, bin_path: Path, load_addr: int) -> None:
    text, _ = read_text_preserve_eol(yaml_path)
    rel_bin = os.path.relpath(bin_path.resolve(), yaml_path.parent.resolve()).replace("\\", "/")

    text_new, n1 = re.subn(
        r"^(inputImageFile:\s*)(\S+)",
        rf"\g<1>{rel_bin}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    text_new, n2 = re.subn(
        r"^(outputImageExecutionAddress:\s*)(0x[0-9a-fA-F]+|\d+)",
        rf"\g<1>0x{load_addr:08X}",
        text_new,
        count=1,
        flags=re.MULTILINE,
    )
    # Ensure .bin extension so J-Link LoadFile/loadbin accepts the signed output directly
    text_new = re.sub(
        r"^(masterBootOutputFile:\s*)(\S+)\.xip\b",
        r"\g<1>\g<2>.bin",
        text_new,
        count=1,
        flags=re.MULTILINE,
    )
    if n1 != 1 or n2 != 1:
        raise RuntimeError(f"Failed to update {yaml_path} (matched inputImageFile={n1}, execAddr={n2})")
    write_text_preserve_eol(yaml_path, text_new)


def update_sb3_yaml(yaml_path: Path, load_addr: int, signed_file_rel: str) -> None:
    text, eol = read_text_preserve_eol(yaml_path)
    addr_hex = f"0x{load_addr:08X}"

    # 1. Remove Flash erase block if present in original NXP template
    text = re.sub(
        r"(?:\r?\n)?[ \t]*# Erase flash[^\r\n]*\r?\n[ \t]*-\s+erase:\s*\r?\n(?:[ \t]+[^\r\n]*\r?\n)*",
        eol,
        text,
    )
    text = re.sub(
        r"(?:\r?\n)?[ \t]*-\s+erase:\s*\r?\n(?:[ \t]+[^\r\n]*\r?\n)*",
        eol,
        text,
    )

    # 2. Update load address and file path
    text, n_load = re.subn(
        r"(-\s+load:\s*\r?\n\s+address:\s*)(0x[0-9a-fA-F]+|\d+)(\r?\n\s+file:\s*)(\S+)",
        rf"\g<1>{addr_hex}\g<3>{signed_file_rel}",
        text,
        count=1,
    )
    if n_load != 1:
        raise RuntimeError(f"Failed to update load command in {yaml_path}")

    # 3. Update existing execute block or append one right after load block
    text, n_exec = re.subn(
        r"(-\s+execute:\s*\r?\n\s+address:\s*)(0x[0-9a-fA-F]+|\d+)",
        rf"\g<1>{addr_hex}",
        text,
        count=1,
    )
    if n_exec == 0:
        text, n_ins = re.subn(
            r"(-\s+load:\s*\r?\n\s+address:\s*\S+\r?\n\s+file:\s*\S+)",
            rf"\g<1>{eol}{eol}  # Execute from Secure SRAM vector table base ({addr_hex}){eol}  - execute:{eol}      address: {addr_hex}",
            text,
            count=1,
        )
        if n_ins != 1:
            raise RuntimeError(f"Failed to insert execute command in {yaml_path}")

    write_text_preserve_eol(yaml_path, text)


def get_signed_output_relpath(yaml_path: Path) -> str:
    text = yaml_path.read_text(encoding="utf-8")
    m = re.search(r"^masterBootOutputFile:\s*(\S+)", text, flags=re.MULTILINE)
    if m:
        return m.group(1)
    return "../workspace/bootable_images/app_maincore_signed.bin"


def update_jlink_script(jlink_path: Path, signed_image_rel: str, load_addr: int, sp: int, pc: int) -> None:
    content = f"""// KW45 Secure SRAM signed image download and execution.
ExitOnError 1
device KW45B41Z83
si SWD
speed 4000
connect
r
h
loadbin {signed_image_rel}, 0x{load_addr:08X}
VerifyBin {signed_image_rel}, 0x{load_addr:08X}
WReg CONTROL 0x00000000
W4 0xE000ED08 0x{load_addr:08X}
WReg MSP 0x{sp:08X}
SetPC 0x{pc & ~1:08X}
WReg XPSR 0x01000000
Regs
g
Exit
"""
    jlink_path.write_text(content, encoding="utf-8")


def update_raw_can_cmd(cmd_path: Path, bin_path: Path, load_addr: int, sp: int, pc: int) -> None:
    rel_bin = os.path.relpath(bin_path.resolve(), cmd_path.parent.resolve()).replace("/", "\\")
    content = f"""@rem Copyright 2021-2025 NXP
@rem All rights reserved.

@rem SPDX-License-Identifier: BSD-3-Clause

:: Download and execute raw RAM binary directly via PCAN CAN ISP (OEM Open only)
blhost -cb "pcan,PCAN_USBBUS1" write-memory 0x{load_addr:08X} "{rel_bin}"
if %errorlevel% neq 0 goto err
blhost -cb "pcan,PCAN_USBBUS1" execute 0x{pc:08X} 0x{load_addr:08X} 0x{sp:08X}
:err
pause
"""
    cmd_path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update dm-kw45-secure-boot-scripts configs and J-Link script from a RAM .bin."
    )
    parser.add_argument(
        "bin_file",
        nargs="?",
        default=str(DEFAULT_BIN),
        help="Path to the RAM-linked .bin file (default: ./freertos_hello.bin).",
    )
    parser.add_argument(
        "-a",
        "--addr",
        default=None,
        help="Optional RAM load address (e.g. 0x30005000 or 0x30008000). Auto-inferred from PC if omitted.",
    )
    args = parser.parse_args()

    bin_path = Path(args.bin_file)
    if not bin_path.is_file():
        print(f"[ERROR] Binary file not found: {bin_path}", file=sys.stderr)
        return 1

    size, sp, pc = parse_vectors(bin_path)
    load_addr = int(args.addr, 0) if args.addr else (pc & 0xFFFFF000)

    print("=== KW45 SB3 RAM Config Updater ===")
    print(f"Input BIN       : {bin_path.resolve()} ({size} bytes)")
    print(f"Initial SP      : 0x{sp:08X}")
    print(f"Reset_Handler   : 0x{pc:08X}")
    print(f"Target RAM Addr : 0x{load_addr:08X}")
    print("-" * 50)

    update_signed_yaml(SIGNED_CFG, bin_path, load_addr)
    print(f"[OK] Updated {SIGNED_CFG.relative_to(REPO_ROOT)}")

    signed_image_posix = get_signed_output_relpath(SIGNED_CFG)
    update_sb3_yaml(SB3_CFG, load_addr, signed_image_posix)
    print(f"[OK] Updated {SB3_CFG.relative_to(REPO_ROOT)}")

    update_jlink_script(JLINK_SCRIPT, signed_image_posix.replace("/", "\\"), load_addr, sp, pc)
    print(f"[OK] Updated {JLINK_SCRIPT.relative_to(REPO_ROOT)}")

    update_raw_can_cmd(RAW_CAN_CMD, bin_path, load_addr, sp, pc)
    print(f"[OK] Updated {RAW_CAN_CMD.relative_to(REPO_ROOT)}")

    print("\nNow run scripts in script/ in order:")
    print("  1. 4.create_signed_app_maincore.cmd")
    print("  2. 5.create_sb3_app_maincore.cmd")
    print("  3. 6.upload_sb3_to_device_maincore_via_can.cmd (or run_signed_ram_jlink.cmd / run_raw_ram_via_can.cmd)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
