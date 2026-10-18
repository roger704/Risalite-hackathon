"""Machine-readable source ledger exports."""

import csv
import io
import re
from urllib.parse import quote

CSV_COLUMNS = [
    "n",
    "evidence_id",
    "title",
    "url",
    "method",
    "tier",
    "confidence",
    "status",
    "quote",
    "retrieved_at",
    "published_at",
]


def _guard_spreadsheet(value: object) -> str:
    text = "" if value is None else str(value)
    risky = text.startswith(("\t", "\r")) or text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@"))
    return "'" + text if risky else text


def sources_csv(bundle) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for entry in bundle.evidence:
        writer.writerow({column: _guard_spreadsheet(entry.get(column)) for column in CSV_COLUMNS})
    return output.getvalue()


def _latex_escape(value: object) -> str:
    text = str(value or "")
    escaped = []
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    for char in text:
        escaped.append(replacements.get(char, char))
    return "".join(escaped)


def _date(value: object) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value or ""))
    return match.group(1) if match else ""


def sources_bibtex(bundle) -> str:
    entries = []
    run_id = str(bundle.run.get("run_id", ""))
    for entry in bundle.evidence:
        if entry.get("status") == "unsupported":
            continue
        evidence_id = str(entry.get("evidence_id", ""))
        key = re.sub(r"[^A-Za-z0-9_-]", "-", f"risalite-{run_id}-{evidence_id}")
        fields = [
            ("title", _latex_escape(entry.get("title", ""))),
            (
                "howpublished",
                r"\url{"
                + quote(str(entry.get("url", "")), safe=":/?#[]@!$&'()*+,;=%")
                .replace("{", "%7B")
                .replace("}", "%7D")
                .replace("\\", "%5C")
                + "}",
            ),
            (
                "url",
                quote(str(entry.get("url", "")), safe=":/?#[]@!$&'()*+,;=%")
                .replace("{", "%7B")
                .replace("}", "%7D")
                .replace("\\", "%5C"),
            ),
        ]
        retrieved = _date(entry.get("retrieved_at"))
        published = str(entry.get("published_at") or "")
        if retrieved:
            fields.append(("urldate", retrieved))
        if re.match(r"^\d{4}", published):
            fields.append(("year", published[:4]))
        note = (
            f"Method: {entry.get('method', '')}; tier: {entry.get('tier', '')}; "
            f"confidence: {entry.get('confidence', '')}; status: {entry.get('status', '')}. "
            f"Quote: ``{entry.get('quote', '')}''"
        )
        fields.append(("note", _latex_escape(note)))
        entries.append(
            "@misc{"
            + key
            + ",\n"
            + ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
            + "\n}"
        )
    return "\n\n".join(entries) + ("\n" if entries else "")
