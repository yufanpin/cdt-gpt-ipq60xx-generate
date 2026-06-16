# CDT and GPT Generate for Qualcomm IPQ60xx

生成 Qualcomm IPQ60xx 系列设备的 eMMC GPT（GUID Partition Table）和 CDT（Configuration Data Table）。

## 快速上手

### 生成 GPT

```bash
python generate_gpt.py -c meta-tools/ipq6018-re-cs-07/config.xml -o gpt_main0.bin
```

输出 `output/gpt_main0.bin`（17408 字节，34 扇区 — 标准 GPT dump 格式）

### 刷入设备

```bash
# 从 U-Boot 或 Linux 系统
dd if=gpt_main0.bin of=/dev/mmcblk0 bs=512 count=34
```

> ⚠️ **只刷 GPT 不刷固件会导致系统无法启动**。刷完 GPT 后需要把对应分区的 firmware 也刷入（HLOS、rootfs 等）。
> 建议：配合 OpenWRT 固件一起刷，先刷 GPT 再刷固件。

---

## 目录结构

```
cdt-gpt-ipq60xx-generate/
│
├── generate_gpt.py              # Python 3 GPT 生成器（核心，无需 QSDK 工具链）
│
├── meta-tools/
│   ├── ipq6018-re-cs-07/         # ⭐ 已有配置：re-cs-07 设备
│   │   ├── config.xml            #    设备信息（磁盘大小、SOC、SDRAM 等）
│   │   └── flash_partition/
│   │       └── emmc-partition.xml #    分区表定义
│   │
│   ├── ipq6018/                  # 参考配置：原厂 64GB eMMC（需 Python 2.7 QSDK）
│   │   └── flash_partition/
│   │       └── emmc-partition.xml
│   │
│   └── ipq5018/ ipq807x/ ipq806x/ ipq40xx/ ...  # 其他 SoC 参考
│
├── .github/workflows/build.yml   # GitHub Actions CI：自动编译 → 发布 Release
│
└── output/                       # 生成的文件（gitignored）
    └── gpt_main0.bin
```

---

## 如何为新设备自定义 GPT（核心教程）

假设你要为新设备 **张三 AX3000 Pro**（ipq6018, 8GB eMMC）创建分区表。

### 第一步：确认磁盘大小

通过原厂固件获取：

```bash
# 方法 1：从运行中的设备读取
cat /sys/block/mmcblk0/size
# 输出: 15269888  ← 这是总扇区数

# 方法 2：从原厂 GPT 中提取
python3 -c "
import struct
with open('mmcblk0_GPT.bin', 'rb') as f:
    data = f.read()
header = data[0x200:0x200+92]  # LBA 1
last_usable = struct.unpack('<Q', header[0x30:0x38])[0]
total_sectors = last_usable + 34
print(f'Total sectors: {total_sectors}')
print(f'Disk size: {total_sectors * 512 / 1024**3:.2f} GiB')
"
```

### 第二步：创建 config.xml

复制现有配置并修改：

```bash
cp -r meta-tools/ipq6018-re-cs-07 meta-tools/ipq6018-zhangsan-ax3000
```

编辑 `config.xml`，修改以下字段：

| 字段 | 位置 | 说明 |
|------|------|------|
| `<SOC>ipq6018-zhangsan-ax3000</SOC>` | ARCH | 设备标识，用于文件路径推导 |
| `<total_block>15269888</total_block>` | EMMC_PARAMETER | **磁盘总扇区数**（必须正确！） |
| `<machid>0x8030300</machid>` | MACH_ID_BOARD_MAP | 设备的 machine ID |
| `<board>zhangsan-ax3000</board>` | MACH_ID_BOARD_MAP | 设备名 |

对于不同容量 eMMC 的 total_block 参考值：

| 标称容量 | 总扇区数 |
|---------|---------|
| 4GB  | ~7,811,072 (7.28 GiB 实际) |
| 8GB  | ~15,269,888 (14.57 GiB 实际) |
| 16GB | ~30,736,384 (29.32 GiB 实际) |
| 64GB | ~125,042,688 (119.25 GiB 实际) |

> 实际可用容量可能因 eMMC 批次略有不同，以 `cat /sys/block/mmcblk0/size` 为准。

### 第三步：编辑分区表 emmc-partition.xml

编辑 `meta-tools/ipq6018-zhangsan-ax3000/flash_partition/emmc-partition.xml`。

#### 分区格式

```xml
<partition
    label="0:HLOS"              <!-- 分区名（在 Linux 下显示为 /dev/mmcblk0p16） -->
    size_in_kb="12288"          <!-- 大小，单位 KB（12MB = 12288KB） -->
    type="B51F2982-3EBE-46DE-8721-EE641E1F9997"  <!-- GPT 分区类型 GUID -->
    bootable="false"
    readonly="false"
    diff_files="true"
    image_type="hlos"           <!-- 固件类型标识，用于打包 -->
    filename_32="openwrt-ipq60xx-fit-uImage.itb"  <!-- 32位固件文件名 -->
    filename_64="openwrt-ipq60xx_64-fit-uImage.itb" />
```

#### Boot 分区（0:SBL1 ~ 0:ART）

这 15 个分区是 **SBL/U-Boot 启动链** 的固件分区：

| 分区 | 大小 | 说明 |
|------|------|------|
| 0:SBL1 | 768KB | 二级 Bootloader |
| 0:BOOTCONFIG / 0:BOOTCONFIG1 | 256KB each | Boot 配置 |
| 0:QSEE / 0:QSEE_1 | 1792KB each | TrustZone |
| 0:DEVCFG / 0:DEVCFG_1 | 256KB each | 设备配置 |
| 0:RPM / 0:RPM_1 | 256KB each | Resource Power Manager |
| 0:CDT / 0:CDT_1 | 256KB each | 配置数据表 |
| 0:APPSBLENV | 256KB | U-Boot 环境变量 |
| 0:APPSBL / 0:APPSBL_1 | 640KB each | **U-Boot** |
| 0:ART | 512KB | 射频校准数据 |

> ⚠️ **这 15 个分区的起始 LBA 和大小在所有 IPQ60xx 设备上基本固定，不建议修改。**
> 如果只想改 firmware 分区，**只动 HLOS / rootfs / storage 即可**。

#### 自定义分区要点

```
0:HLOS — kernel 分区 (FIT image)
  ├── 大小参考：原厂 6MB，建议 12MB（kernel 通常 4-6MB）
  ├── 双分区升级：0:HLOS（主）+ 0:HLOS_1（备份）
  └── 类型 GUID: B51F2982-3EBE-46DE-8721-EE641E1F9997

rootfs — 根文件系统
  ├── 大小：通常 1GB (1048576KB) 或更大
  └── 类型 GUID: 98D2248D-7140-449F-A954-39D67BD6C3B4

storage — 用户数据 / overlay
  ├── 设 size_in_kb="0"  +  GROW_LAST_PARTITION_TO_FILL_DISK=true
  └── 自动占满剩余空间
```

> 常用分区类型 GUID 对照表见文末附录。

#### parser_instructions 选项

```xml
<parser_instructions>
    GROW_LAST_PARTITION_TO_FILL_DISK = true   <!-- 最后一个分区自动填满磁盘 -->
    WRITE_PROTECT_BOUNDARY_IN_KB    = 65536   <!-- 写保护边界（默认 64MB） -->
</parser_instructions>
```

### 第四步：生成 GPT

```bash
python generate_gpt.py -c meta-tools/ipq6018-zhangsan-ax3000/config.xml -o gpt_main0.bin
```

输出文件会包含 34 扇区的 GPT dump，同时脚本会自动校验：

```
[OK] Primary GPT verification passed: MBR, GPT Header, Partition Entries all OK
```

### 第五步：验证 GPT

```bash
# 查看分区表
python generate_gpt.py -c meta-tools/ipq6018-zhangsan-ax3000/config.xml

# 或者从生成的二进制解析
python3 -c "
import struct
with open('gpt_main0.bin', 'rb') as f:
    data = f.read()
for i in range(128):
    off = 1024 + i*128
    entry = data[off:off+128]
    if entry[0:16] == b'\x00'*16: break
    name = entry[56:128].decode('utf-16-le').rstrip('\x00')
    slba = struct.unpack('<Q', entry[32:40])[0]
    elba = struct.unpack('<Q', entry[40:48])[0]
    size = (elba-slba+1)*512/1024/1024
    print(f'{name:20} {slba:8}-{elba:8}  {size:.1f}M' if size<1024 else f'{name:20} {slba:8}-{elba:8}  {size/1024:.2f}G')
"
```

### 第六步：刷入设备

```bash
# 刷 GPT
dd if=gpt_main0.bin of=/dev/mmcblk0 bs=512 count=34

# 验证写入结果
dd if=/dev/mmcblk0 bs=512 count=34 | md5sum
md5sum gpt_main0.bin
# 两个 md5 应该一致
```

---

## CI 自动编译

提交到 GitHub 的 `test` 分支会自动触发 CI，流程：

```
git commit → push → GitHub Actions → 编译 GPT → 发布 Release
```

`test` 分支每次 push 都会编译并发布 Release（非 draft）。

---

## 参考

### re-cs-07 分区布局（当前配置）

| 分区 | 大小 | 起始 LBA | 结束 LBA | 说明 |
|------|:----:|:--------:|:--------:|------|
| 0:SBL1 | 0.8MB | 34 | 1569 | XBL |
| 0:BOOTCONFIG | 0.2MB | 1570 | 2081 | Boot config |
| 0:BOOTCONFIG1 | 0.2MB | 2082 | 2593 | Boot config 备份 |
| 0:QSEE | 1.8MB | 2594 | 6177 | TrustZone |
| 0:QSEE_1 | 1.8MB | 6178 | 9761 | TZ 备份 |
| 0:DEVCFG | 0.2MB | 9762 | 10273 | Device config |
| 0:DEVCFG_1 | 0.2MB | 10274 | 10785 | Devcfg 备份 |
| 0:RPM | 0.2MB | 10786 | 11297 | RPM firmware |
| 0:RPM_1 | 0.2MB | 11298 | 11809 | RPM 备份 |
| 0:CDT | 0.2MB | 11810 | 12321 | Configuration Data Table |
| 0:CDT_1 | 0.2MB | 12322 | 12833 | CDT 备份 |
| 0:APPSBLENV | 0.2MB | 12834 | 13345 | U-Boot 环境变量 |
| 0:APPSBL | 0.6MB | 13346 | 14625 | **U-Boot 主** |
| 0:APPSBL_1 | 0.6MB | 14626 | 15905 | **U-Boot 备份** |
| 0:ART | 0.5MB | 15906 | 16929 | 射频校准数据 |
| **0:HLOS** | **12MB** | **16930** | **41505** | **Kernel (主核)** |
| **0:HLOS_1** | **6MB** | **41506** | **53793** | **Kernel (备份核)** |
| **rootfs** | **1GB** | **53794** | **2150945** | **根文件系统** |
| **storage** | **~6.26GB** | **2150946** | **15269854** | **用户数据 / overlay** |

### 刷写 U-Boot

```bash
# 刷到 0:APPSBL（LBA 13346，640KB）
dd if=uboot.bin of=/dev/mmcblk0 bs=512 seek=13346 count=1280 conv=fsync

# 刷到 0:APPSBL_1（LBA 14626，640KB）
dd if=uboot.bin of=/dev/mmcblk0 bs=512 seek=14626 count=1280 conv=fsync
```

### 附录：常用 GPT 分区类型 GUID

| 用途 | GUID |
|------|------|
| 普通数据分区 | `EBD0A0A2-B9E5-4433-87C0-68B6B72699C7` |
| HLOS (kernel) | `B51F2982-3EBE-46DE-8721-EE641E1F9997` |
| HLOS_1 | `A71DA577-7F81-4626-B4A2-E377F9174525` |
| rootfs | `98D2248D-7140-449F-A954-39D67BD6C3B4` |
| rootfs_1 | `5647B280-DC2A-485D-9913-CF53AC40FA32` |
| storage | `1B1720DA-A8BB-4B6F-92D2-0A93AB9609CA` |
| SBL1 | `DEA0BA2C-CBDD-4805-B4F9-F428251C3E98` |
| BOOTCONFIG | `2B7D04FF-31F0-4E6A-BE9A-DA50314DAD58` |
| BOOTCONFIG1 | `7BD25378-5C39-11E5-8A77-40A8F05F1418` |
| QSEE (TZ) | `A053AA7F-40B8-4B1C-BA08-2F68AC71A4F4` |
| QSEE_1 | `A6DD74A1-C8BF-4DBC-AE39-62B8E78C4038` |
| DEVCFG | `F65D4B16-343D-4E25-AAFC-BE99B6556A6D` |
| DEVCFG_1 | `48BFA451-9443-46F7-B400-892A6B1BFC16` |
| RPM | `098DF793-D712-413D-9D4E-89D711772228` |
| RPM_1 | `2D2BE762-890B-11E5-AAF3-40A8F05F1418` |
| CDT | `A19F205F-CCD8-4B6D-8F1E-2D9BC24CFFB1` |
| CDT_1 | `7A795379-C250-4282-A2C7-FC4E13F4A43D` |
| APPSBLENV | `300FFDCD-22E0-47E7-9A23-F16ED9382387` |
| APPSBL (U-Boot) | `400FFDCD-22E0-47E7-9A23-F16ED9382388` |
| APPSBL_1 | `C126787D-3EEF-444C-9E43-FEFF3F103E22` |
| ART (caldata) | `A72E50C1-D37C-429D-9620-35FCA612B9A8` |
