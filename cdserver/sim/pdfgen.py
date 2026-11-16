"""Minimal one-page text PDFs, optionally password-locked (AES-128)."""

from __future__ import annotations

import io


def make_pdf(lines: list[str], password: str | None = None) -> bytes:
    from pypdf import PdfReader, PdfWriter
    esc = [l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for l in lines]
    stream = "BT /F1 11 Tf 50 790 Td 14 TL " + " ".join(f"({l}) Tj T*" for l in esc) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for n, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n{o}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    data = out.getvalue()
    if password:
        w = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
        w.encrypt(password, algorithm="AES-128")
        buf = io.BytesIO()
        w.write(buf)
        data = buf.getvalue()
    return data
