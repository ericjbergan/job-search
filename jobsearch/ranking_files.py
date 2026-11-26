"""Get plain text out of an uploaded resume (Word .docx or .txt), standard library only."""

import io
import re
import zipfile
import xml.etree.ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def resume_text(filename, data):
    name = (filename or "").lower()
    if name.endswith(".txt"):
        for enc in ("utf-8-sig", "cp1252"):
            try:
                return _tidy(data.decode(enc))
            except UnicodeDecodeError:
                continue
        raise ValueError("Couldn't read that text file")
    if name.endswith(".docx"):
        try:
            doc = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))
        except (zipfile.BadZipFile, KeyError, ET.ParseError):
            raise ValueError("That doesn't look like a Word (.docx) file")
        lines = []
        for p in doc.iter(f"{_W}p"):
            parts = []
            for node in p.iter():
                if node.tag == f"{_W}t" and node.text:
                    parts.append(node.text)
                elif node.tag == f"{_W}tab":
                    parts.append("\t")
                elif node.tag in (f"{_W}br", f"{_W}cr"):
                    parts.append("\n")
            lines.append("".join(parts))
        return _tidy("\n".join(lines))
    if name.endswith(".pdf"):
        raise ValueError("PDF resumes can't be read here. Open the PDF, press Ctrl+A then Ctrl+C, "
                         "and paste the text into the resume box instead.")
    raise ValueError("Use a Word (.docx) or text (.txt) file, or paste the resume text")


def _tidy(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
