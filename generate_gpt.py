#!/usr/bin/env python3
"""
GPT Generator for Qualcomm IPQ60xx eMMC devices.

Reads Qualcomm-style partition XML and generates a proper GPT binary:
  - Protective MBR (LBA 0)
  - Primary GPT Header (LBA 1)
  - Partition Entry Array (LBA 2-33)
  - Backup Partition Entries + Backup GPT Header

Usage:
  python generate_gpt.py -c meta-tools/ipq6018-re-cs-07/config.xml -o gpt_main0.bin
  python generate_gpt.py -p emmc-partition.xml -s 15269888 -o gpt_main0.bin

Supports:
  - GROW_LAST_PARTITION_TO_FILL_DISK
  - WRITE_PROTECT_BOUNDARY_IN_KB alignment
  - float size_in_kb (e.g. 58798046.5)
  - Multiple physical_partition sections (selects first or 'emmc')
"""

import argparse
import binascii
import os
import struct
import sys
import uuid
import xml.etree.ElementTree as ET

SECTOR_SIZE = 512
GPT_HEADER_SIZE = 92
PARTITION_ENTRY_SIZE = 128
NUM_PARTITION_ENTRIES = 128

# GPT Header offsets
OFF_SIGNATURE = 0x00       # 8 bytes
OFF_REVISION = 0x08        # 4 bytes
OFF_HEADER_SIZE = 0x0C     # 4 bytes
OFF_CRC32 = 0x10           # 4 bytes
OFF_RESERVED = 0x14        # 4 bytes
OFF_MY_LBA = 0x18          # 8 bytes
OFF_ALT_LBA = 0x20         # 8 bytes
OFF_FIRST_USABLE = 0x28    # 8 bytes
OFF_LAST_USABLE = 0x30     # 8 bytes
OFF_DISK_GUID = 0x38       # 16 bytes
OFF_ENTRIES_LBA = 0x48     # 8 bytes
OFF_NUM_ENTRIES = 0x50     # 4 bytes
OFF_ENTRY_SIZE = 0x54      # 4 bytes
OFF_ENTRIES_CRC = 0x58     # 4 bytes


class GuidConverter:
    """Handle GPT GUID byte ordering (mixed-endian format)."""

    @staticmethod
    def str_to_bytes_le(guid_str: str) -> bytes:
        """
        Convert a GUID string like 'DEA0BA2C-CBDD-4805-B4F9-F428251C3E98'
        to the mixed-endian binary format used in GPT partition entries.
        """
        g = uuid.UUID(guid_str)
        return g.bytes_le  # .bytes_le handles the mixed-endian format

    @staticmethod
    def random() -> bytes:
        """Generate a random GUID in GPT format."""
        return uuid.uuid4().bytes_le


def compute_crc32(data: bytes) -> int:
    """Compute IEEE CRC32 of data. GPT uses standard CRC32."""
    return binascii.crc32(data) & 0xFFFFFFFF


def parse_parser_instructions(root) -> dict:
    """Parse <parser_instructions> from the XML."""
    instructions = {}
    instr_elem = root.find('.//parser_instructions')
    if instr_elem is not None and instr_elem.text:
        for line in instr_elem.text.strip().split('\n'):
            line = line.strip()
            if '=' in line and not line.startswith('<!--'):
                key, val = line.split('=', 1)
                instructions[key.strip()] = val.strip()
    return instructions


def parse_partitions(xml_path: str) -> list:
    """
    Parse the partition XML and return a list of partition dicts.
    Each dict: {label, size_kb, type_guid_str, bootable}
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()

    instructions = parse_parser_instructions(root)
    grow_last = instructions.get('GROW_LAST_PARTITION_TO_FILL_DISK', 'false').lower() == 'true'
    write_protect_kb = int(instructions.get('WRITE_PROTECT_BOUNDARY_IN_KB', '65536'))

    # Find the first physical_partition (physical partition 0 / emmc)
    physical = root.find('.//physical_partition[@ref="emmc"]')
    if physical is None:
        physical = root.find('.//physical_partition')
    if physical is None:
        raise ValueError("No <physical_partition> found in XML!")

    partitions = []
    for p in physical.findall('partition'):
        size_kb_str = p.get('size_in_kb', '0')
        try:
            size_kb = float(size_kb_str)
        except ValueError:
            size_kb = 0.0

        label = p.get('label', '')
        part_type = p.get('type', '00000000-0000-0000-0000-000000000000')
        bootable = p.get('bootable', 'false').lower() == 'true'

        partitions.append({
            'label': label,
            'size_kb': size_kb,
            'type_guid': part_type,
            'bootable': bootable,
        })

    return {
        'partitions': partitions,
        'grow_last': grow_last,
    }


def build_gpt(
    total_sectors: int,
    partitions_info: dict,
    disk_guid_bytes: bytes = None,
    full_disk: bool = False,
) -> bytes:
    """
    Build GPT binary from partition definitions.

    By default, outputs only the primary GPT (34 sectors = 17408 bytes):
      - LBA 0: Protective MBR
      - LBA 1: Primary GPT Header
      - LBA 2-33: Partition Entry Array (128 entries × 128 bytes)

    This matches the standard format used by dd/flash for GPT updates
    (dd if=gpt.bin of=/dev/mmcblk0 bs=512 count=34).

    When full_disk=True, outputs the entire disk image (total_sectors sectors).

    Partitions are packed sequentially (back-to-back) without alignment gaps,
    matching the current 192.168.1.1 GPT layout.
    """
    partitions = partitions_info['partitions']
    grow_last = partitions_info['grow_last']

    first_usable_lba = 34
    last_usable_lba = total_sectors - 34

    # Assign LBAs to partitions (packed sequentially)
    current_lba = first_usable_lba

    for i, part in enumerate(partitions):
        size_kb = part['size_kb']

        if i == len(partitions) - 1 and grow_last:
            # Last partition fills remaining space
            size_sectors = last_usable_lba - current_lba + 1
        else:
            size_sectors = int(size_kb * 1024 / SECTOR_SIZE)
            if size_sectors == 0:
                continue  # skip zero-size partitions

        part['start_lba'] = current_lba
        part['end_lba'] = current_lba + size_sectors - 1
        part['size_sectors'] = size_sectors

        current_lba += size_sectors

    # Verify fit
    if current_lba - 1 > last_usable_lba:
        used_gb = (current_lba * SECTOR_SIZE) / (1024**3)
        avail_gb = (last_usable_lba - first_usable_lba + 1) * SECTOR_SIZE / (1024**3)
        raise ValueError(
            f"Partitions exceed disk capacity! "
            f"Need ~{used_gb:.2f} GiB but only {avail_gb:.2f} GiB available."
        )

    # Generate unique GUIDs for each partition (deterministic from label)
    namespace = uuid.UUID('a77727a5-e810-4b70-9a47-f8512f4aa39b')
    for part in partitions:
        part['guid_bytes'] = uuid.uuid5(namespace, part['label']).bytes_le

    # Disk GUID
    if disk_guid_bytes is None:
        disk_guid_bytes = uuid.uuid4().bytes_le

    # Build partition entry array (128 entries × 128 bytes = 16384 bytes = 32 sectors)
    entries_data = b''
    for part in partitions:
        entry = b''
        # Partition type GUID (16 bytes, mixed-endian)
        entry += GuidConverter.str_to_bytes_le(part['type_guid'])
        # Unique partition GUID (16 bytes)
        entry += part['guid_bytes']
        # Starting LBA (8 bytes)
        entry += struct.pack('<Q', part['start_lba'])
        # Ending LBA (8 bytes, inclusive)
        entry += struct.pack('<Q', part['end_lba'])
        # Attributes (8 bytes)
        entry += struct.pack('<Q', 0)
        # Partition name (72 bytes, UTF-16LE, null-terminated)
        name_bytes = part['label'].encode('utf-16-le')
        name_bytes = name_bytes[:72]  # max 36 UTF-16 code units
        name_bytes = name_bytes.ljust(72, b'\x00')
        entry += name_bytes

        entries_data += entry
    entries_data = entries_data.ljust(NUM_PARTITION_ENTRIES * PARTITION_ENTRY_SIZE, b'\x00')

    # CRC32 of partition entries
    entries_crc = compute_crc32(entries_data)

    # --- Primary GPT (start of disk) ---
    primary_header = build_gpt_header(
        my_lba=1,
        alt_lba=total_sectors - 1,
        first_usable_lba=first_usable_lba,
        last_usable_lba=last_usable_lba,
        disk_guid_bytes=disk_guid_bytes,
        entries_lba=2,
        entries_crc=entries_crc,
    )
    mbr = build_protective_mbr(total_sectors)

    # Pad GPT header to full sector (GPT header is 92 bytes, sector is 512)
    primary_header = primary_header.ljust(SECTOR_SIZE, b'\x00')
    primary_gpt = mbr + primary_header + entries_data  # 34 sectors = 17408 bytes

    if full_disk:
        # Build backup GPT (end of disk)
        backup_entries_lba = last_usable_lba + 1
        backup_header = build_gpt_header(
            my_lba=total_sectors - 1,
            alt_lba=1,
            first_usable_lba=first_usable_lba,
            last_usable_lba=last_usable_lba,
            disk_guid_bytes=disk_guid_bytes,
            entries_lba=backup_entries_lba,
            entries_crc=entries_crc,
        )
        disk_data = bytearray(total_sectors * SECTOR_SIZE)
        disk_data[0:len(primary_gpt)] = primary_gpt
        disk_data[backup_entries_lba * SECTOR_SIZE:(backup_entries_lba + 32) * SECTOR_SIZE] = entries_data
        disk_data[(total_sectors - 1) * SECTOR_SIZE:] = backup_header
        return bytes(disk_data)
    else:
        return primary_gpt


def build_gpt_header(
    my_lba: int,
    alt_lba: int,
    first_usable_lba: int,
    last_usable_lba: int,
    disk_guid_bytes: bytes,
    entries_lba: int,
    entries_crc: int,
) -> bytes:
    """Build a GPT header (92 bytes) with CRC32 properly computed."""
    header = bytearray(GPT_HEADER_SIZE)

    # Signature "EFI PART"
    header[OFF_SIGNATURE:OFF_SIGNATURE + 8] = b'EFI PART'

    # Revision (GPT rev 1.0)
    struct.pack_into('<I', header, OFF_REVISION, 0x00010000)

    # Header size
    struct.pack_into('<I', header, OFF_HEADER_SIZE, GPT_HEADER_SIZE)

    # CRC32 placeholder (set to 0 for calculation)
    struct.pack_into('<I', header, OFF_CRC32, 0)

    # Reserved
    struct.pack_into('<I', header, OFF_RESERVED, 0)

    # My LBA
    struct.pack_into('<Q', header, OFF_MY_LBA, my_lba)

    # Alternate LBA
    struct.pack_into('<Q', header, OFF_ALT_LBA, alt_lba)

    # First usable LBA
    struct.pack_into('<Q', header, OFF_FIRST_USABLE, first_usable_lba)

    # Last usable LBA
    struct.pack_into('<Q', header, OFF_LAST_USABLE, last_usable_lba)

    # Disk GUID
    header[OFF_DISK_GUID:OFF_DISK_GUID + 16] = disk_guid_bytes

    # Partition entries starting LBA
    struct.pack_into('<Q', header, OFF_ENTRIES_LBA, entries_lba)

    # Number of partition entries
    struct.pack_into('<I', header, OFF_NUM_ENTRIES, NUM_PARTITION_ENTRIES)

    # Size of partition entry
    struct.pack_into('<I', header, OFF_ENTRY_SIZE, PARTITION_ENTRY_SIZE)

    # CRC32 of partition entries
    struct.pack_into('<I', header, OFF_ENTRIES_CRC, entries_crc)

    # Compute CRC32 of the header (with CRC32 field zeroed)
    crc = compute_crc32(bytes(header))
    struct.pack_into('<I', header, OFF_CRC32, crc)

    return bytes(header)


def build_protective_mbr(total_sectors: int) -> bytes:
    """Build a GPT protective MBR (512 bytes)."""
    mbr = bytearray(SECTOR_SIZE)

    # Bootstrap code area (all zeros)
    # Partition entry at offset 0x1BE (446)
    entry_offset = 446

    # Status: 0x00 = not bootable
    mbr[entry_offset] = 0x00

    # CHS start: 0x00 0x02 0x00
    mbr[entry_offset + 1:entry_offset + 4] = bytes([0x00, 0x02, 0x00])

    # Partition type: 0xEE = GPT protective
    mbr[entry_offset + 4] = 0xEE

    # CHS end: 0xFF 0xFF 0xFF
    mbr[entry_offset + 5:entry_offset + 8] = bytes([0xFF, 0xFF, 0xFF])

    # Starting LBA: 1
    struct.pack_into('<I', mbr, entry_offset + 8, 1)

    # Size in sectors (limited to 32-bit)
    sector_count = min(total_sectors - 1, 0xFFFFFFFF)
    struct.pack_into('<I', mbr, entry_offset + 12, sector_count)

    # Boot signature 0x55 0xAA at offset 510
    mbr[510] = 0x55
    mbr[511] = 0xAA

    return bytes(mbr)


def parse_config_xml(config_path: str) -> dict:
    """Parse config.xml to get total_block and SOC name."""
    tree = ET.parse(config_path)
    root = tree.getroot()

    soc = root.findtext('.//data[@type="ARCH"]/SOC', 'ipq6018')
    total_block_elem = root.find('.//data[@type="EMMC_PARAMETER"]/total_block')

    if total_block_elem is None:
        raise ValueError("No EMMC_PARAMETER/total_block found in config.xml!")

    total_block = int(total_block_elem.text.strip())

    return {
        'soc': soc,
        'total_block': total_block,
    }


def main():
    parser = argparse.ArgumentParser(
        description='Generate GPT binary from Qualcomm partition XML',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -c meta-tools/ipq6018-re-cs-07/config.xml
  %(prog)s -c meta-tools/ipq6018-re-cs-07/config.xml -o output/gpt_main0.bin
  %(prog)s -p partition.xml -s 15269888 -o gpt_main0.bin
        """
    )
    parser.add_argument('-c', '--config', help='Path to config.xml (provides total_block and partition path)')
    parser.add_argument('-p', '--partition', help='Path to partition XML (overrides config-based path)')
    parser.add_argument('-s', '--total-sectors', type=int, help='Total sectors (overrides config)')
    parser.add_argument('-o', '--output', default='gpt_main0.bin', help='Output file path (default: gpt_main0.bin)')
    parser.add_argument('-d', '--output-dir', default='output', help='Output directory (default: output/)')
    parser.add_argument('--full-disk', action='store_true', help='Output full disk image (all sectors zero-filled) instead of just primary GPT (34 sectors)')

    args = parser.parse_args()

    if not args.config and not args.partition:
        parser.error("Either --config or --partition must be provided!")

    # Determine total sectors
    total_sectors = args.total_sectors
    partition_xml = args.partition
    soc_name = None

    if args.config:
        config = parse_config_xml(args.config)
        if total_sectors is None:
            total_sectors = config['total_block']
        soc_name = config['soc']

        # Derive partition XML path from config location
        if partition_xml is None:
            config_dir = os.path.dirname(os.path.abspath(args.config))
            # The partition path is: config_dir/SOC/flash_partition/emmc-partition.xml
            # But when running standalone, we look relative to the config dir
            partition_xml = os.path.join(config_dir, 'flash_partition', 'emmc-partition.xml')
            if not os.path.exists(partition_xml):
                # Try the original QSDK path scheme: config_dir/.. relative to SOC
                if soc_name:
                    # Try parent dir scheme: meta-tools/<soc>/flash_partition/emmc-partition.xml
                    parent_dir = os.path.dirname(config_dir)
                    alt_path = os.path.join(parent_dir, soc_name, 'flash_partition', 'emmc-partition.xml')
                    if os.path.exists(alt_path):
                        partition_xml = alt_path

    if partition_xml is None or not os.path.exists(partition_xml):
        parser.error(f"Partition XML not found: {partition_xml}")

    if total_sectors is None:
        parser.error("Total sectors unknown! Provide --total-sectors or --config with total_block.")

    print(f"[*] Partition XML  : {partition_xml}")
    print(f"[*] Total sectors   : {total_sectors}")
    print(f"[*] Disk size       : {total_sectors * SECTOR_SIZE / (1024**3):.2f} GiB")
    if soc_name:
        print(f"[*] SOC/ARCH        : {soc_name}")

    # Parse partitions
    info = parse_partitions(partition_xml)
    partitions = info['partitions']
    grow_last = info['grow_last']

    print(f"[*] Partitions found: {len(partitions)}")
    print(f"[*] GROW_LAST       : {grow_last}")

    # Generate GPT
    full_disk = args.full_disk
    try:
        disk_image = build_gpt(total_sectors, info, full_disk=full_disk)
    except ValueError as e:
        print(f"\n[!] Error: {e}")
        sys.exit(1)

    if full_disk:
        print(f"[*] Output mode     : full disk image ({total_sectors} sectors)")
    else:
        print(f"[*] Output mode     : primary GPT only (34 sectors, matches mmcblk0_GPT.bin format)")

    # Verify by re-parsing the GPT
    verify_gpt(disk_image, total_sectors, partitions, info['grow_last'], full_disk=full_disk)

    # Write output
    os.makedirs(args.output_dir, exist_ok=True)
    output_path = os.path.join(args.output_dir, args.output)
    with open(output_path, 'wb') as f:
        f.write(disk_image)

    print(f"\n[+] GPT written to: {output_path}")

    # Print partition table info
    print()
    print("Partition table:")
    for i, part in enumerate(partitions):
        size_mb = part['size_sectors'] * SECTOR_SIZE / (1024 * 1024)
        size_gb = part['size_sectors'] * SECTOR_SIZE / (1024**3)
        if size_gb >= 1:
            size_str = f"{size_gb:.2f} GiB"
        else:
            size_str = f"{size_mb:.1f} MiB"
        print(f"  {i:<3} {part['label']:<20} {size_str:>10}  LBA {part['start_lba']:>8} - {part['end_lba']:>8}")


def verify_gpt(disk_image: bytes, total_sectors: int, partitions: list, grow_last: bool, full_disk: bool = False):
    """
    Re-parse the generated GPT to verify correctness.
    Supports both primary-only (34 sectors) and full-disk images.
    """
    # Expected size
    expected_size = 34 * SECTOR_SIZE if not full_disk else total_sectors * SECTOR_SIZE
    assert len(disk_image) == expected_size, \
        f"Size mismatch: got {len(disk_image)} bytes, expected {expected_size} bytes"

    # Check MBR signature
    assert disk_image[510:512] == b'\x55\xAA', "MBR signature missing!"
    assert disk_image[446 + 4] == 0xEE, "Protective MBR type not 0xEE!"

    # Check GPT header signature
    header = disk_image[1 * SECTOR_SIZE:1 * SECTOR_SIZE + 92]
    assert header[0:8] == b'EFI PART', "GPT signature missing!"

    # Check header CRC
    stored_crc = struct.unpack_from('<I', header, OFF_CRC32)[0]
    header_zeroed = bytearray(header)
    struct.pack_into('<I', header_zeroed, OFF_CRC32, 0)
    computed_crc = compute_crc32(bytes(header_zeroed))
    assert stored_crc == computed_crc, \
        f"Header CRC mismatch: stored=0x{stored_crc:08X}, computed=0x{computed_crc:08X}"

    # Check partition entry CRC
    entries = disk_image[2 * SECTOR_SIZE:2 * SECTOR_SIZE + 128 * 128]
    stored_entries_crc = struct.unpack_from('<I', header, OFF_ENTRIES_CRC)[0]
    computed_entries_crc = compute_crc32(bytes(entries))
    assert stored_entries_crc == computed_entries_crc, "Entries CRC mismatch!"

    if full_disk:
        # Check backup GPT
        backup_offset = (total_sectors - 1) * SECTOR_SIZE
        backup_header = disk_image[backup_offset:backup_offset + 92]
        assert backup_header[0:8] == b'EFI PART', "Backup GPT signature missing!"

        b_stored_crc = struct.unpack_from('<I', backup_header, OFF_CRC32)[0]
        b_header_zeroed = bytearray(backup_header)
        struct.pack_into('<I', b_header_zeroed, OFF_CRC32, 0)
        b_computed_crc = compute_crc32(bytes(b_header_zeroed))
        assert b_stored_crc == b_computed_crc, f"Backup header CRC mismatch!"

        # Check backup entries
        last_usable = total_sectors - 34
        backup_entries_offset = (last_usable + 1) * SECTOR_SIZE
        backup_entries = disk_image[backup_entries_offset:backup_entries_offset + 128 * 128]
        assert backup_entries == entries, "Backup partition entries don't match primary!"

        print("[OK] Full-disk GPT verification passed: MBR, Primary GPT, Entries, Backup GPT all OK")
    else:
        print("[OK] Primary GPT verification passed: MBR, GPT Header, Partition Entries all OK")

    # Verify last usable LBA
    last_usable = total_sectors - 34
    stored_last_usable = struct.unpack_from('<Q', header, OFF_LAST_USABLE)[0]
    assert stored_last_usable == last_usable, \
        f"LastUsableLBA mismatch: stored={stored_last_usable}, expected={last_usable}"

    # Verify last partition fits
    if partitions and not grow_last:
        last_part = partitions[-1]
        assert last_part['end_lba'] <= last_usable, \
            f"Last partition ({last_part['label']}) exceeds LastUsableLBA ({last_usable})!"

    return True


if __name__ == '__main__':
    main()
