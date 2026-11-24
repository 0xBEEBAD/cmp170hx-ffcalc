# FF 熔丝烧写实录与操作规程（实卡闭环验证）

> **⚠ 刷卡有风险：不可逆，最坏后果整卡报废。只有在你能接受最坏后果的前提下才动手操作。每一步谨慎、谨慎、再谨慎；数据必须交叉核对（burn 前 `verify`+`sim`+现场逐行对快照只是本工具自带的三道闸，建议再用独立工具/另一张参考卡比一遍），多找几个工具对比，全部对上了才允许 PROGRAM。**

本文是 `python -m ffcalc.burntool` 的操作规程：寄存器与命令（全部实机确认）、烧写流程与断言清单、错误锁死后的恢复、以及一张 CMP 170HX 从破解到烧完的完整闭环记录。硬件侧（glitch 台架、窗口获取）见姊妹项目 [esp32-ga100vfi](https://github.com/…/esp32-ga100vfi) README。

## 1. 寄存器与命令（实机确认值）

| 寄存器 | BAR0 偏移 | 说明 |
|---|---|---|
| `FUSECTRL` | `0x00820000` | 命令/状态。状态字段 = bits[20:16]；IDLE=4（`0xE0040000`）。命令 = 低 5 位直写 |
| `FUSEADDR` | `0x00820004` | 行号锁存；任何读/写行之前写它，读完恢复 |
| `FUSEDATA` | `0x00820008` | **只读**数据口。往这里写记录会静默丢失（实踩） |
| `FUSEWDATA` | `0x0082000C` | **记录写入口**。PROGRAM 前写这里 |
| `FUSEDEBUG` | `0x00820060` | 必须为 0 |
| `CRC_STATUS` | `0x00820070` | 健康 = `0x55`；烧写后仍 `0x55`（CRC 由控制器自动重算，已实测） |
| `EFP_STATUS` | `0x00820078` | 期望 1 |
| `DSOV_STATUS` | `0x00820084` | 写窗口内 = 0 |
| `SBFE_SHADOW` | `0x00821108` | SBFE 影子位，**DSOV=0 时可写**（写 0 = 授权烧写）。冷启动后恢复 1 |
| `WDATA_WR_SECURE` | `0x0082048C` | 写 0 → `PLM_FUSE(0x8200F4)` 自动放开（自动读 `0xffffffff`）。**别直写 PLM**（写不进，实踩） |

| 命令（写 FUSECTRL 低 5 位） | 值 | 实测行为 |
|---|---|---|
| `CMD_READ` | 1 | 读 `FUSEDATA`；**未烧行读 0x00000000**（kate 的 rows 转写里是 `-`，本仓库按 0 处理，正确） |
| `CMD_PROGRAM` | 2 | **实机确认**。瞬时状态 `0xE00C0000`（WRITE_PROGRAM, state=3），约 1 ms 回 IDLE |
| `CMD_SENSE` | 3 | 重新解析 OTP→影子，保留 DSOV；冷启动/PEX_RST 后第一步就该发它 |

`PEX_RST`（冷启动或平台复位脉冲）会让卡**重新解析 FF**（等价于完整冷启动），是烧写后免断电的快速验证手段。

## 2. 烧写窗口

烧写只允许在**卡从未被 PCIe 枚举**时进行：VFI/glitch 上电 + PEX_RST 脉冲 → 驱动未绑定 → `DSOV=0`。枚举过的卡必须整机断电（不是 warm reboot）回到该状态。窗口判据（`ffmacro.in_write_window`）：

```
FUSECTRL idle(4) ∧ FUSEDEBUG=0 ∧ CRC_STATUS=0x55 ∧ EFP=1 ∧ DSOV=0
```

## 3. 烧写流程（burntool 自动执行，逐条断言）

1. **离线核验**（不碰卡）：`simulate(plan, dump)` 必须 0 错误 —— 记录重编码、行占用、old==快照回放、目标行空白、FF 范围。官方 `ffcalc verify --dir collected` 现场再验一次 UUID/fingerprint。在此之前两个输入文件本身必须可解析：计划缺少 `burn` 列表，或快照任一行不合法（行号越界、值超出 32 位、JSON 不完整、同一行两个值），一律 `REFUSED` 且**卡未被写入**（rc=1）。快照无法解析即无从得知该行当前内容，不具备烧写条件。
2. `status`：确认在写窗口内、控制器未因失败锁死。
3. `CMD_SENSE`：完成后 DSOV 必须仍为 0，否则**不烧**。
4. **现场逐行对快照**：计划涉及的每一行 CMD_READ 回读，必须与 dump 的 `ff_rows` 一致（防错卡/防中途被烧）。
5. `arm`：写 `SBFE_SHADOW=0`、`WDATA_WR_SECURE=0`，断言 `armed_problems()==[]`（idle/debug/CRC/EFP/DSOV/SBFE/WSEC 七项）。
6. 每条记录：`program_row` = 断言已授权 → 目标行读 0 → 锁行校验 → 写 `FUSEWDATA` → `CMD_PROGRAM` → 等 IDLE（≤10 s）→ **回读必须等于记录值**。任何一条不符立即中止。目标行若**已经恰好等于该记录值**（上次中断已烧成）则原样跳过——中断后直接重跑同一计划是安全的。
7. 收尾：再读 `status`（CRC 应仍 `0x55`），冷启动，重跑 `ffscan.sh`：fingerprint 的 defective 部分不变（SBFE 位翻转为 1 属预期），剩余计划应显著缩短/为空。

**首次试烧**建议 `--rows <单行>`（如 fbp 那条），验证 PROGRAM 行为与 CRC 自补后再上全量——本次实测就是这么做的（315 行先烧，`0x420004c1` 一次成功）。

## 4. 控制器错误锁死（poison）与恢复

任何命令失败后 `FUSECTRL` 停在 `0xBADFxxxx`（state=31），控制器**不再应答任何 BAR0 读**。规则：

1. 看到 `0xBADF` 前缀立刻**停止一切读**（继续读会加深状态）；
2. 整机断电冷启动；
3. 重新进窗口后先发 `CMD_SENSE`，确认 `FUSEDEBUG=0`、CRC 正常，再评估是否继续；
4. 锁死未清除前禁止盲目重试 PROGRAM。

burntool 在每一步都检测锁死并以此消息中止（rc=3）。

## 5. 实卡完整验证记录（UUID 对应 fingerprint 前缀 2d827969）

- 17 条记录烧入行 304–320：`fbp 0x852→0x840`、`fbpa/fbio/rop_l2 0xc0330c→0xc03000`、`gpc→0x20`、`tpc4→0x40`、`tpc7→0x20`、`pes→0x38000`、`nvlink/sys_pipe→0`、PCIe Gen4 位清零（含 `MAGIC_D` 未烧，Gen4 仍通）。
- 每条烧写前七项授权断言 + 现场空白检查全过；每条回读一致；全程未出现锁死。
- 冷启动后：`BOOT0=0x170000a1`、CRC=`0x55`、DSOV=1、SBFE=1；FF 区 222–486 除新 17 行外零改动；defective 16/16 不变（同一块 die）。
- `tpc_gpc3/6_disable` 在 **status 层**显示 `0x1`（fused 层 =0x0）：OEM 固件运行时自屏蔽，参考机上未烧卡的同字段行为相同，**属正常现象**，别再往这两个位上补烧。
- 烧写使 SBFE=1 → fingerprint 变为 `3ab5093e8f9f78b5`（defective 基不变）。用**烧后指纹**认后续快照。
- 重生成计划 = 0 条：潜力 80 GiB / 108 SM / NVLink / Gen4 全部就位；驱动层（geometry/Gen4）另行处理，与 FF 无关。
- **指纹不是唯一身份证**：defective+SBFE 的哈希在同批次卡之间会相同（同批卡的出厂卡 SBFE 本来就是 1，其指纹与我们烧穿后重算的指纹一模一样——我们曾被这份烧前旧快照误导过一次）。fingerprint 只用来核对"烧前烧后同一块 die"，**认卡、对计划必须用 UUID**；burntool 逐行对快照是 UUID 之外的最后一道闸。

## 6. 踩坑速查（本流程实际踩过或已替你挡住）

| 症状 | 原因 | 处置 |
|---|---|---|
| 写了记录但行还是 0 | 写进了 `FUSEDATA`（只读口） | 写 `FUSEWDATA(0x82000C)` |
| `PLM_FUSE` 写不进 | 该位由 `WDATA_WR_SECURE` 门控 | 写 WSEC=0，PLM 自动放开 |
| 冷启动后 SBFE 还是 1 | 以为"没授权所以不能写" | DSOV=0 时 `0x821108` 可写，写 0 即授权；冷启动后恢复 1 属正常 |
| 读到 `0xBADFxxxx` | 命令失败，控制器锁死 | 停止读→冷启动→SENSE 评估（§4） |
| `tpc3/6` status≠fused | 固件运行时自屏蔽 | 正常，fused 层对即可 |
| 拿错卡烧错行 | 槽位变了 UUID/指纹没对 | 第 1、4 步的 UUID/fingerprint/逐行对快照三道闸全部必过 |
| 计划里行号变了 | 上一次烧写后旧计划作废 | 每轮烧完重新 `scan`，以新计划为准 |
