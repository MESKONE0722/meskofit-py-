"""QR codes for the terminal and as SVG, so the iPhone can open MeskoFit by pointing its camera at the PC screen."""
from __future__ import annotations

import segno


def _bitmap(text: str, border: int) -> list[list[bool]]:
    """Modules as rows of booleans (True = dark) with a quiet zone of ``border`` modules."""
    q = segno.make_qr(text, error="m", boost_error=False)
    rows = [[bool(v) for v in row] for row in q.matrix]
    n = len(rows) + 2 * border
    pad = [False] * n
    return [pad[:] for _ in range(border)] + [[False] * border + r + [False] * border for r in rows] + [pad[:] for _ in range(border)]


def terminal(text: str) -> str:
    """Draw the code with half-block characters, two QR rows per line. Light modules are printed as
    blocks so it reads correctly on the dark background most terminals use. Raises ValueError (segno's
    DataOverflowError) if the text is too long for a QR code."""
    bm = _bitmap(text, 2)
    out: list[str] = []
    for y in range(0, len(bm), 2):
        line = []
        for x in range(len(bm[y])):
            top = not bm[y][x]
            bottom = True if y + 1 >= len(bm) else not bm[y + 1][x]  # pad the last line with light
            line.append("█" if top and bottom else "▀" if top else "▄" if bottom else " ")
        out.append("".join(line))
    return "\n".join(out) + "\n"


def svg(text: str) -> str:
    """A crisp, scalable QR code image (4-module quiet zone, one path of horizontal runs)."""
    bm = _bitmap(text, 4)
    n = len(bm)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">',
        f'<rect width="{n}" height="{n}" fill="#fff"/><path fill="#000" d="',
    ]
    for y, row in enumerate(bm):
        x = 0
        while x < len(row):
            if not row[x]:
                x += 1
                continue
            start = x
            while x < len(row) and row[x]:
                x += 1
            parts.append(f"M{start} {y}h{x - start}v1h-{x - start}z")
    parts.append('"/></svg>')
    return "".join(parts)
