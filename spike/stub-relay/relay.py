#!/usr/bin/env python3
"""Stub relay for the Android spike.

Speaks a single framing on TCP: uint32 big-endian length, then PCM S16LE mono at --rate.
Downstream it emits a continuous sine tone; upstream it measures the incoming stream and prints
one status line per second. Standard library only, loopback by default, no auth, no TLS.

This is not the voice relay. It exists so the Android demo can prove audio flows in both
directions while the screen is off.
"""

import argparse
import array
import math
import socket
import struct
import sys
import threading
import time

FRAME_SECONDS = 0.020


def tone_frame(sample_rate, freq_hz, amplitude, phase):
    n = int(sample_rate * FRAME_SECONDS)
    buf = array.array("h", [0]) * n
    step = 2.0 * math.pi * freq_hz / sample_rate
    for i in range(n):
        buf[i] = int(amplitude * 32767 * math.sin(phase))
        phase += step
    phase %= 2.0 * math.pi
    return buf.tobytes(), phase


def rms_of(pcm):
    if not pcm:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    total = 0
    peak = 0
    for s in samples:
        total += s * s
        a = abs(s)
        if a > peak:
            peak = a
    return math.sqrt(total / len(samples)), peak


def recv_exact(conn, n):
    chunks = []
    remaining = n
    while remaining > 0:
        try:
            chunk = conn.recv(remaining)
        except (ConnectionResetError, OSError):
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def handle(conn, addr, args, stats):
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    stats["connected"] = True
    print(f"relay: client connected from {addr}", flush=True)

    def downstream():
        phase = 0.0
        next_at = time.monotonic()
        while stats["connected"]:
            pcm, phase = tone_frame(args.rate, args.tone_hz, args.amplitude, phase)
            try:
                conn.sendall(struct.pack(">I", len(pcm)) + pcm)
            except (ConnectionResetError, BrokenPipeError, OSError):
                break
            stats["down_frames"] += 1
            stats["down_bytes"] += len(pcm)
            next_at += FRAME_SECONDS
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_at = time.monotonic()

    def upstream():
        while stats["connected"]:
            header = recv_exact(conn, 4)
            if header is None:
                break
            (length,) = struct.unpack(">I", header)
            if length == 0 or length > 1 << 20:
                print(f"relay: bad frame length {length}", flush=True)
                break
            payload = recv_exact(conn, length)
            if payload is None:
                break
            level = rms_of(payload)
            stats["up_frames"] += 1
            stats["up_bytes"] += len(payload)
            if isinstance(level, tuple):
                stats["up_rms"], stats["up_peak"] = level

    threads = [threading.Thread(target=downstream, daemon=True),
               threading.Thread(target=upstream, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        try:
            t.join()
        except Exception:  # noqa: BLE001 - a reset connection must not kill the relay
            pass
    stats["connected"] = False
    try:
        conn.close()
    except OSError:
        pass
    print("relay: client disconnected", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Hermes Talk spike stub relay")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (loopback by default)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--rate", type=int, default=16000)
    parser.add_argument("--tone-hz", type=float, default=440.0)
    parser.add_argument("--amplitude", type=float, default=0.25)
    args = parser.parse_args()

    stats = {"connected": False, "up_frames": 0, "up_bytes": 0, "down_frames": 0,
             "down_bytes": 0, "up_rms": 0.0, "up_peak": 0}
    started = time.monotonic()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(1)
    print(f"relay: listening on {args.host}:{args.port} "
          f"(rate={args.rate} tone={args.tone_hz}Hz)", flush=True)

    def reporter():
        last = (0, 0)
        while True:
            time.sleep(1.0)
            if not stats["connected"]:
                continue
            up, down = stats["up_frames"], stats["down_frames"]
            print(f"relay: t={time.monotonic() - started:7.1f}s "
                  f"up_frames={up} (+{up - last[0]}) up_rms={stats['up_rms']:.0f} "
                  f"up_peak={stats['up_peak']} down_frames={down} (+{down - last[1]})",
                  flush=True)
            last = (up, down)

    threading.Thread(target=reporter, daemon=True).start()

    try:
        while True:
            conn, addr = server.accept()
            handle(conn, addr, args, stats)
    except KeyboardInterrupt:
        print("relay: stopped", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
