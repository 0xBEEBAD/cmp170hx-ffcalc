# ffcalc — GA100 CMP 170HX 熔丝解锁预计算工具

针对 [kate-land 的 GA100 VFI 研究](https://blog.kate.land/posts/using-voltage-glitching-to-unlock-the-a100/)的离线辅助工具，纯 Python 标准库 + 一个只读内核探测模块 + 刷写计划 + 可选刷写，与 [cmpunlocker] 独立。

> ## ⚠ 刷卡有风险
>
> FF 熔丝烧写**物理不可逆**，错误目标值最坏的后果是**整卡报废**——动手前请确认这个最坏后果是你能接受的。每个操作谨慎、谨慎、再谨慎；**数据一定要交叉核对，多找几个工具互相印证**（本工具的 `verify`/`sim`、外部读行工具、对照另一张已知好卡），任何一处对不上就停下来查，全部对上了再烧。
>
> 最好用ai直接来读取分析执行本项目，省心省力，但是要对ai的行为进行约束！
>
> 本仓库负责只读采集、分析、计划与核验；烧写执行用 `python -m ffcalc.burntool`（流程、断言与错误锁死后的恢复见 [docs/BURNING.md](docs/BURNING.md)，已实卡闭环）。风险自负。

## 依赖

- Python ≥ 3.6（仅标准库，分析端可完全离线运行）
- 采集端（Linux 主机）：`gcc` / `make` / 与运行内核匹配的 `linux-headers`、`sudo`、已加载 nvidia 驱动的机器（`nvidia-smi` 用于读取 GPU UUID；没有时退化为 fingerprint 认卡）

## 子命令

| 命令                                                         | 作用                                                         |
| ------------------------------------------------------------ | ------------------------------------------------------------ |
| `scan --dir collected [--outdir cards --plans plans]`        | 全表潜力分析 + 按 UUID 保存 dump 与计划；FF 行未知时跳过计划（退出码 1） |
| `mergeff dump.json ffrows.txt`                               | 把外部读出的 FF 行合并进卡 dump，并报告覆盖是否完整          |
| `verify <plan> --dir collected [--ffrows ffrows.txt] \| --dump dump.json` | UUID/fingerprint/回放 核验                                   |
| `report dump.json`                                           | 单卡潜力分析                                                 |
| `gen dump.json -o plan.json [--ffrows ffrows.txt]`           | 生成 FF 烧写记录（自动 16-bit 块合并）；FF 记录未读时拒绝出计划（退出码 2，不写文件） |
| `sim dump.json plan.json [--ffrows ffrows.txt]`              | 逐字段回放核对                                               |
| `burntool status --bdf …`                                    | 烧写窗口判据（只读）                                         |
| `burntool burn plan.json --dump dump.json --bdf … [--rows 315]` | 断言式烧写执行（见 docs/BURNING.md）                         |

常用参数：`--set fbp_disable=0x840`（覆盖目标值）、`--only` / `--skip`（字段过滤，首次试烧建议 `--only fbp_disable`）。

**默认参数（不加 `--a100`）就已经放出这张卡的全部能力并拿到 Gen4。** `--a100`（`gen` / `scan` / `scripts/ffscan.sh`，或环境变量 `A100=1`）是另一件事：它把**熔丝里的 device id** 也改成 A100，并连带整字改写 MAGIC_BITS_D 能力宣告字，代价是一次侵入式的 vBIOS 刷写，属于可选且高风险的「身份转换」——见下面单独一节。



## 使用流程：

### 第 1 步 · 采集 + 出计划（一条命令）

在插着卡的 Linux 主机、仓库根目录执行（内核拒绝 BAR0 sysfs mmap 的机器需要 sudo，见「采集原理」）：

```sh
git clone <repo> && cd ffcalc
sudo bash scripts/ffscan.sh            # 常规计划：潜力解锁（FB / SM / NVLink / Gen4）的全部记录
sudo bash scripts/ffscan.sh --a100     # 同上 + 熔丝 devid 改写成 A100、MAGIC_BITS_D 整字满血宣告（高风险，见「A100 身份转换」；`--no-magic-d` 只改 devid）
```

不加 `--a100` 就是推荐路径：计划里只有 `fbp/fbio/fbpa/gpc/tpc/pes/rop_l2/nvlink/sys_pipe` 与 PCIe Gen3/4 那些潜力记录，烧完就是 80 GiB + 全 SM + NVLink + Gen4，卡仍然是 `CMP 170HX`，不碰 vBIOS。`--a100` 会额外追加改写熔丝 device id 的记录（以及整字改写 MAGIC_BITS_D 的那几条），并把一次 nvflash 刷写变成必需 —— 这一步的风险和后果写在「A100 身份转换（`--a100`）」一节里，计划与 `gen` 输出也会逐条打印警告。

它构建探测模块 `ffprobe.ko`，对每张 CMP 170HX（device `0x20c2`；已经被 `--a100` 转成 A100 身份的同一张卡是 `0x20b5`，脚本两种都认）只读 61 个熔丝寄存器，再自动跑 CMD_READ 读出全部 512 物理熔丝行（只写 FUSEADDR/FUSECTRL 命令寄存器；控制器非 IDLE 会中止并如实报 partial），然后用 `nvidia-smi` 关联 UUID、做潜力分析、按 UUID 保存文件。结束时打印（实机输出示例）：

```
card UUID                                BDF           FB now->max    SM  FP         plan
GPU-38bbb4b3-bbfc-09b4-6ec2-0cce33417b95 0000:05:00.0  64->80 GiB    124 fc497d01…  plans/38bbb4b3-….burn.json (16 records)
GPU-b6b9f653-ec1d-f20f-829c-243314997fa8 0000:06:00.0  64->64 GiB    124 98fa952a…  plans/b6b9f653-….burn.json (11 records)
```

这一行表怎么读：

| 列 | 含义 |
|---|---|
| `FB now->max` | 当前显存 → 硬件潜力。**`64->64` 说明这张卡白烧**（物理 FBP 缺陷封顶，如 defective 0x2d 那张），别烧 |
| `SM` | 按 defective 底线能放出的 SM 数（A100 量产只开 108，CMP 常藏着 120+） |
| `FP` | fingerprint：`*_DEFECTIVE` 全字 + SBFE 位的 sha256 前 16 位。物理缺陷不随烧写/重启变化，**烧前烧后必须一致**，是 nvidia-smi 不在场时（glitch 台架）的认卡手段；同批卡 defective 相同、出厂 SBFE 即为 1，指纹可与烧后完全相同——认卡、对计划以 UUID 为准 |
| `plan` | 该卡的烧写计划文件；显示 `SKIPPED (FF rows not read)` 表示 FF 行没读全，工具拒绝出计划（见「兜底路」） |

### 第 2 步 · 看计划、定范围

`plans/<uuid>.burn.json` 是烧写方唯一需要读的文件，每条记录就是要追加的一行物理熔丝：

```json
"burn": [ { "row": 297, "record": "0x80070141", "chain": 1, "block": 5,
            "old": "0x9867", "new": "0x8007", "fields": ["fbio_disable"] }, ... ]
```

| 字段 | 含义 |
|---|---|
| `row` | 目标物理熔丝行号（222–486 内）。计划自动排在**该卡最后一条已烧行之后**，绝不与现有记录重叠 |
| `record` | 烧进该行的 32-bit 值，编码见「FF 记录格式」。例：`0x80070141` = chain1 / block5 / 新值 `0x8007` |
| `chain` / `block` | record 的可读拆解：FF 链号与 16-bit 块号 |
| `old` → `new` | 该块当前值（由卡上真实 FF 快照回放得出）→ 烧写后有效值。相邻字段的块会合并成一条记录，`old` 不是想当然的 0 |
| `fields` | 这条记录**真正改动了哪些 option word 的位**（为什么烧它）。多个字段共享同一块时（如整字 `magic_d` 与它的 bit25，或跨块的 device id），只有在这块里真的有位要变的字段才列名——所以它是「为什么烧这条」的凭据，不是「这块与哪些字段沾边」的清单 |

文件其余顶层字段：`uuid`/`fingerprint`（这张计划的身份，verify 用它对卡）、`targets`（各 option word 目标值）、`analysis`（烧完可达到的 FB/SM 潜力）、`warnings`（如「目标值没包住 defective 底线」；计划里若含熔丝 devid 改写，这里会写清它在做什么、要付出什么）。

想改范围就用 `gen` 重新生成：`python -m ffcalc gen cards/<uuid>.json -o plans/try1.json --only fbp_disable`（首次试烧强烈建议只烧 fbp 一条，用第 4 步的 `--rows` 单行模式；冷启动后读 `CRC_STATUS==0x55` 已实测成立——控制器自动重算 CRC）。`--set fbp_disable=0x840` 可手工钉目标值。

`cards/<uuid>.json` 是该卡完整快照（计划由它推出，重跑会自动继承）：

| 字段 | 含义 |
|---|---|
| `uuid` / `fingerprint` / `bdf` | 卡的三重身份（PCIe 槽位号 `bdf` 会变，UUID 不变） |
| `sku` | `8gb`/`10gb`，决定每个 FBP 折算容量 |
| `ff_rows` | **全部 512 物理行快照**（0–221 regular、222–486 FF、487–511 IFF）；未烧的行值为 0。burn 计划的存在性/正确性完全依赖这个字段覆盖完整。**值的写法两种都合法**：`"0x0fe805c1"` 字符串（`capture` 通道）与 `267006913` int（`ingest` 通道），历史文件里都有，所以读取方必须自己归一化（统一走 `ffcalc/dump.py`，不要绕过它直接 `json.load`）。行号键十进制或 `0x` 均可；无 `0x` 前缀的数按十进制解释。新写入统一为 `0x%08x` |
| `option_words` | 当前有效值（`status` 优先于 `fused`，含 `*_defective` 底线）；`xxx__fused` 表示该字 status 与 fused 不一致（软件覆盖过） |
| `info` | 辅助状态字（nvdec、SBFE、half_fbpa 等） |
| `read_only` | 本次采集是否零写入（跑了 CMD_READ 则为 false） |

`collected/` 是原始采集留档（排查用）：`bar0map.tsv`（BDF、BAR0 物理基址、UUID）、`probe_<bdf>.txt`（dmesg 摘录，每行 `FFPROBE <名字> <地址> <值>`；`ffrow.<n>` 行即第 n 号物理行，`ffrow_error busy_N/timeout_N` 表示读行中止、覆盖不完整）、`kprobe/`（模块源码与编译产物）。三者连同 `cards/`、`plans/` 都是运行产物，不要提交；换槽位/重插后重跑即可。

### 第 3 步 · 烧写前核验（烧写工具动手前必须调，非 0 不许烧）

```sh
python -m ffcalc verify plans/<uuid>.burn.json --dir collected
```

它现场重新采集一遍，然后拒掉四种情况：① 机器里没有这张 UUID 的卡（拿错卡/插错槽）；② UUID 对但 fingerprint 不对（不是同一块 die）；③ 现场 FF 行读不全（无法核对行占用与 old 值）；④ 回放不通过（计划要写的行已被占、或 old 对不上、或烧完到不了目标值）。输出 `OK: plan … belongs to card …` + rc=0 才放行。

### 第 4 步 · 烧写执行（卡必须在烧写窗口内，见 docs/BURNING.md）

```sh
python3 -m ffcalc.burntool status --bdf 0000:05:00.0          # 检查写窗口条件
python3 -m ffcalc.burntool burn  plans/<uuid>.burn.json \
    --dump cards/<uuid>.json --bdf 0000:05:00.0 --rows 315    # 先试烧一行
python3 -m ffcalc.burntool burn  plans/<uuid>.burn.json \
    --dump cards/<uuid>.json --bdf 0000:05:00.0                # 再上全量
```

它按顺序强制：回显计划自带的 `warnings`（含熔丝 devid 改写的计划会在这里再次提醒：后面欠一次 vBIOS 刷写）→ simulate 离线核验（0 错误）→ `YES` 确认 → 窗口判据 → `CMD_SENSE` 后 DSOV 仍为 0 → 计划涉及的每一行现场回读必须与快照一致 → `arm`（SBFE 影子位 + WDATA_WR_SECURE，七项断言）→ 逐条 PROGRAM（目标行必须空白、锁行校验、回读必须等于记录值；上次中断已烧成的行自动跳过，重跑安全）。控制器命令失败后锁死（`FUSECTRL` 读 `0xBADFxxxx`）或任何控制器异常立即中止（rc=3）：停止一切读，整机断电后重新走第 1 步。首次使用务必先 `--rows` 试烧单条。

### 第 5 步 · 冷启动 → 闭环

断电重启（FF 只在开机沿重新解析）→ 重跑 `sudo bash scripts/ffscan.sh`：defective 指纹必须不变（SBFE 位会因烧写翻成 1，指纹随之前移属预期）；潜力表 `FB now->max`/plan 应反映烧后状态（已烧字段消失，剩余记录行号后移）。要加烧就以这轮新计划为准重复第 2–5 步。**每次烧写后 FF 已变，旧计划作废。**

## A100 身份转换（`--a100`，可选 · 高风险）

> **只想解锁全部能力（80 GiB / 全 SM / NVLink）与 Gen4 的话，默认参数就够了，这一节整段可以跳过。** `--a100` 不是「解锁」的一部分，它是换身份。

`--a100` 在默认计划之外追加身份相关的记录，把**熔丝里的 device id** 从 CMP `0x20C2` 改成 A100 `0x20B5`（熔丝编码是 `device_id << 1`：`0x20C2<<1=0x4184` → `0x20B5<<1=0x416A`）：

| chain | block | 字段 | 记录 |
|---|---|---|---|
| 2 | 37（DEVIDA） | `pcie_devida` | `0x416A0942` |
| 2 | 38（DEVIDB） | `pcie_devidb` | `0x416A0982` |
| 2 | 45/46/47（MAGIC_BITS_D） | `magic_d` | 整字写成 `0x00200000`（社区猜测值，见下；哪些块要写取决于卡现在的值） |

前两条就是社区报告里转换时烧进去的那两条（**他人实测，我们自己的卡还没走过这条路**），`ffcalc gen --a100` 生成的正是它们（卡已是 A100 身份则自动跳过）。`old` 列是 FF 层对该块的回放值：实卡出厂就在 FF 链里写了 `0x4184`（= `0x20C2<<1`），所以正常显示 `0x4184 -> 0x416a`；若这次读出的行没覆盖到承载 id 的那条记录，`old` 会显示 `0x0000`，此时计划会额外警告「回放值与熔丝寄存器读数不一致」——记录写的是**整块新值**（链内后写覆盖先写），所以不受影响，但值得用 `ffcalc report` 的 `[Fused device id]`（直接读熔丝寄存器 `0x8204d8`/`0x82056c`，即 lspci 看到的 id）核对一遍。

MAGIC_BITS_D 那几条**不是报告的步骤，是我们的判断**：既然已经付了换身份 + 刷 ROM 的代价，就不该在 A100 的 id 旁边继续挂着 CMP 时代的能力宣告位（Gen4 的宣告本来就靠清 bit25，默认计划已经清了；整字改写是顺带把其余 CMP 位一起归位）。`0x00200000` 是社区猜测、没人从真 A100 上读过，所以计划会为它单独警告一次，并给好退路：`--a100 --skip magic_d` = 退回「只烧报告烧过的那两条」；自己从真 A100 读到过值就 `--set magic_d=HEX` 钉死。

**为什么非烧不可**：GSP/DevInit 的启动门槛比的是**熔丝 devid ↔ VBIOS devid** 一致性。往熔丝仍是 `0x20C2` 的卡上直刷 A100 ROM 必被拒；反过来先把熔丝改成 `0x20B5`，A100 ROM 就是「原厂签名 + ID 匹配」，MAC 校验自然通过——**ROM 一个字节都不用改**。

**它在做什么、会带来什么后果**（这就是它比打金窗 + 烧 FF 更危险的原因：多了一次侵入式刷写）：

1. 冷启动后芯片自己就是 `10de:20b5`：lspci、驱动、启动门槛一律按 A100 认它，预期 `nvidia-smi` 报 `A100 80GB PCIe / 81920 MiB`、BAR1 128 GiB（这一步能否落地取决于平台，见下）；
2. 卡**必须**刷成对应的 A100 80GB PCIe vBIOS 才起得来：熔丝已是 A100、SPI 里还是 CMP ROM 时 ID 不匹配，卡起不来。所以 `--a100` 实际是把一次 **vBIOS 刷写**变成了必需步骤（nvflash 5.692 x64；先 `nvflash --save` 备份自己这份 CMP ROM，只认自己这份 dump）；
3. 一旦平台 POST 不过，**带内没有退路**（驱动不在、nvflash 进不去），唯一救法是 CH341A + SOP8 夹离线刷 SPI。**没备好 CH341A 和烙铁就别烧这几条**；
4. 熔丝层面还能再补一条记录把 id 指向别的值（链内后写覆盖先写），但前提是卡还能冷启动进烧写窗口；
5. 平台 BIOS 必须能分配 128 GB 的 64-bit prefetchable BAR1（A100 VBIOS 声明 BAR1=128GB）：`Above 4G Decoding = Enabled`（必开，不开直接卡 VGA 灯）、`CSM = Disabled`、`Re-Size BAR = Enabled`、`MMIO High Size ≥ 128G`（有则设满）。判据是 **MMIO 窗口 ≥ 128G**，「新板」≠「能过」；
6. 只烧一半（`pcie_devida` 或 `pcie_devidb`）会留下两份 id 不一致，而门槛读哪一份没有文档——计划会警告，除非明确知道自己在干什么，否则两半同一轮烧完。

顺序（缺一步就可能把卡留在半路）：

1. 平台 BIOS 先开好 `Above 4G` / `CSM=Disabled` / `Re-Size BAR` / `MMIO High Size ≥ 128G`；
2. `nvflash --save` 备份这张卡自己的 CMP ROM；
3. `sudo bash scripts/ffscan.sh --a100` 出计划（默认那十几条 + 身份/能力那几条一次出全）→ `verify` → 同一轮窗口里烧完；
4. 冷启动 → `nvflash` 刷 A100 80GB PCIe vBIOS → 冷启动；
5. 再跑一次 `sudo bash scripts/ffscan.sh` 确认（脚本已同时识别 `0x20c2` 与 `0x20b5`；`report` 的 `[Fused device id]` 应显示 `0x20b5`，潜力表照常读）。

**来源与状态**：这条路线（改熔丝 devid → 刷 A100 ROM → 识别为 `A100 80GB PCIe / 81920 MiB`）出自社区/他人的实测报告，**我们尚未在自己的卡上复现**，所以这里不复述对方的平台与驱动细节，也不替结果背书：请把 `--a100` 当作未验证路径，按最坏后果（CH341A 离线救）评估。

**MAGIC_BITS_D（BAR0 `0x820520`）不是那道启动门槛**：它是 PCIe **能力宣告**字，不代表实际速率——清掉 bit25（默认计划本来就清）就足以宣告并跑起 Gen4，这是我们自己卡上的观察（整字未动，Gen4 训练通过）。社区报告的转换同样只动 devid。`--a100` 仍会整字改写它，理由是「都要当 A100 了就把能力也宣告满」，而 `0x00200000` 至今没人从真 A100 上读过：认为这个取舍不值，就 `--a100 --skip magic_d`（或自己读到过真值后 `--set magic_d=HEX` 钉死）。

## 兜底路（读不到 FF 行时）

工具规定：`ff_rows` 缺失或只覆盖部分 FF 行 = 「没读过或读失败」，一律拒绝出计划（宁可没计划，不烧错行）。此时：

- 卡不空载：`FFROWS=0 sudo bash scripts/ffscan.sh` 先只采 words，卡空下来后单跑 `python3 -m ffcalc.capture --read-ff-rows -o cards/<uuid>.json` 之外的采集，或
- 外部读出 512 行（如 Kate 的方法）后存成行文件：每行 `<行号> <值>`（十进制或 `0x`，**未烧行必须写 0**，`#` 注释、dmesg 前缀可容忍），然后
  `python -m ffcalc mergeff cards/<uuid>.json ffrows.txt` 合并进快照，重跑 `scan`（或直接 `gen --ffrows ffrows.txt`）。`mergeff` 会报告覆盖是否完整，不完整继续拒绝。

`mergeff` 对快照本身同样严格：合并前先校验快照中**已有**的每一行（行号 0–511、值为 32 位、同一行不得有两个值），任一不合格即 `FAIL:` 退出且**不写入任何字节**——否则无效行会被写回快照，直到 `gen` 才报错，错误位置与原因分离。保存是原子的：先写同目录临时文件、回读校验、再重命名覆盖，中途失败或磁盘满都不会让这张卡唯一的快照变成不完整的 JSON。若外部行文件与快照同行不同值，以现场读取值为准，并打印被覆盖的行（`note: … 222 0x….->0x….`）——若此前未做烧写，则说明该行的来源文件不属于这张卡。

## 退出码 / 常见消息速查

| 消息 / 命令 | rc | 含义与下一步 |
|---|---|---|
| `FAIL: <快照路径>: …`（任何子命令，含 report/gen/mergeff/scan） | 2 | 快照不可信（行号越界、值超出 32 位、JSON 不完整、同一行两个值）。**文件未被修改**：按消息指出的键修正，或重新采集这张卡 |
| `REFUSED: ff_rows is empty / covers only …`（gen） | 2 | FF 快照不全，**没有写出计划文件**；走「兜底路」 |
| `SKIPPED (FF rows not read)`（scan 表格 plan 列） | 1 | 同上，dump 已保存，补 FF 行后重跑 |
| `STILL partial: N of 265 FF rows unknown`（mergeff） | 1 | 行文件没列全（多半是没把未烧行写 0），补全再 merge |
| verify `FAIL: … not present / fingerprint differs / ff rows readout …` | 1 | 换卡重采 / 排查是否烧过 defective / 先补 FF 行 |
| verify `OK: plan … belongs to card …` | 0 | 可以烧 |
| burntool `REFUSED: …` | 1 | 计划/快照/窗口任一条件不满足，**卡未被写入**；按消息处置后重跑 |
| burntool `ABORT: …`（poison / 超时 / 未授权） | 3 | 停止一切 BAR0 读 → 整机断电 → 重新进窗口先 `CMD_SENSE`（docs/BURNING.md §4）；断点续烧安全：已是目标值的行会被跳过 |
| burntool `plan warning: …` / `NOTICE: … fused device id …` | 0 | 提示不是错误：这条计划要改写熔丝 devid，刷完欠一次 A100 vBIOS，POST 挂死只能 CH341A（见「A100 身份转换」） |
| `insmod: … Resource temporarily unavailable` | — | 正常：探测模块打印完 `return -EAGAIN` 自卸载 |



## FF 记录格式（本工具严格遵循）

`bits[4:0]=chain, bit5=0(FF), bits[15:6]=16bit 块号, bits[31:16]=新块值`
例：`0x0fe805c1` = chain1 / block23 / data 0x0fe8。链内后写的记录覆盖先写的，因此新记录追加在最后一条已烧行之后。

## 采集原理

目标内核（6.18.x-trim + nvidia 驱动占线）拒绝 GPU BAR0 的 sysfs mmap（BAR1/3 正常），所以走 `samples/gen_probe_c.py` 从 `ffcalc/data/ga100-fuses.json` 生成的内核模块：`ioremap(bar0)` → `readl` 61 个寄存器 → 按 CMD_READ 逐行读出 512 物理熔丝行（仅写 FUSEADDR/FUSECTRL 命令寄存器；控制器非 IDLE 则中止，报 `ffrow_error`）→ `pr_info` → `return -EAGAIN` 自卸载。words 部分对设备零写入。原始日志格式：

```
FFPROBE <字段名> <寄存器物理地址> <32bit值>      # 熔丝行行名形如 ffrow.<行号>
```

`ffcalc/ingest.py` 负责按 BAR0 前缀分组、还原 option words、算 fingerprint。

## 已确认 / 待验证

- 三张实卡数据：两张 80 GiB 潜力（10 FBP），一张 64 GiB 封顶（很郁闷。。）（defective 0x2d）；defective 底线全部读到，option word 映射与博客数据一致
- **实卡烧写闭环**（一张 CMP 170HX，17 条记录，行 304–320）：`CMD_PROGRAM=2` 确认（瞬时态 `0xE00C0000`）、空白行读 0、CRC 由控制器自动重算（烧后仍 0x55）、冷启动 BOOT0=0x170000a1、潜力全部落地；`tpc_gpc3/6` status 层 0x1 为固件运行时自屏蔽（fused 层 0x0，参考卡同样如此）。全过程与踩坑见 docs/BURNING.md
- `DISABLE_GEN3_SPEED` 是 block39 的 bit1 还是 bit9 未定（上游未解决），默认两个都清——实卡两条都清 + MAGIC_D 未动（除 bit25），Gen4 训练通过（bit1 非必要嫌疑上升）
- **A100 身份转换：路线可信、结果待我们自己复现**。启动门槛比的是**熔丝 devid**（不是 VBIOS devid，也不是 MAGIC_BITS_D），改法是烧 chain2 blk37/38 → `0x416A`（`0x20B5<<1`）再刷 A100 ROM——这条出自社区/他人的实测报告，本仓库只验证了「FF 编码与记录格式自洽、`gen --a100` 生成的就是那两条记录」，**尚未在自己的卡上走过这条路**（平台 128G MMIO 窗口、nvflash 刷写、POST 风险一律按未验证对待）
- `MAGIC_BITS_D` 是 PCIe **能力宣告**字（不代表实际速率）：清 bit25 即宣告支持 Gen4，这是我们自己卡上的观察（未整字改 MAGIC_D，Gen4 训练通过）。`--a100` 顺带整字改它是「换身份就把能力宣告满」的判断，**不在社区报告步骤里**；`0x00200000` 至今没有实测支撑，属未验证加法（`--skip magic_d` 可退出，`--set magic_d=HEX` 可钉自己的读数）
- MIG / ECC / NVDEC 解锁（`sys_pipe_disable`）作者标注 untested；本卡已烧 0，功能验证待做
- 10GB SKU 每 FBP 折算容量按 4 GiB 估
- raw FF 行读出已内置：`ffprobe.ko` 默认跑 CMD_READ pass（`ffcalc/ffmacro.py` 单源生成 C，协议与 ga100vfi 一致：先查 FUSECTRL IDLE/FUSEDEBUG，锁行校验回读，发 CMD_READ，轮询回 IDLE，读 FUSEDATA，结束恢复 FUSEADDR）；`capture.py --read-ff-rows` 同流程。控制器忙 → 中止并保留已读行，工具按“读取不完整”拒绝出计划；外部读出的行仍可走 `mergeff`/`--ffrows` 导入

## 仓库结构

```
ffcalc/optionmap.py    option word ↔ FF chain/bit 映射（照抄博客数据表）
ffcalc/ffrecord.py    FF 32-bit 记录编解码 + 链回放
ffcalc/ffrows.py      FF 行快照完整性判定 + 外部行文件解析/合并
ffcalc/ffmacro.py     fuse macro CMD_READ 读行协议（Python 用 + 生成内核 C 同源）+ 烧写指令组（sense/arm/program_row，实机确认）
ffcalc/burntool.py     断言式烧写执行 CLI（回显计划警告→离线核验→YES→窗口→SENSE→对快照→arm→逐条 PROGRAM）
ffcalc/analyze.py      潜力分析（FB/SM/NVLink/PCIe）
ffcalc/generate.py     烧写计划生成（16-bit 块合并、行分配）
ffcalc/simulate.py     计划回放核对
ffcalc/ingest.py       ffprobe 日志 → UUID 化 dump + fingerprint
ffcalc/capture.py      备用采集（sysfs BAR0 mmap，适用于内核允许映射 BAR0 的主机；--read-ff-rows 读行）
ffcalc/data/ga100-fuses.json   寄存器地址表（社区逆向，vendored）
samples/gen_probe_c.py         从地址表生成 ffprobe.c（words 只读 + CMD_READ 读行）
scripts/ffscan.sh              一键采集+分析
docs/BURNING.md                烧写操作规程 + 实卡验证记录（寄存器/断言/错误锁死恢复）
tests/                       unittest，无外部依赖
```

## 测试环境

Debian 12，内核 6.18.x-trim，nvidia-open 610.x（cmpunlocker  p2p补丁版 + shim rebar1 修复）。sysfs `resource0` mmap 在该内核上对 NVIDIA BAR0 返回 EINVAL（BAR1/3 正常），因此默认走内核模块；其他内核可先试 `ffcalc/capture.py`。

## 数据来源与致谢

寄存器地址与 option word 位映射来自 [kate-land / ga100vfi](https://blog.kate.land/posts/using-voltage-glitching-to-unlock-the-a100/) 的社区研究，非 NVIDIA 官方文档；CMD_READ 读行流程（FUSECTRL/FUSEADDR/FUSEDATA 偏移、IDLE 判据、超时与地址恢复）照其 `gpu0-registers/gpu0-capture` 实现，见 `ffcalc/ffmacro.py`。本工具与 [cmpunlocker] 无代码依赖。

## License

MIT（见 LICENSE）。

