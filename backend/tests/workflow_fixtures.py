"""Disposable fixtures for the NEXT-A workflow tests: real PDFs with extractable text."""

from __future__ import annotations

import hashlib
from pathlib import Path


def _esc(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(lines: list[str]) -> bytes:
    """A minimal single-page PDF whose text pypdf can extract."""
    ops = "BT /F1 11 Tf 40 760 Td 14 TL " + " ".join(f"({_esc(ln)}) Tj T*" for ln in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(ops)} >>\nstream\n{ops}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


JAVA_BASICS = [
    "Java Basics Course Notes",
    "Java is a statically typed language that runs on the JVM.",
    "A Java class defines fields and methods; every Java program starts in main.",
    "The Java compiler produces bytecode, executed by the Java virtual machine.",
    "Interfaces, generics and collections are core Java features.",
]
OOP_NOTES = [
    "Object oriented programming notes",
    "In Java every class extends Object. The JVM loads each Java class lazily.",
    "Inheritance, polymorphism and encapsulation are shown with Java examples.",
    "Spring Boot applications are Java programs packaged as a single jar.",
]
JAVA_ISLAND = [
    "Travel guide: the island of Java",
    "Java is the most populous island of Indonesia, home to Jakarta.",
    "Visit Yogyakarta and the volcanoes. Java coffee is famous across the island.",
]
JAVASCRIPT = [
    "JavaScript guide",
    "JavaScript runs in the browser and in Node. It is unrelated to Java despite the name.",
    "Use const and let, promises and async functions.",
]
BUDGET = ["Household budget 2026", "Rent 900. Food 400. Travel 150. Savings 300."]
REQUIREMENTS = [
    "Project Requirements Document",
    "R1. The system shall let a user record expenses with an amount and a category.",
    "R2. The system shall report monthly totals per category.",
    "R3. All data shall be stored locally in a single file.",
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_java_workspace(base: Path) -> dict[str, Path]:
    """Nested folders, a duplicate filename, an identical copy, unrelated and ambiguous files."""
    files: dict[str, Path] = {}

    def put(rel: str, data: bytes | str) -> None:
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        files[rel] = p

    put("courses/java-basics.pdf", make_pdf(JAVA_BASICS))
    put("courses/notes/Intro_to_OOP.pdf", make_pdf(OOP_NOTES))  # about Java, name says nothing
    put("archive/old/java-basics.pdf", make_pdf(JAVA_BASICS))  # identical copy, same file name
    put("travel/java-island-guide.pdf", make_pdf(JAVA_ISLAND))  # the island: ambiguous
    put("courses/javascript-guide.pdf", make_pdf(JAVASCRIPT))  # not Java
    put("courses/java-notes-scan.pdf", b"%PDF-1.4 scanned image only")  # name only, unreadable
    put("finance/budget-2026.pdf", make_pdf(BUDGET))  # unrelated
    put("misc/readme.txt", "Notes about Java, Java and more Java.")  # not a PDF
    return files
