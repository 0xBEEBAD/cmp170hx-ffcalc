# ffcalc — GA100 CMP 170HX 熔丝解锁预计算工具

针对 [kate-land 的 GA100 VFI 研究](https://blog.kate.land/posts/using-voltage-glitching-to-unlock-the-a100/)的离线辅助工具，纯 Python 标准库 + 一个只读内核探测模块，与 [cmpunlocker] 独立。

> **警告**：FF 熔丝烧写是**物理不可逆**操作，错误目标值可能永久报废显存分区甚至整卡；本工具只做只读采集、分析与计划生成，**不执行烧写**。风险自负。

## 依赖

- Python ≥ 3.6（仅标准库，分析端可完全离线运行）
- 采集端（Linux 主机）：`gcc` / `make` / 与运行内核匹配的 `linux-headers`、`sudo`、已加载 nvidia 驱动的机器（`nvidia-smi` 用于读取 GPU UUID；没有时退化为 fingerprint 认卡）
- 分析端：无要求，dump JSON 拷到哪都能跑

## 一条命令

在装有 nvidia 补丁驱动的 Linux 主机上（仓库根目录）：

```sh
git clone <repo> && cd ffcalc
sudo bash scripts/ffscan.sh
```

它会：构建只读探测模块 `ffprobe.ko` → 对每张 CMP 170HX（device 0x20c2）读 61 个熔丝寄存器（纯 MMIO 读，模块打印完自动卸载，运行中的模型零感知）→ 用 `nvidia-smi` 拿每张卡的 **GPU UUID** → 打印潜力表 → 按 UUID 保存：

```
card UUID                                BDF           FB now->max    SM  FP         plan
GPU-38bbb4b3-bbfc-09b4-6ec2-0cce33417b95 0000:05:00.0  64->80 GiB    124 fc497d01…  plans/38bbb4b3-….burn.json (9 records)
```

- `cards/<uuid去掉GPU-前缀>.json` — 该卡完整 dump（option words、defective、info、fingerprint）
- `plans/<uuid>.burn.json` — 该卡的 FF 烧写计划（文件内含 `uuid` 与 `fingerprint` 字段）
- `FP`（fingerprint）= `*_DEFECTIVE` 全字 + SBFE 位的 sha256 前 16 位。物理缺陷不随烧写变化，所以 **烧前烧后、重启前后 fingerprint 恒定**，用作 nvidia-smi 不在场时的身份核验（glitch 台架上）。

`collected/`、`cards/`、`plans/` 都是运行产物，不要提交；重新插拔、换槽位后重跑一次即可，UUID 不变。


## 烧写前核验（写入工具必须调）

PCIe 端口号会随插槽/重启漂移，UUID 才是卡的唯一身份。烧写工具动手前先跑：

```sh
python -m ffcalc verify plans/<uuid>.burn.json --dir collected   # 现场重新采集后核验
python -m ffcalc verify plans/<uuid>.burn.json --dump 某张卡.json  # 或对着单卡 dump 核验
```

verify 拒绝三种情况：机器里没有这张 UUID 的卡；UUID 对但 fingerprint 不对；计划与卡状态回放不通过（行被占用、old 值对不上、after 不等于目标）。全过才返回 0。

## 子命令

| 命令 | 作用 |
|---|---|
| `scan --dir collected [--outdir cards --plans plans]` | 全表潜力分析 + 按 UUID 保存 dump 与计划 |
| `verify <plan> --dir collected \| --dump dump.json` | UUID/fingerprint/回放 三重核验 |
| `report dump.json` | 单卡潜力分析 |
| `gen dump.json -o plan.json` | 生成 FF 烧写记录（自动 16-bit 块合并） |
| `sim dump.json plan.json` | 逐字段回放核对 |

常用参数：`--set fbp_disable=0x840`（覆盖目标值）、`--only` / `--skip`（字段过滤，首次试烧建议 `--only fbp_disable`）。

## FF 记录格式（本工具严格遵循）

`bits[4:0]=chain, bit5=0(FF), bits[15:6]=16bit 块号, bits[31:16]=新块值`
例：`0x0fe805c1` = chain1 / block23 / data 0x0fe8。链内后写的记录覆盖先写的，因此新记录追加在最后一条已烧行之后。

## 采集原理

目标内核（6.18.x-trim + nvidia 驱动占线）拒绝 GPU BAR0 的 sysfs mmap（BAR1/3 正常），所以走 `samples/gen_probe_c.py` 从 `ffcalc/data/ga100-fuses.json` 生成的最小内核模块：`ioremap(bar0)` → `readl` 61 个寄存器 → `pr_info` → `return -EAGAIN` 自卸载。全程对设备零写入。原始日志格式：

```
FFPROBE <字段名> <寄存器物理地址> <32bit值>
```

`ffcalc/ingest.py` 负责按 BAR0 前缀分组、还原 option words、算 fingerprint。

## 已确认 / 待验证

- 三张实卡数据：两张 80 GiB 潜力（10 FBP），一张 64 GiB 封顶（很郁闷。。）（defective 0x2d）；defective 底线全部读到，option word 映射与博客数据一致
- `DISABLE_GEN3_SPEED` 是 block39 的 bit1 还是 bit9 未定（上游未解决），默认两个都清
- FF 链 CRC 是否由控制器在烧写时自动重算未验证——先 `--only fbp_disable` 烧一行，冷启动后读 `CRC_STATUS==0x55`，通过再烧全量
- MIG / ECC / NVDEC 解锁（`sys_pipe_disable`）作者标注 untested
- 10GB SKU 每 FBP 折算容量按 4 GiB 估
- raw FF 行读出（fuse macro CMD_READ 是一次寄存器写）默认关闭，空闲卡上才可用 `capture.py --read-ff-rows`

## 仓库结构

```
ffcalc/optionmap.py    option word ↔ FF chain/bit 映射（照抄博客数据表）
ffcalc/ffrecord.py     FF 32-bit 记录编解码 + 链回放
ffcalc/analyze.py      潜力分析（FB/SM/NVLink/PCIe）
ffcalc/generate.py     烧写计划生成（16-bit 块合并、行分配）
ffcalc/simulate.py     计划回放核对
ffcalc/ingest.py       ffprobe 日志 → UUID 化 dump + fingerprint
ffcalc/capture.py      备用采集（sysfs BAR0 mmap，适用于内核允许映射 BAR0 的主机）
ffcalc/data/ga100-fuses.json   寄存器地址表（社区逆向，vendored）
samples/gen_probe_c.py         从地址表生成 ffprobe.c（只读内核模块）
scripts/ffscan.sh              一键采集+分析
tests/                       unittest，无外部依赖
```

## 测试环境

Debian 12，内核 6.18.x-trim，nvidia-open 610.x（cmpunlocker  p2p补丁版 + shim rebar1 修复）。sysfs `resource0` mmap 在该内核上对 NVIDIA BAR0 返回 EINVAL（BAR1/3 正常），因此默认走内核模块；其他内核可先试 `ffcalc/capture.py`。

## 数据来源与致谢

寄存器地址与 option word 位映射来自 [kate-land / ga100vfi](https://blog.kate.land/posts/using-voltage-glitching-to-unlock-the-a100/) 的社区研究，非 NVIDIA 官方文档。本工具与 [cmpunlocker] 无代码依赖。

## License

MIT（见 LICENSE）。

