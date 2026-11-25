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
| `CMD_SENSE` | 3 | 重新解析 OTP→影子，保留 DSOV；冷启动/PEX_RST 后第一步就该发它。社区报告称它**只重新感测 control rows 0–1、不刷新 FF 池**（我们这轮是直接冷启动，没验过 SENSE 后 FF 层是否更新），所以刚烧完想在免驱态读新烧的 FF 行，按冷启动或 bus reset 后重映射 BAR0 处理，别把 `0x00000000` 当成烧坏了 |

`PEX_RST`（冷启动或平台复位脉冲）会让卡**重新解析 FF**（等价于完整冷启动），是烧写后免断电的快速验证手段。

## 2. 烧写窗口

烧写只允许在**卡从未被 PCIe 枚举**时进行：VFI/glitch 上电 + PEX_RST 脉冲 → 驱动未绑定 → `DSOV=0`。枚举过的卡必须整机断电（不是 warm reboot）回到该状态。窗口判据（`ffmacro.in_write_window`）：

```
FUSECTRL idle(4) ∧ FUSEDEBUG=0 ∧ CRC_STATUS=0x55 ∧ EFP=1 ∧ DSOV=0
```

## 3. 烧写流程（burntool 自动执行，逐条断言）

1. **先读计划自己**：burntool 打印 `plan["warnings"]`（若含熔丝 devid 改写，还会额外一条 `NOTICE`：后面欠一次 A100 vBIOS 刷写，POST 挂死只剩 CH341A），这些必须在 `YES` 之前看到。
2. **离线核验**（不碰卡）：`simulate(plan, dump)` 必须 0 错误 —— 记录重编码、行占用、old==快照回放、目标行空白、FF 范围。官方 `ffcalc verify --dir collected` 现场再验一次 UUID/fingerprint。在此之前两个输入文件本身必须可解析：计划缺少 `burn` 列表，或快照任一行不合法（行号越界、值超出 32 位、JSON 不完整、同一行两个值），一律 `REFUSED` 且**卡未被写入**（rc=1）。快照无法解析即无从得知该行当前内容，不具备烧写条件。
3. `YES`：确认发生在警告与离线核验**之后**——先让人看到风险，再问要不要动手。
4. `status`：确认在写窗口内、控制器未因失败锁死。
5. `CMD_SENSE`：完成后 DSOV 必须仍为 0，否则**不烧**。
6. **现场逐行对快照**：计划涉及的每一行 CMD_READ 回读，必须与 dump 的 `ff_rows` 一致（防错卡/防中途被烧）。
7. `arm`：写 `SBFE_SHADOW=0`、`WDATA_WR_SECURE=0`，断言 `armed_problems()==[]`（idle/debug/CRC/EFP/DSOV/SBFE/WSEC 七项）。
8. 每条记录：`program_row` = 断言已授权 → 目标行读 0 → 锁行校验 → 写 `FUSEWDATA` → `CMD_PROGRAM` → 等 IDLE（≤10 s）→ **回读必须等于记录值**。任何一条不符立即中止。目标行若**已经恰好等于该记录值**（上次中断已烧成）则原样跳过——中断后直接重跑同一计划是安全的。
9. 收尾：再读 `status`（CRC 应仍 `0x55`），冷启动，重跑 `ffscan.sh`：fingerprint 的 defective 部分不变（SBFE 位翻转为 1 属预期），剩余计划应显著缩短/为空。

**首次试烧**建议 `--rows <单行>`（如 fbp 那条），验证 PROGRAM 行为与 CRC 自补后再上全量——本次实测就是这么做的（315 行先烧，`0x420004c1` 一次成功）。

## 4. 控制器错误锁死（poison）与恢复

任何命令失败后 `FUSECTRL` 停在 `0xBADFxxxx`（state=31），控制器**不再应答任何 BAR0 读**。规则：

1. 看到 `0xBADF` 前缀立刻**停止一切读**（继续读会加深状态）；
2. 整机断电冷启动；
3. 重新进窗口后先发 `CMD_SENSE`，确认 `FUSEDEBUG=0`、CRC 正常，再评估是否继续；
4. 锁死未清除前禁止盲目重试 PROGRAM。

burntool 在每一步都检测锁死并以此消息中止（rc=3）。

## 5. 实卡完整验证记录（UUID 对应 fingerprint 前缀 2d827969）

- 17 条记录烧入行 304–320：`fbp 0x852→0x840`、`fbpa/fbio/rop_l2 0xc0330c→0xc03000`、`gpc→0x20`、`tpc4→0x40`、`tpc7→0x20`、`pes→0x38000`、`nvlink/sys_pipe→0`、PCIe Gen4 位清零（block39 的两个 `DISABLE_GEN3_SPEED` 位 + `MAGIC_BITS_D` bit25；整字 MAGIC_D 未动）。
- 每条烧写前七项授权断言 + 现场空白检查全过；每条回读一致；全程未出现锁死。
- 冷启动后：`BOOT0=0x170000a1`、CRC=`0x55`、DSOV=1、SBFE=1；FF 区 222–486 除新 17 行外零改动；defective 16/16 不变（同一块 die）。
- `tpc_gpc3/6_disable` 在 **status 层**显示 `0x1`（fused 层 =0x0）：OEM 固件运行时自屏蔽，参考机上未烧卡的同字段行为相同，**属正常现象**，别再往这两个位上补烧。
- 烧写使 SBFE=1 → fingerprint 变为 `3ab5093e8f9f78b5`（defective 基不变）。用**烧后指纹**认后续快照。
- 重生成计划 = 0 条：潜力 80 GiB / 108 SM / NVLink / Gen4 全部就位；驱动层（geometry/Gen4）另行处理，与 FF 无关。Gen4 能起来靠的是 `MAGIC_BITS_D` bit25 被清（那是 PCIe **能力宣告**，不代表实际速率），整字 MAGIC_D 未动也不影响。
- **指纹不是唯一身份证**：defective+SBFE 的哈希在同批次卡之间会相同（同批卡的出厂卡 SBFE 本来就是 1，其指纹与我们烧穿后重算的指纹一模一样——我们曾被这份烧前旧快照误导过一次）。fingerprint 只用来核对"烧前烧后同一块 die"，**认卡、对计划必须用 UUID**；burntool 逐行对快照是 UUID 之外的最后一道闸。

### 5.1 采集表扩容后重采（2026-11-25，两张卡，驱动在场 + 完整 512 行）

新增的 7 个只读字全部读到，两张卡读数一致：`nvdec_disable`/`status_nvdec` = `0x1f`/`0x1f`、`nvjpg_disable`/`status_nvjpg` = `0x1`/`0x1`、`ofa_disable`/`status_ofa` = `0x1`/`0x1`、`status_display`/`status_disp_head` = `0x1`/`0xf`、`FUSE_EN_SW_OVERRIDE`(`0x820040`) = 0、`DSOV`(`0x820084`) = 1（驱动在场，正常）。三对 `STATUS_OPT == OPT_DISABLE`（SYS_PIPE 已烧 0，status 层也已经是 0）⇒ **5 NVDEC + NVJPG0 + OFA0 零缺陷**，也就是说 `mig` 段显示 8/8 live 的这两张卡，现在缺的只有媒体引擎那三位。默认计划 0 记录，`gen --mig` 各 3 条：`38bbb4b3` 行 313–315 = `0x00060201`/`0x00000241`/`0x000005c1`，`85b20be7` 行 321–323 同值（`blk8` 的 `old` 不同：`0xf906 -> 0x0106`）。CMD_READ pass 在驱动在场、显存被占满的情况下跑完 512 行无 `ffrow_error`，读后 `CRC`/`DSOV` 无异常，业务无感知。指纹仍是 §5 那个烧后值 `3ab5093e8f9f78b5`（`85b20be7`），认卡仍然只认 UUID。

## 6. 计划里带 `--mig`（媒体引擎与 MIG 门槛）时

`ffcalc gen --mig` 会在默认计划之外多清 chain1 的三个 floorsweep 位段：`blk8[15:11]`（5 个 NVDEC）、`blk9[2]` 与 `blk23[3]`（NVJPG0 + OFA0，两位谁对应谁未分离，所以成对清）。烧写动作与其它记录**完全相同**，前面 §1–§4 的规则一条不漏；需要注意的是烧完之后：

1. **烧完四个 `STATUS_OPT_*` 都应读 0**：NVDEC `0x820c24`（OPT `0x820378`）、NVJPG `0x820c0c`（OPT `0x820370`）、OFA `0x820e34`（OPT `0x8202d8`）、SYS_PIPE `0x820e30`（OPT `0x8203a4`，就是 `sys_pipe_disable` 的 status 层）。任一仍非 0 就说明那位单元有物理缺陷（硬件算的是 `STATUS_OPT = OPT_DISABLE | OPT_DEFECTIVE`），这**不是**烧写失败：FF 层回读应当等于新值，缺陷位是任何熔丝都变不出去的，别再往同一位补烧。外部那张卡四条记录烧完，四个寄存器同时归 0。
2. **`0x820c04`/`0x820c08`（DISPLAY / DISP_HEAD）保持原样**。它们对应 chain1 bit `78..82`，是那条链上除 NVDEC/NVJPG/OFA 外剩下的未归属位，而 GA100 没有显示输出——清它们没有任何收益，只有不可逆的消耗。
3. **开关 MIG 模式之后要重启**：`nvidia-smi -mig 1` / `-mig 0` 会触发驱动内 FLR，把易失的 `FBPA_CFG1`/`LOCAL_RANGE` 几何打回出厂 8192 MiB，已建立的 slice 会悄悄缩成 `1g.1gb`。MIG 模式本身持久在 InfoROM，冷启动后由 initramfs 阶段的解锁脚本先恢复满容量几何，MIG 分片才是你要的大小（那份外部报告里写的 `65536 MiB` 是它那张卡的满容量，我们这两张是 80 GiB）。
4. **行代价：同一轮 1 行，事后补烧 3 行**。FF 记录写的是**整个 16-bit 块**：`nvdec_disable` 在 blk8、`bit371` 在 blk23，只要这一轮本来就为 gpc/fbpa 与 nvlink/sys_pipe 重写这两个块，引擎位就是顺带写进去的、不吃行，真正新增的只有承载 `blk9[2]` 那一条。**但算力已经烧完的卡享受不到这点**——我们两张已完全解锁的卡重采后 `gen --mig` 各出 3 条（`blk8`/`blk9`/`blk23` 一人一行，例如 `0x00060201`、`0x00000241`、`0x000005c1`，接在最后已烧行之后）。外部那张卡分轮清这几个位花了 4 行（`blk23` 被写两次）。`gen --mig` 打印的是这张卡这一轮的真实数字（`row cost of --mig on this card: N extra FF row(s) ...`），别按「反正只有一行」估。
5. **采集表扩容前抓的老快照缺字**：`--mig` 只在快照读到过 `nvjpg_disable`/`status_nvjpg`/`ofa_disable`/`status_ofa` 时才会为那一对位出行（这三个引擎没有 `*_DEFECTIVE` 寄存器，`STATUS_OPT == OPT_DISABLE` 是唯一能证明单元存在的路）。缺字时计划警告 `re-capture` 并跳过，**此时 `--set` 也不能绕过**——手填的值同样不能告诉你这块 die 上有什么。我们的两张卡已按新表重采，四对 `STATUS_OPT`/`OPT_*` 全部相等（`0x1f`/`0x1f`、`0x1`/`0x1`、`0x1`/`0x1`，SYS_PIPE 已烧 0 故读 0），也就是**三个引擎都物理存在且零缺陷，烧下去能全部放出来**。

## 7. 计划里有 `pcie_devida` / `pcie_devidb` 时（A100 身份转换）

`ffcalc gen --a100` 会多出身份相关的记录：chain2 blk37（DEVIDA）、blk38（DEVIDB）写 `0x416a` = `0x20B5<<1`，即 `0x416A0942` / `0x416A0982`；外加把整字 MAGIC_BITS_D 写成 `0x00200000`（按卡现值涉及 blk45/46/47 中真正变化的那几块）。**状态：身份那两条出自社区/他人的实测报告，本仓库还没在自己的卡上走过；MAGIC_BITS_D 的整字改写更不是报告步骤，是我们「都要当 A100 就把能力宣告满」的判断，`0x00200000` 没人从真 A100 上读过**——我们能验证的是编码与记录格式自洽（自家 FF 快照里 blk37/38 回放出的就是 `0x4184`，`gen --a100` 生成的就是上面那两条），平台侧结果（POST、BAR1 128G、驱动识别）一律按未验证对待。不认可整字改写就 `--a100 --skip magic_d`，退化成「只烧报告烧过的那两条」。

烧写动作与其它记录**完全相同**——同一套 SENSE→arm→PROGRAM→回读断言，同样多的空白行，本文前面的规则一条不漏。不同的是烧完之后的世界：

1. **身份那两条改变的是「这张卡是谁」**：冷启动后芯片自己就是 `10de:20b5`，lspci、驱动、GSP/DevInit 的启动门槛全部按 A100 认它。其它记录（含 MAGIC_BITS_D 那几条）只改容量/SM/链路/能力宣告，从不碰身份。
2. **烧完不等于结束**：SPI 里还是 CMP ROM 时，VBIOS devid 与熔丝 devid 不匹配，卡起不来。门槛比的是**熔丝 devid**，所以必须 `nvflash` 刷一份 A100 80GB PCIe vBIOS（ROM 不用改一个字节）。`nvflash --save` 先备份这张卡自己的 CMP ROM。
3. **带内无退路**：平台 POST 不过 → 驱动不在 → nvflash 进不去 → 只能 CH341A + SOP8 夹离线刷 SPI。这就是「没备好 CH341A 和烙铁不要烧这几条」的原因。熔丝本身可以再补一条记录把 id 指回别的值（链内后写覆盖先写），但前提仍是卡能冷启动回到烧写窗口。
4. **平台门槛**：A100 VBIOS 声明 BAR1=128GB，BIOS 必须给得出 128 GB 的 64-bit prefetchable BAR1（`Above 4G Decoding` 开、`CSM` 关、`Re-Size BAR` 开、`MMIO High Size ≥ 128G`），否则卡死在 VGA 灯上。判据是 MMIO 窗口，不是主板新旧。
5. **`old` 列是 FF 层对该块的回放值**：实卡出厂就在 FF 链里写了 `0x4184`（= `0x20C2<<1`），所以正常看到 `0x4184 -> 0x416a`。如果这次读出的行没覆盖到承载 id 的那条记录，`old` 会显示 `0x0000`，计划会额外警告「回放值与熔丝寄存器读数不一致」——记录写的是整块新值（链内后写覆盖先写），不受影响，但要用 `ffcalc report` 的 `[Fused device id]`（读熔丝寄存器 `0x8204d8`/`0x82056c`）确认现在到底是哪个 id。
6. burntool 在 `YES` 之前会打印计划警告与一条 `NOTICE`，并跑完离线核验才发问——**别不看就敲 YES**。
7. 转换之后 `scripts/ffscan.sh` 仍能找到这张卡（`0x20c2` 与 `0x20b5` 都认），闭环检查照做。
8. **和 `--mig` 同一轮一起烧**（`gen --a100 --mig`，或 `ffscan.sh --a100 --mig`）：两者动的是不同的链——引擎位在 chain1 `blk8/blk9/blk23`，身份与能力在 chain2 `blk37/blk38/blk46/blk47`——互不重叠。计划里那三条引擎记录与只加 `--mig` 时逐字节相同（测试钉住），`--a100` 也不会让引擎位的行代价变大：那条警告的基线本来就包含身份那几条。计划按链排序，**chain1 的引擎位排在 chain2 的身份位之前**，而 burntool 就按计划顺序执行，所以万一中途断链，留下的是「仍是 CMP 170HX、但媒体引擎已放开」的可启动状态，而不是「熔丝已是 A100、ROM 还没刷」那种只能 CH341A 救的半成品；反过来自觉先烧身份再回头补引擎才是危险顺序。另外：换身份**不会白送媒体引擎**，NVDEC/NVJPG/OFA 照样由同一套 floorsweep 位管着，该清的一位不少。

## 8. 踩坑速查（本流程实际踩过或已替你挡住）

| 症状 | 原因 | 处置 |
|---|---|---|
| 写了记录但行还是 0 | 写进了 `FUSEDATA`（只读口） | 写 `FUSEWDATA(0x82000C)` |
| `PLM_FUSE` 写不进 | 该位由 `WDATA_WR_SECURE` 门控 | 写 WSEC=0，PLM 自动放开 |
| 冷启动后 SBFE 还是 1 | 以为"没授权所以不能写" | DSOV=0 时 `0x821108` 可写，写 0 即授权；冷启动后恢复 1 属正常 |
| 读到 `0xBADFxxxx` | 命令失败，控制器锁死 | 停止读→冷启动→SENSE 评估（§4） |
| `tpc3/6` status≠fused | 固件运行时自屏蔽 | 正常，fused 层对即可 |
| 拿错卡烧错行 | 槽位变了 UUID/指纹没对 | 第 1、4 步的 UUID/fingerprint/逐行对快照三道闸全部必过 |
| 计划里行号变了 | 上一次烧写后旧计划作废 | 每轮烧完重新 `scan`，以新计划为准 |
| 烧完 devid 后卡不 POST / 不初始化 | 熔丝已是 A100 `0x20B5`，SPI 里还是 CMP ROM（ID 不匹配，门槛比的是熔丝） | 刷 A100 80GB PCIe vBIOS；已经挂死则 CH341A 离线刷 SPI |
| 刷了 A100 ROM 仍卡 VGA 灯 | 平台没给出 128G 64-bit prefetchable BAR1 | `Above 4G` 开、`CSM` 关、`Re-Size BAR` 开、`MMIO High Size ≥ 128G` |
| 转 A100 后 `ffscan.sh` 扫不到卡 | 脚本原先只认 device `0x20c2` | 现在 `0x20c2`/`0x20b5` 都认；`capture.py`/`burntool` 同样两种都收 |
| 只烧了 `pcie_devida` 或只烧了 `pcie_devidb` | 两份 id 不一致，门槛读哪份无文档 | 计划已警告；同一轮把两半补齐 |
| 按 `--a100` 烧了 MAGIC_BITS_D，之后想收回 | 整字值 `0x00200000` 是社区猜测，报告里根本没动这个字 | 补一条记录把该块写成别的值（链内后写覆盖先写，前提仍是能进烧写窗口）；不确定就别烧：`--a100 --skip magic_d` |
| `--mig` 计划里没有 NVJPG/OFA 那两位 | 快照是采集表扩容前抓的，`nvjpg_disable`/`ofa_disable` 这两个字没读到，工具不肯为「大概没事」花行 | 重跑 `ffscan.sh`（新探针多读 7 个字，61 → 68）再出计划；`--set` 故意不能绕过这道门 |
| 烧完 MIG 后 slice 缩成 `1g.1gb`、显存显示 8192 MiB | `-mig 1`/`-mig 0` 触发驱动内 FLR，易失的 `FBPA_CFG1`/`LOCAL_MEMORY_RANGE` 被打回出厂几何 | 立刻重启，让 initramfs 阶段的解锁脚本先恢复满容量几何（§6） |
| 免驱态刚烧完读新 FF 行读出 `0x00000000` | `CMD_SENSE` 只重新感测 control rows 0–1，不刷新 FF 池 | 冷启动，或 `revert` 后 `echo bus > reset_method; echo 1 > reset` 再重映射 BAR0；别当作烧坏再补一条 |
| 烧完四个 `STATUS_OPT_*` 里还有位是 1 | 那位单元物理缺陷（硬件算 `STATUS_OPT = OPT_DISABLE \| OPT_DEFECTIVE`） | 不是烧写失败（FF 层回读已等于新值就行），**别补烧**；该引擎按少一个单元计 |
| blk23 被烧了两次、白吃一行 | SYS_PIPE 与 bit371 分了两个窗口清 | 同一轮出计划：`nvlink`/bit371/`sys_pipe` 合成一条 `0x000005c1`（§6） |
