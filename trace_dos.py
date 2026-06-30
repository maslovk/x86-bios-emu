#!/usr/bin/env python3
"""Trace DOS 3.3 boot: log every INT 21h / INT 13h / INT 10h / INT 29h call
with arguments BEFORE the handler and the return values AFTER.

Goal: find which INT 21h call DOS's SYSINIT makes immediately before it prints
"Bad or missing CON" and "Bad or missing Command Interpreter".
"""
import sys
sys.path.insert(0, '.')

from main import Emulator
from video import Video

# Dos 3.3 floppy image
IMG = 'DOS3_3_525/DISK01.IMG'

emu = Emulator(boot_file=None, step_mode=False, floppy_image=IMG)
emu.bios.initialize()
if emu.pic:
    emu.pic.initialize()
emu._setup_ivt_irq_handlers()

# Load boot sector at 0x7C00 (same as Emulator.run does)
buf = bytearray(512)
emu.disk.read_sector(0, buf)
for i in range(512):
    emu.mem.write_byte(0x7C00 + i, buf[i])
emu.cpu.cs = 0x0000
emu.cpu.ip = 0x7C00
emu.cpu.ds = 0x0000
emu.cpu.es = 0x0000
emu.cpu.ss = 0x0000
emu.cpu.sp = 0x7C00

if emu.kbd_ctrl:
    emu.kbd_ctrl.feed_string("\r")

# --- INT 21h function-name table (subset relevant to boot) ---
INT21_NAMES = {
    0x01: 'GETC', 0x02: 'PUTC', 0x06: 'DIRCON', 0x07: 'CHARIN', 0x08: 'NOECHO',
    0x09: 'PUTS', 0x0A: 'BUFREAD', 0x0B: 'STAT', 0x0C: 'FLUSH+IN',
    0x0D: 'DISKRESET', 0x0E: 'SETDRV', 0x0F: 'OPENFCB', 0x10: 'CLOSEFCB',
    0x11: 'FINDF_STDA', 0x12: 'FINDN_STDA', 0x13: 'DELFCB', 0x14: 'SEQREAD',
    0x15: 'SEQWRITE', 0x16: 'MKFCB', 0x17: 'RENFCB', 0x19: 'GETDRV',
    0x1A: 'SETDTA', 0x1B: 'GETFAT', 0x1C: 'GETFATDRV', 0x21: 'RANDREAD',
    0x25: 'SETVEC', 0x26: 'NEWPS', 0x27: 'RANDBLKR', 0x29: 'PARSEFNAME',
    0x2C: 'GETTIME', 0x2D: 'SETTIME', 0x2E: 'SETVERIFY', 0x2F: 'GETDTA',
    0x30: 'GETVERSION', 0x31: 'KEEPPROC', 0x33: 'CTRLBRK', 0x35: 'GETVEC',
    0x36: 'FREESPACE', 0x37: 'SWITCH', 0x38: 'COUNTRY', 0x3C: 'CREATE',
    0x3D: 'OPEN', 0x3E: 'CLOSE', 0x3F: 'READ', 0x40: 'WRITE', 0x41: 'UNLINK',
    0x42: 'LSEEK', 0x43: 'GETATTR', 0x44: 'IOCTL', 0x45: 'DUP', 0x46: 'DUP2',
    0x47: 'GETCWD', 0x48: 'ALLOC', 0x49: 'FREE', 0x4A: 'SETBLOCK',
    0x4B: 'EXEC', 0x4C: 'EXIT', 0x4D: 'WAIT', 0x4E: 'FINDF', 0x4F: 'FINDN',
    0x50: 'SETPSP', 0x51: 'GETPSP', 0x52: 'GETLIST', 0x56: 'RENAME',
    0x57: 'GSDATETIME', 0x58: 'STRATEGY', 0x59: 'EXTERR', 0x5D: 'EXTERR',
}

def read_ds_dx_str(cpu, n):
    a = (cpu.ds << 4) + cpu.dx
    out = bytearray()
    for i in range(n):
        b = cpu.mem.read_byte(a + i)
        if b == 0:
            break
        out.append(b)
    return out

def read_es_di_str(cpu, n):
    a = (cpu.es << 4) + cpu.di
    out = bytearray()
    for i in range(n):
        b = cpu.mem.read_byte(a + i)
        if b == 0:
            break
        out.append(b)
    return out

# Log buffer (print everything to stderr, stream to a file)
out = sys.stderr

def logint21_in(cpu, depth):
    ah = (cpu.ax >> 8) & 0xFF
    al = cpu.ax & 0xFF
    name = INT21_NAMES.get(ah, f'UNK{ah:02X}')
    line = f'[INT21 IN ] {name:10s} AH={ah:02X} AL={al:02X} ' \
           f'BX={cpu.bx:04X} CX={cpu.cx:04X} DX={cpu.dx:04X} ' \
           f'SI={cpu.si:04X} DI={cpu.di:04X} DS={cpu.ds:04X} ES={cpu.es:04X}'
    # Show pointed strings where meaningful
    if ah == 0x3D:   # open DS:DX
        s = read_ds_dx_str(cpu, 64)
        line += f'  DS:DX="{s.decode("ascii","replace")}"'
    elif ah == 0x4E:  # find first DS:DX pattern
        s = read_ds_dx_str(cpu, 64)
        line += f'  DS:DX="{s.decode("ascii","replace")}"'
    elif ah == 0x40:  # write BX=handle CX=count DS:DX=buf
        a = (cpu.ds << 4) + cpu.dx
        cnt = min(cpu.cx, 64)
        s = bytes(cpu.mem.read_byte(a+i) for i in range(cnt))
        line += f'  buf="{s.decode("ascii","replace")}"'
    elif ah == 0x09:  # print $ string DS:DX
        a = (cpu.ds << 4) + cpu.dx
        s = bytearray()
        for i in range(128):
            b = cpu.mem.read_byte(a+i)
            if b == ord('$'):
                break
            s.append(b)
        line += f'  DS:DX="{s.decode("ascii","replace")}"'
    elif ah == 0x35:  # get vector AL
        line += f' vec=INT{al:02X}'
    elif ah == 0x25:  # set vector AL
        line += f' vec=INT{al:02X} -> {cpu.ds:04X}:{cpu.dx:04X}'
    elif ah == 0x3C:
        s = read_ds_dx_str(cpu, 64)
        line += f'  DS:DX="{s.decode("ascii","replace")}"'
    elif ah == 0x43:
        s = read_ds_dx_str(cpu, 64)
        line += f'  DS:DX="{s.decode("ascii","replace")}"'
    elif ah == 0x4B:  # EXEC DS:DX prog, ES:BX param block
        s = read_ds_dx_str(cpu, 64)
        pb = (cpu.es << 4) + cpu.bx
        env = cpu.mem.read_word(pb)
        cmdline_off = cpu.mem.read_word(pb+2)
        cmdline_seg = cpu.mem.read_word(pb+4)
        line += f'  DS:DX="{s.decode("ascii","replace")}" env={env:04X} cmd={cmdline_seg:04X}:{cmdline_off:04X}'
    print(line, file=out)

def logint21_out(cpu, depth):
    # AX/carry already set by the INT 21h handler (which is DOS's own code).
    ah = cpu.ax & 0xFF  # on return, AX holds result; AL often handle or count
    cf = bool(cpu.flags & 1)
    label = 'OK ' if not cf else 'ERR'
    print(f'[INT21 OUT] {label} AX={cpu.ax:04X} CF={int(cf)}', file=out)

# Track INT 13h calls
def logint13_in(cpu):
    ah = (cpu.ax >> 8) & 0xFF
    al = cpu.ax & 0xFF
    dl = cpu.dl & 0xFF
    detail = f'AH={ah:02X} AL={al:02X} DL={dl:02X}'
    if ah == 0x02 or ah == 0x03:
        sector = cpu.cl & 0x3F
        head = (cpu.dx >> 8) & 0xFF
        cyl = cpu.ch | ((cpu.cl & 0xC0) << 2)
        media = getattr(emu.disk, 'media_type', 0xF9)
        spt = {0xF9:18,0xF8:15,0xF0:15,0xF1:9,0xFD:9}.get(media,18)
        lba = (cyl*2+head)*spt + (sector-1)
        cnt = al
        detail += f' CHS={cyl}/{head}/{sector} n={cnt} ->LBA={lba} ES:BX={cpu.es:04X}:{cpu.bx:04X}'
    elif ah == 0x08:
        detail += ' (get params)'
    elif ah == 0x00:
        detail += ' (reset)'
    elif ah == 0x15:
        detail += ' (disk type)'
    elif ah == 0x41:
        detail += f' BX={cpu.bx:04X} (ext check)'
    elif ah == 0x42:
        ds = cpu.ds; si = cpu.si
        dap = (ds<<4)+si
        size = cpu.mem.read_word(dap)
        cnt = cpu.mem.read_word(dap+6)
        bseg = cpu.mem.read_word(dap+8)
        boff = cpu.mem.read_word(dap+10)
        lba = cpu.mem.read_dword(dap+12)
        detail += f' extread n={cnt} LBA={lba} buf={bseg:04X}:{boff:04X} sz={size}'
    print(f'[INT13 IN ] {detail}', file=out)

def logint13_out(cpu):
    cf = bool(cpu.flags & 1)
    print(f'[INT13 OUT] AX={cpu.ax:04X} CF={int(cf)}', file=out)

# Wrap the installed hook so we capture interrupts that route to Python
# BIOS handlers AND those that route through the IVT (DOS-installed).
bios_ref = emu.bios

# State: which vector we are currently inside (for OUT logging)
cur_vec = [None]
cur_was_python = [False]

# Pending returns for interrupts routed to DOS (INT 0x21, 0x2F):
# list of {'ret_phys', 'n', 'ah', 'name'}; LIFO matched against cur_ip in loop.
pending = []

INT21_ERRORS = {
    0x01: 'inv_function', 0x02: 'file_not_found', 0x03: 'path_not_found',
    0x04: 'no_handles', 0x05: 'access_denied', 0x06: 'inv_handle',
    0x07: 'mem_ctrl_blk_destroyed', 0x08: 'insufficient_mem', 0x09: 'inv_mem_blk',
    0x0A: 'inv_env', 0x0B: 'inv_format', 0x0C: 'inv_access_code',
    0x0D: 'inv_data', 0x0F: 'inv_drive', 0x11: 'not_same_device',
    0x12: 'no_more_files', 0x13: 'disk_prot_viol', 0x1E: 'general_failure',
}

def hooked_interrupt(n):
    cpu = emu.cpu
    saved_flags = cpu.flags
    cpu._push(saved_flags)
    cpu.tf = False
    cpu.if_flag = False
    cpu._push(cpu.cs)
    cpu._push(cpu.ip)
    cpu.int_no_return = False

    # Determine dispatch target
    ip = cpu.mem.read_word(n*4)
    cs = cpu.mem.read_word(n*4+2)
    stub = bios_ref.ivt_stubs.get(n)
    # If vector has been overwritten by DOS, we transfer control (no py handler for high ints like 0x20/0x21)
    handler = bios_ref.handlers.get(n)

    # Decide routing
    if n == 0x21:
        logint21_in(cpu, 0)
    elif n == 0x13:
        logint13_in(cpu)
    elif n == 0x10:
        ah = (cpu.ax>>8)&0xFF
        al = cpu.ax&0xFF
        if ah == 0x0E:
            ch = al
            printable = chr(ch) if 32 <= ch < 127 else f'[{ch:02X}]'
            print(f'[INT10 IN ] TELETYPE "{printable}" (AL={al:02X})', file=out)
        elif ah == 0x13:
            s_addr = (cpu.es<<4)+cpu.bp
            cnt = cpu.cx
            s = bytes(cpu.mem.read_byte(s_addr+i) for i in range(min(cnt,128)))
            print(f'[INT10 IN ] WRITESTR cnt={cnt} "{s.decode("ascii","replace")}"', file=out)
        elif ah == 0x06 or ah == 0x07:
            print(f'[INT10 IN ] SCROLL AH={ah:02X} BH={(cpu.bx>>8)&0xFF:02X}', file=out)
        elif ah == 0x00:
            print(f'[INT10 IN ] SETMODE AL={al:02X}', file=out)
        elif ah == 0x02:
            print(f'[INT10 IN ] SETCUR DH={(cpu.dx>>8)&0xFF:02X} DL={cpu.dl&0xFF:02X}', file=out)
        elif ah == 0x09 or ah == 0x0A:
            print(f'[INT10 IN ] WRCHAR AH={ah:02X} AL={al:02X} CX={cpu.cx:04X}', file=out)
        elif ah == 0x03:
            print(f'[INT10 IN ] GETCUR BH={cpu.bx&0xFF:02X}', file=out)
        elif ah == 0x0F:
            print(f'[INT10 IN ] GETMODE', file=out)
        elif ah == 0x01:
            print(f'[INT10 IN ] SETCURTYPE CX={cpu.cx:04X}', file=out)
        elif ah == 0x05:
            print(f'[INT10 IN ] SETPAGE AL={al:02X}', file=out)
        else:
            print(f'[INT10 IN ] AH={ah:02X} AL={al:02X} (untracked)', file=out)
    elif n == 0x29:
        ch = cpu.al
        printable = chr(ch) if 32 <= ch < 127 else f'[{ch:02X}]'
        print(f'[INT29 IN ] PUTCHAR "{printable}" (AL={ch:02X})', file=out)
    elif n == 0x20:
        print(f'[INT20 IN ] TERMINATE', file=out)
    elif n == 0x2F:
        print(f'[INT2F IN ] AH={cpu.ax:04X}', file=out)
    elif n == 0x28:
        print(f'[INT28 IN ] IDLE', file=out)

    cur_vec[0] = n
    cur_was_python[0] = (handler is not None and not (stub and (cs,ip)!=stub and (ip,cs)!=(0,0) and handler is None))

    # Dispatch exactly like Emulator._dispatch_hardware_interrupt + bios path
    bios_ref.handle_interrupt(cpu, n)

    if not cpu.int_no_return:
        # Python-handled vector: emit OUT synchronously and IRET-style restore.
        if n == 0x21:
            logint21_out(cpu, 0)
        elif n == 0x13:
            logint13_out(cpu)
        emu._finish_interrupt_return(saved_flags)
    elif n in (0x21, 0x2F):
        # Routed to DOS's own IVT handler: capture return site from the
        # 6-byte frame we just pushed (IP at SS:SP, CS at SS:SP+2) so the
        # run loop can detect arrival back here after the handler IRETs.
        sp = (cpu.ss << 4) + cpu.sp
        rip = cpu.mem.read_word(sp)
        rcs = cpu.mem.read_word(sp + 2)
        ret_phys = (rcs << 4) + rip
        ah = (cpu.ax >> 8) & 0xFF
        name = INT21_NAMES.get(ah, f'UNK{ah:02X}') if n == 0x21 else f'2F_{ah:02X}'
        pending.append({'ret_phys': ret_phys, 'n': n, 'ah': ah, 'name': name})

    cur_vec[0] = None

emu.cpu._do_interrupt = hooked_interrupt

# --- Run loop ---
step = 0
last_ip = None
stuck = 0
PIT_INSN_INTERVAL = 500
pit_acc = 0

try:
    while True:
        if not emu.cpu.halted:
            if not emu.cpu.execute():
                break
            step += 1
        if step > 5_000_000:
            print(f'[TRACE] step limit {step}', file=out)
            break
        if emu.pit:
            if emu.cpu.halted:
                emu.io.tick(1.0/18.2)
            else:
                pit_acc += 1
                if pit_acc >= PIT_INSN_INTERVAL:
                    pit_acc = 0
                    emu.io.tick(1.0/18.2)
        if emu.pic:
            emu._check_and_dispatch_irq()
        # keyboard controller IRQ
        if emu.kbd_ctrl and emu.kbd_ctrl.has_data() and not getattr(emu.kbd_ctrl,'irq_pending',False):
            emu.kbd_ctrl.irq_pending = True
            if emu.pic:
                emu.pic.raise_irq(1)
        cur_ip = (emu.cpu.cs<<4)+emu.cpu.ip
        # Detect arrival at a pending INT 21h / INT 2Fh return site.
        if pending and cur_ip == pending[-1]['ret_phys']:
            p = pending.pop()
            if p['n'] == 0x21:
                cf = bool(emu.cpu.flags & 1)
                ax = emu.cpu.ax
                if p['ah'] == 0x3D:
                    err = INT21_ERRORS.get(ax & 0xFF, 'OK' if not cf else f'unk{ax&0xFF:02X}')
                    print(f"[INT21 OUT] {p['name']:10s} {('ERR='+err) if cf else 'OK'} AX={ax:04X} CF={int(cf)}", file=out)
                else:
                    print(f"[INT21 OUT] {p['name']:10s} AX={ax:04X} CF={int(cf)}", file=out)
            else:
                cf = bool(emu.cpu.flags & 1)
                print(f"[INT2F OUT] AH={p['ah']:02X} AX={emu.cpu.ax:04X} CF={int(cf)}", file=out)
        if cur_ip == last_ip:
            stuck += 1
            if stuck > 200000:
                print(f'[TRACE] stuck at CS:IP={emu.cpu.cs:04X}:{emu.cpu.ip:04X} after {step} steps', file=out)
                break
        else:
            stuck = 0
        last_ip = cur_ip
except KeyboardInterrupt:
    pass

s = emu.cpu.status()
print(f'[TRACE] end CS:IP={s["cs"]:04X}:{s["ip"]:04X} steps={step}', file=out)
