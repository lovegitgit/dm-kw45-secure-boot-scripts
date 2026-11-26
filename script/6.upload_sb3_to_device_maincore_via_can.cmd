@rem Copyright 2021-2025 NXP
@rem All rights reserved.

@rem SPDX-License-Identifier: BSD-3-Clause

:: Script uploads SB3.1 file with main core RAM image to device via PCAN CAN ISP.
:: For UART ISP, use: blhost -p COM3,460800 receive-sb-file ../workspace/bootable_images/app_maincore.sb3
blhost -cb "pcan,PCAN_USBBUS1" receive-sb-file ../workspace/bootable_images/app_maincore.sb3
pause