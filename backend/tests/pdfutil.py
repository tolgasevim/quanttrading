"""Build a tiny text-only PDF, so statement extraction is tested end to end without real files."""


def make_text_pdf(lines: list[tuple[float, float, str]]) -> bytes:
    """`lines` are (x, y, text) in points. Returns a one-page PDF with Helvetica 9 pt text."""

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    stream = (
        "BT\n/F1 9 Tf\n"
        + "".join(f"1 0 0 1 {x} {y} Tm ({esc(t)}) Tj\n" for x, y, t in lines)
        + "ET\n"
    )
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream.encode('cp1252'))} >>\nstream\n{stream}endstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("cp1252")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out


# Column x positions (points) mirroring the real statement's layout.
COLUMNS = {"qty": 40, "name": 130, "price": 250, "cost": 340, "pnl": 420, "value": 520}


def crypto_statement_pdf(
    rows: list[tuple[str, str, str, str, str, str]],
    count: int | None = None,
    total: str = "0,00",
    holder: bool = True,
) -> bytes:
    """rows: (quantity, name, price, cost, pnl, value) as printed, e.g. ('0,093152', 'Bitcoin', ...)."""
    y = 560.0
    lines: list[tuple[float, float, str]] = []
    if holder:
        lines += [
            (40, y, "TRADE REPUBLIC BANK GMBH   BRUNNENSTRASSE 19-21   10119 BERLIN"),
            (40, y - 40, "MAX MUSTERMANN"),
            (40, y - 52, "Musterweg 12"),
            (40, y - 64, "12345 Musterstadt"),
            (300, y - 52, "Aufstellung ueber die Cryptos in deinem Depot 0123456789"),
        ]
    lines.append((520, y - 90, "CRYPTO-ÜBERSICHT"))
    lines.append((520, y - 102, "zum 27.09.2026"))
    y -= 140
    header = {
        "qty": "NOMINALE",
        "name": "INSTRUMENT NAME",
        "price": "PREIS JE ANTEIL",
        "cost": "KAUFWERT IN EUR",
        "pnl": "GEWINN / VERLUST",
        "value": "KURSWERT IN EUR",
    }
    lines += [(COLUMNS[k], y, v) for k, v in header.items()]
    y -= 20
    for qty, name, price, cost, pnl, value in rows:
        lines += [
            (COLUMNS["qty"], y, f"{qty} Stk."),
            (COLUMNS["name"], y, f"{name} ({name})"),
            (COLUMNS["price"], y, price),
            (COLUMNS["cost"], y, cost),
            (COLUMNS["pnl"], y, pnl),
            (COLUMNS["value"], y, value),
        ]
        lines.append((COLUMNS["price"], y - 10, "27.09.2026"))
        y -= 30
    footer = f"ANZAHL DER POSITIONEN: {len(rows) if count is None else count}"
    lines.append((130, y, footer))
    lines.append((420, y, f"SUMME KURSWERTE: {total} €"))
    return make_text_pdf(lines)
