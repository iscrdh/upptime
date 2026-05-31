#!/usr/bin/env python3
"""Genera los iconos PNG de la PWA sin dependencias externas.

Dibuja un fondo con degradado oscuro y una linea de tendencia ascendente
(verde) con un punto luminoso, evocando mercados/cripto. Salida: PNG RGBA.
"""
import struct
import zlib
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def lerp(a, b, t):
    return a + (b - a) * t


def mix(c1, c2, t):
    return tuple(int(round(lerp(c1[i], c2[i], t))) for i in range(3))


def dist_point_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def make_icon(size):
    top = (11, 16, 32)        # #0b1020
    bottom = (23, 31, 54)     # #171f36
    green = (22, 199, 132)    # #16c784
    glow = (110, 231, 183)

    # Puntos de la linea de tendencia (normalizados 0..1), ligeramente ascendente.
    pts_n = [
        (0.12, 0.70), (0.28, 0.58), (0.40, 0.66),
        (0.55, 0.42), (0.70, 0.50), (0.88, 0.24),
    ]
    pts = [(x * size, y * size) for (x, y) in pts_n]
    line_w = size * 0.055
    end = pts[-1]

    data = bytearray()
    for y in range(size):
        data.append(0)  # filtro PNG: None
        ty = y / (size - 1)
        bg = mix(top, bottom, ty)
        for x in range(size):
            r, g, b = bg
            # Vineta suave radial para dar profundidad
            cx, cy = size / 2, size / 2
            rad = math.hypot(x - cx, y - cy) / (size / 2)
            vig = max(0.0, 1.0 - rad * 0.35)
            r, g, b = int(r * (0.7 + 0.3 * vig)), int(g * (0.7 + 0.3 * vig)), int(b * (0.7 + 0.3 * vig))

            # Distancia minima a la polilinea
            dmin = 1e9
            for i in range(len(pts) - 1):
                d = dist_point_segment(x, y, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
                if d < dmin:
                    dmin = d
            half = line_w / 2
            if dmin < half + 1.5:
                a = max(0.0, min(1.0, (half + 1.5 - dmin) / 2.0))
                r = int(lerp(r, green[0], a))
                g = int(lerp(g, green[1], a))
                b = int(lerp(b, green[2], a))

            # Punto luminoso al final de la linea
            dd = math.hypot(x - end[0], y - end[1])
            dotr = size * 0.085
            if dd < dotr:
                a = max(0.0, min(1.0, (dotr - dd) / (dotr * 0.6)))
                r = int(lerp(r, glow[0], a))
                g = int(lerp(g, glow[1], a))
                b = int(lerp(b, glow[2], a))

            data.extend((r, g, b, 255))

    raw = bytes(data)
    compressed = zlib.compress(raw, 9)

    def chunk(typ, payload):
        c = struct.pack(">I", len(payload)) + typ + payload
        crc = zlib.crc32(typ + payload) & 0xffffffff
        return c + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit, RGBA
    png = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    return png


def main():
    for name, size in [("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)]:
        path = os.path.join(HERE, name)
        with open(path, "wb") as f:
            f.write(make_icon(size))
        print("escrito", path)


if __name__ == "__main__":
    main()
