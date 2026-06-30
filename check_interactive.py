#!/usr/bin/env python3
"""Spawn the emulator in a pseudo-terminal (pty), wait for COMMAND.COM's
DATE prompt, then type the date + time and check for the A> prompt.

This exercises the real interactive code path (cbreak mode, os.read, terminal
restore) the way a human would, without pipe-timing artifacts.
"""
import os, pty, time, select, sys, re

EMU = [sys.executable, 'main.py', '--floppy', 'DOS3_3_525/DISK01.IMG', '--interactive']

def read_avail(fd, timeout=0.1):
    out = b''
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], max(0, end - time.time()))
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            end = time.time() + 0.05  # keep draining briefly
        else:
            break
    return out

pid, fd = pty.fork()
if pid == 0:
    # child
    os.execvp(EMU[0], EMU)
    os._exit(1)

# parent: drive the child
buf = b''
deadline = time.time() + 120
typed_date = False
typed_time = False
saw_aprompt = False
saw_date = False
saw_time = False
while time.time() < deadline:
    chunk = read_avail(fd, 0.2)
    if chunk:
        buf += chunk
        # Strip ANSI for matching.
        plain = re.sub(rb'\x1b\[[0-9;]*[A-Za-z]', b'', buf).decode('ascii', 'replace')
        if not saw_date and ('Enter new date' in plain or 'Enter new time' in plain):
            label = 'DATE' if 'date' in plain else 'TIME'
            if label == 'DATE':
                saw_date = True
            else:
                saw_time = True
            payload = b'01-01-1980\r' if label == 'DATE' else b'\r'
            print(f"[parent] saw {label} prompt, typing {payload!r}", flush=True)
            os.write(fd, payload)
            time.sleep(0.5)
            # Snapshot the VGA so we can see what got echoed.
            snap = read_avail(fd, 0.3)
            if snap:
                buf += snap
                sp = re.sub(rb'\x1b\[[0-9;]*[A-Za-z]', b'', snap).decode('ascii','replace')
                # Print the last non-empty screen row containing the date field.
                rows = [r for r in sp.splitlines() if r.strip()]
                print(f"[parent]   echoed screen rows: {rows[-3:] if rows else '(none)'}", flush=True)
            continue
        if typed_date and not saw_time and 'Enter new time' in plain:
            saw_time = True
            print(f"[parent] saw TIME prompt, typing <CR>", flush=True)
            os.write(fd, b'\r')
            typed_time = True
            time.sleep(0.5)
            continue
        if 'A>' in plain or 'C>' in plain:
            saw_aprompt = True
            print(f"[parent] saw A>/C> prompt!", flush=True)
            break
        if 'Bad or missing' in plain:
            print(f"[parent] ERROR: DOS gave up: Bad or missing", flush=True)
            break
    # check child exited
    try:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid != 0:
            print(f"[parent] child exited status={status}")
            break
    except ChildProcessError:
        break

# show final screen tail for diagnosis
plain = re.sub(rb'\x1b\[[0-9;]*[A-Za-z]', b'', buf).decode('ascii', 'replace')
print("\n=== emulator stderr markers ===")
for line in plain.splitlines():
    if any(m in line for m in ['Step ', 'STUCK', 'HALTED', 'Reached', 'Interactive', 'Interrupted']):
        print(line)
print("\n=== final screen tail ===")
tail = '\n'.join(l.rstrip() for l in plain.splitlines() if l.strip())[-40:]
print(tail)
print(f"\n=== RESULT: date_prompt={saw_date} time_prompt={saw_time} A>_prompt={saw_aprompt} ===")
# tidy up
try:
    os.write(fd, b'\x03')  # Ctrl+C
    time.sleep(0.3)
except OSError:
    pass
try:
    os.close(fd)
except OSError:
    pass
try:
    os.waitpid(pid, 0)
except ChildProcessError:
    pass
sys.exit(0 if saw_aprompt else 1)
