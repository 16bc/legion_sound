#!/usr/bin/env python3
"""
cs35l41-legion-quirk.py - keep internal speakers working on Lenovo Legion 7 16IAX7 (82TD)
across kernel updates.

Per kernel release, idempotently:
  1. locates the stock modules that miss the device quirk:
       snd-hda-scodec-cs35l41   -> device table lacks "17AA3874"
       snd-hda-codec-alc269     -> no quirk for PCI SSID 17aa:382b (Realtek <-> CS35L41 link)
  2. if the module already carries the fix (upstream), does nothing;
  3. otherwise derives a patched copy from the stock module, by PATTERN (never by offset):
       cs35l41 : same-length string swap 17AA386F -> 17AA3874
       alc269  : reuses an existing "sibling" Legion quirk entry (17aa:386f, and a list of
                 other Legion models that use ALC287_FIXUP_CS35L41_I2C_2) and rewrites its
                 PCI subdevice to our 17aa:382b.  The fixup id VALUE is copied from the
                 donor entry, because the enum id shifts between kernel builds
                 (e.g. 0x10d on 7.0.0-34 vs 0x10e on 7.0.0-38).
       then removes the now-invalid .module_sig section;
  4. installs into /lib/modules/<krel>/updates/ (searched BEFORE kernel/);
  5. ensures firmware symlinks 17aa3874 -> 17aa386f;
  6. depmod + update-initramfs (only when something actually changed).

Never modifies the stock modules. Never exits non-zero, so a kernel package
install can never be broken by this tool.
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys
import time

LOG = "/var/log/cs35l41-legion-quirk.log"
STAGE = "/var/lib/cs35l41-legion-quirk"
FW = "/lib/firmware/cirrus"

CS_NAME = "snd-hda-scodec-cs35l41"
ALC_NAME = "snd-hda-codec-alc269"

OLD_STR = b"17AA386F"
NEW_STR = b"17AA3874"

LEGION_VENDOR = 0x17AA
TARGET_PCI_SUB = 0x382B      # our PCI SSID (audio controller 00:1f.3)
TARGET_CODEC_SUB = 0x3874    # our HDA codec SSID (ALC287)

# Legion models that upstream maps to ALC287_FIXUP_CS35L41_I2C_2 - used as donors.
DONOR_IDS = [0x386F, 0x3877, 0x3878, 0x3865, 0x3866,
             0x38B4, 0x38B5, 0x38B6, 0x38B7]

FW_LINKS = [
    ("cs35l41-dsp1-spk-prot-17aa3874-l0.bin.zst", "cs35l41-dsp1-spk-prot-17aa386f-l0.bin.zst"),
    ("cs35l41-dsp1-spk-prot-17aa3874-r0.bin.zst", "cs35l41-dsp1-spk-prot-17aa386f-r0.bin.zst"),
    ("cs35l41-dsp1-spk-prot-17aa3874.wmfw.zst",   "cs35l41-dsp1-spk-prot-17aa386f.wmfw.zst"),
]


def log(msg, logfile=LOG):
    line = "%s %s\n" % (time.strftime("%F %T"), msg)
    sys.stderr.write(line)
    try:
        with open(logfile, "a") as fh:
            fh.write(line)
    except OSError:
        pass


def run(cmd):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode("utf-8", "replace").strip()
    except OSError as exc:
        return 127, str(exc)


def find_stock(root, name):
    cands = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn in (name + ".ko.zst", name + ".ko"):
                cands.append(os.path.join(dirpath, fn))
    cands.sort(key=lambda p: (0 if p.endswith(".zst") else 1, len(p)))
    return cands[0] if cands else None


def to_raw(src, dst):
    if src.endswith(".zst"):
        with open(dst, "wb") as fh:
            p = subprocess.run(["zstdcat", src], stdout=fh)
        return p.returncode == 0
    shutil.copyfile(src, dst)
    return True


# ---- alc269 quirk table helpers -------------------------------------------------
# record (12 bytes): subvendor u16 | subdevice u16 | mask u16 | match_codec_ssid u8 | pad | value u32
REC = struct.Struct("<HHHBB")

def entry_head(subdev, mask=0xFFFF, mc=0):
    return REC.pack(LEGION_VENDOR, subdev, mask, mc, 0)

def build_entry(subdev, value, mask=0xFFFF, mc=0):
    return entry_head(subdev, mask, mc) + struct.pack("<I", value)

def find_entry(data, subdev, mask=0xFFFF, mc=0):
    """All (offset, value) of quirk records for subvendor 17aa / given subdevice."""
    head = entry_head(subdev, mask, mc)
    res, i = [], 0
    while True:
        i = data.find(head, i)
        if i < 0:
            return res
        res.append((i, struct.unpack_from("<I", data, i + 8)[0]))
        i += 1


def patch_cs35l41(data):
    if NEW_STR in data:
        return None, "already carries %s (upstream fix)" % NEW_STR.decode()
    if data.count(OLD_STR) != 1:
        return None, "unexpected count of %s (%d) - not patched" % (OLD_STR.decode(), data.count(OLD_STR))
    return data.replace(OLD_STR, NEW_STR), "swapped %s -> %s" % (OLD_STR.decode(), NEW_STR.decode())


def patch_alc269(data):
    if find_entry(data, TARGET_PCI_SUB) or find_entry(data, TARGET_CODEC_SUB, mc=1):
        return None, "already carries the 17aa:382b/3874 quirk (upstream fix)"
    tried = []
    for dev in DONOR_IDS:
        hits = find_entry(data, dev)
        tried.append("%04x" % dev)
        if not hits:
            continue
        off, value = hits[0]
        if off + 12 > len(data):
            continue
        new = build_entry(TARGET_PCI_SUB, value)
        return (data[:off] + new + data[off + 12:],
                "reused 17aa:%04x (fixup id 0x%x) -> 17aa:382b" % (dev, value))
    return None, "no Legion donor entry found (tried %s) - NOT patched" % ",".join(tried)


def process(name, stockroot, stage, updir, patch_fn, logfile):
    src = find_stock(stockroot, name)
    if not src:
        log("  %-24s stock module not found under %s" % (name, stockroot), logfile)
        return "missing"
    raw = os.path.join(stage, name + ".ko")
    if not to_raw(src, raw):
        log("  %-24s decompress failed" % name, logfile)
        return "error"
    with open(raw, "rb") as fh:
        data = fh.read()
    out, note = patch_fn(data)
    if out is None:
        log("  %-24s %s" % (name, note), logfile)
        return "uptodate"
    with open(raw, "wb") as fh:
        fh.write(out)
    nosig = raw + ".nosig"
    rc, o = run(["objcopy", "--remove-section=.module_sig", raw, nosig])
    if rc == 0 and os.path.exists(nosig):
        os.replace(nosig, raw)
        log("  %-24s removed .module_sig (signature invalid after patching)" % name, logfile)
    else:
        log("  %-24s WARNING objcopy failed rc=%d %s" % (name, rc, o), logfile)
    with open(raw, "rb") as fh:
        final = fh.read()
    dst = os.path.join(updir, name + ".ko")
    old = None
    if os.path.exists(dst):
        with open(dst, "rb") as fh:
            old = fh.read()
    if old == final:
        log("  %-24s %s already up to date (%s)" % (name, dst, note), logfile)
        return "uptodate"
    shutil.copyfile(raw, dst)
    os.chmod(dst, 0o644)
    log("  %-24s installed %s (%s)" % (name, dst, note), logfile)
    return "patched"


def fix_firmware(logfile):
    changed = False
    for link, target in FW_LINKS:
        lp = os.path.join(FW, link)
        tp = os.path.join(FW, target)
        if os.path.lexists(lp):
            continue
        if not os.path.exists(tp):
            log("  fw: %s missing, cannot link %s" % (target, link), logfile)
            continue
        os.symlink(target, lp)
        log("  fw: linked %s -> %s" % (link, target), logfile)
        changed = True
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kernel", nargs="?", default=None)
    ap.add_argument("--stock-root", default=None)
    ap.add_argument("--updates-dir", default=None)
    ap.add_argument("--stage", default=STAGE)
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--no-firmware", action="store_true")
    ap.add_argument("--no-depmod", action="store_true")
    ap.add_argument("--no-initramfs", action="store_true")
    a = ap.parse_args()

    krel = a.kernel
    if not krel:
        with open("/proc/sys/kernel/osrelease") as fh:
            krel = fh.read().strip()

    base = "/lib/modules/" + krel
    stockroot = a.stock_root or (base + "/kernel")
    updir = a.updates_dir or (base + "/updates")

    log("=== cs35l41-legion-quirk: kernel=%s ===" % krel, a.log)

    if not a.stock_root and not os.path.isdir(base):
        log("  kernel dir %s missing -> nothing to do" % base, a.log)
        return 0

    os.makedirs(a.stage, exist_ok=True)
    os.makedirs(updir, exist_ok=True)

    results = {
        "cs35l41": process(CS_NAME, stockroot, a.stage, updir, patch_cs35l41, a.log),
        "alc269": process(ALC_NAME, stockroot, a.stage, updir, patch_alc269, a.log),
    }

    if not a.no_firmware:
        fix_firmware(a.log)

    if "patched" in results.values():
        if not a.no_depmod:
            rc, o = run(["depmod", "-a", krel])
            log("  depmod -a %s rc=%d" % (krel, rc), a.log)
        if not a.no_initramfs:
            rc, o = run(["update-initramfs", "-u", "-k", krel])
            log("  update-initramfs -u -k %s rc=%d" % (krel, rc), a.log)

    log("  result: %s" % results, a.log)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:            # never break a kernel install
        try:
            log("FATAL: %r" % (exc,))
        except Exception:
            pass
        sys.exit(0)
