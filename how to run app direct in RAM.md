# KW45 直接在 RAM 中运行应用指南（SB3.1 + PCAN ISP）

> **测试基线**：NXP KW45 (`KW45B41Z83`) · `kw45b41zevk_freertos_hello`（**tested on MCUXpresso SDK 26.06**）

本文档说明如何将 `freertos_hello` 工程打补丁并链接到 Secure SRAM，按顺序执行 `4`、`5`、`6` 原生脚本生成签名 `.bin`（`app_maincore_signed.bin`）与 `.sb3` 容器，并通过 `blhost` + PCAN CAN ISP 直接加载到 RAM 运行。

---

## 1. 为 `freertos_hello` 打补丁并链接到 Secure RAM

在 **MCUXpresso for VS Code** 中导入 **Standalone 模式** 的 `kw45b41zevk_freertos_hello` 示例工程（tested on **MCUXpresso SDK 26.06**），在工程根目录应用 `0001-link-to-ram-run.patch` 即可编译出 RAM 运行镜像（注意：补丁基于 Standalone 工程目录结构，非 Standalone 工程路径无法直接应用）。

**补丁核心原理**：
- 将代码段重定位至 Secure SRAM (`0x30005000`，避开 ROM 保留区与 `0x30004000` `comm_buffer`)，数据与堆栈段置于 `0x30010000 ~ 0x3001C000`；
- 在 RAM 启动分支补全数据段 128-bit ECC 初始化，避免压栈触发 `BusFault`；
- 在 `SystemInit()` 入口清理 ROM ISP 残留的 `SysTick` 与 `NVIC` 中断状态。

> **说明（非 link-to-RAM 核心补丁）**：为了便于通过串口直观验证任务在 RAM 中持续调度运行，示例中的 `freertos_hello.c` 已将主循环任务改写如下（先延时等待 UART 稳定再打印，避免首行 `count 0` 乱码）：
> ```c
> static void hello_task(void *pvParameters)
> {
>     static uint32_t count = 0;
>     for (;;)
>     {
>         vTaskDelay(pdMS_TO_TICKS(1000));
>         PRINTF("Hello world count %d.\r\n", count++);
>     }
> }
> ```

编译完成后将生成的 RAM `.bin` 放入当前目录（即 [freertos_hello.bin](freertos_hello.bin)），然后运行 [prepare_ram_config.py](prepare_ram_config.py) 自动解析镜像的 `SP`、`Reset_Handler` 与加载基址（支持 `0x30005000` / `0x30008000` 等任意 4 KB 对齐地址），一键更新配置与 J-Link 脚本：

```powershell
python .\prepare_ram_config.py .\freertos_hello.bin
```

该脚本会自动刷新以下文件中的路径、基址与向量寄存器：
- [config/signed_config_app_maincore.yaml](config/signed_config_app_maincore.yaml)（更新 `inputImageFile` 与 `outputImageExecutionAddress`）
- [config/sb3_config_maincore.yaml](config/sb3_config_maincore.yaml)（更新 `load` 与 `execute` 地址）
- [script/run_signed_ram.jlink](script/run_signed_ram.jlink)（更新 `loadbin`、`VTOR`、`MSP` 与 `SetPC`）
- [script/run_raw_ram_via_can.cmd](script/run_raw_ram_via_can.cmd)（生成带动态计算地址的原始 BIN PCAN 下载脚本）

---

## 2. 按顺序执行 `4`、`5`、`6` 原生脚本

进入 `dm-kw45-secure-boot-scripts\script\` 目录，按顺序运行以下三个批处理脚本：

| 顺序 | 脚本文件 | 执行动作 | 输出产物 |
| :--- | :--- | :--- | :--- |
| **Step 4** | [script/4.create_signed_app_maincore.cmd](script/4.create_signed_app_maincore.cmd) | 调用 `nxpimage mbi export` 使用 `ROT1_p384.pem` 与 `ROT1_cert_block.bin` 对 RAM `.bin` 签名 | `workspace/bootable_images/app_maincore_signed.bin` |
| **Step 5** | [script/5.create_sb3_app_maincore.cmd](script/5.create_sb3_app_maincore.cmd) | 调用 `nxpimage sb31 export` 使用 `sb3kdk.txt` 将签名 `.bin` 封装为加密签名的 SB3.1 容器 | `workspace/bootable_images/app_maincore.sb3` |
| **Step 6 (PCAN)** | [script/6.upload_sb3_to_device_maincore_via_can.cmd](script/6.upload_sb3_to_device_maincore_via_can.cmd) | 调用 `blhost` 通过 PCAN 将 `app_maincore.sb3` 下发至芯片并直接在 RAM 启动（若走 UART COM 口请改用 [6.upload_sb3_to_device_maincore.cmd](script/6.upload_sb3_to_device_maincore.cmd)） | 芯片从 RAM (`0x30005000`) 运行 |

---

## 3. `blhost` 通过 PCAN CAN ISP 烧录运行演示

### 硬件连接与进入 ISP
1. 将 PCAN-USB 接入开发板 CAN 接口（标准经典 CAN，1 Mbit/s）。
2. 按住 KW45 EVK 上的 **SW4 (`PTA4`)** 并按下复位键，使芯片进入 ROM ISP 模式。

### 通信探测与 SB3 下发（Step 6 等效命令）
```powershell
# 1. 测试 PCAN 与 ROM ISP 通信是否正常
blhost -cb "pcan,PCAN_USBBUS1" get-property 1

# 2. 下发 SB3.1 容器（ROM 自动验签、解密、load 到 0x30005000 并 execute）
blhost -cb "pcan,PCAN_USBBUS1" receive-sb-file ..\workspace\bootable_images\app_maincore.sb3
```

预期返回：
```text
Sending SB file  [####################################]  100%
Response status = 0 (0x0) Success.
```

### 附 1：调试阶段直接下载原始 RAM BIN（仅限 `OEM Open`）
若芯片处于 `OEM Open` 调试阶段，在执行完 `prepare_ram_config.py` 后，直接在 `script/` 目录下**双击运行 [script/run_raw_ram_via_can.cmd](script/run_raw_ram_via_can.cmd)** 即可（脚本内已自动填入计算出的基址、`Reset_Handler` 与 `SP`）。

### 附 2：通过 J-Link 直接下载已签名镜像到 RAM（适用于开启 Secure Boot）
开启 Secure Boot（`OEM Closed`）后，ROM ISP 会禁用 `write-memory` / `execute` 等非签名写入命令（此时 ISP 仅允许 `receive-sb-file` 下发 `.sb3`）。若已通过 Debug Auth 打开 SWD 调试权限，在 `script/` 目录下**直接双击运行 [script/run_signed_ram_jlink.cmd](script/run_signed_ram_jlink.cmd)** 即可（内部自动调用 [script/run_signed_ram.jlink](script/run_signed_ram.jlink) 将 Step 4 生成的 `app_maincore_signed.bin` 下载到 Secure SRAM 运行）。

### 运行结果验证
打开开发板 Debug UART 串口终端（波特率 `115200`，`8N1`），即可看到 FreeRTOS 任务每秒递增打印：
```text
Hello world count 0.
Hello world count 1.
Hello world count 2.
```
