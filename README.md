You will also like to see https://github.com/lgs2007m/uboot-ipq60xx-build

# CDT and GPT generate

CDT is Qualcomm Configuration Data Table.
GPT is GUID Partition Table.

## Prerequisites

- **Linux**: Python 2.7 (for QSDK tools) or Python 3 (for standalone GPT generator)
- **Windows**: Python 3 (standalone GPT generator only)

## Quick Start (Python 3, no QSDK needed)

Generate re-cs-07 GPT (simplified layout, HLOS=16MB, rootfs=1GB):

```bash
python generate_gpt.py -c meta-tools/ipq6018-re-cs-07/config.xml -o gpt_main0.bin
```

Output: `output/gpt_main0.bin` (17408 bytes, 34 sectors — standard GPT dump format)

Options:
- `--full-disk`: Output full disk image (all sectors zero-filled)
- `-s <sectors>`: Override total sectors
- `-p <file>`: Use custom partition XML

## Original QSDK Method (Linux, Python 2.7)

### Generate CDT + GPT for factory boards

```bash
git clone https://github.com/lgs2007m/cdt-gpt-ipq60xx-generate
cd cdt-gpt-ipq60xx-generate/meta-tools/
python2.7 prepareSingleImage.py --arch ipq6018 --fltype emmc --gencdt --genpart
```

### Windows (Python 2.7)

```cmd
cd cdt-gpt-ipq60xx-generate/meta-tools/
prepareSingleImage.py --arch ipq6018 --fltype emmc --gencdt --genpart
```

## Output files

| Config | Target | Description | File |
|--------|--------|-------------|------|
| `ipq6018` | Factory (FSEIASLD-64G-J02) | 64GB eMMC, full layout | `meta-tools/ipq6018/in/gpt_main0.bin` |
| `ipq6018-re-cs-07` | re-cs-07 (8GB eMMC) | Simplified layout, HLOS=16MB | `output/gpt_main0.bin` |

CDT binary (1G SDRAM):
`meta-tools/ipq6018/in/cdt-AP-CP03-C2_Arthur_512M16(1G)_DDR3.bin`

## re-cs-07 Partition Layout

| Partition | Size | Start LBA | Description |
|-----------|------|-----------|-------------|
| 0:SBL1 ~ 0:ART (15 parts) | ~8 MB | 34–16929 | Boot firmware (unchanged) |
| **0:HLOS** | **16 MB** | 16930–49697 | Kernel/FIT image |
| rootfs | 1024 MB | 49698–2146849 | Root filesystem (squashfs) |
| storage | ~6.26 GB | 2146850–15269854 | User data / overlay |

### Flashing

```bash
# Write GPT to device (from U-Boot or Linux)
dd if=gpt_main0.bin of=/dev/mmcblk0 bs=512 count=34
```
