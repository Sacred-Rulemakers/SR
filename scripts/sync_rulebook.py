import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

DOC_ID = os.environ.get("DOC_ID", "1PJbfJ5XwtrKbiE7gu8aqkdoXbWQZ07YqC4t8gB6kOQk")
KEY_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
FORCE = os.environ.get("FORCE_SYNC", "false").lower() == "true"
DATA_PATH = Path("standard-rulebook/rulebook-data.json")
STATUS_PATH = Path("standard-rulebook/rulebook-sync-status.json")

BOOK_RE = re.compile(r"^BOOK\s+[IVXLCDM]+\s+[—–-]\s+.+$", re.I)
PART_RE = re.compile(r"^PART\s+[IVXLCDM]+\s+[—–-]\s+.+$", re.I)
SECTION_RE = re.compile(r"^Section\s+\d+\.\s+.+$", re.I)
RULE_RE = re.compile(r"^Rule\s+(\d{3,})\.\s+(.+)$", re.I)

def paragraphs(elements):
    for item in elements or []:
        if "paragraph" in item:
            paragraph = item["paragraph"]
            text = "".join(
                part.get("textRun", {}).get("content", "")
                for part in paragraph.get("elements", [])
            )
            for line in text.splitlines():
                value = " ".join(line.replace("\u00a0", " ").split()).strip()
                if value:
                    yield value
        elif "table" in item:
            for row in item["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    yield from paragraphs(cell.get("content", []))
        elif "tableOfContents" in item:
            yield from paragraphs(item["tableOfContents"].get("content", []))

def document_paragraphs(document):
    body = document.get("body", {}).get("content")
    if body:
        yield from paragraphs(body)
    def walk_tabs(tabs):
        for tab in tabs or []:
            tab_body = tab.get("documentTab", {}).get("body", {}).get("content")
            if tab_body:
                yield from paragraphs(tab_body)
            yield from walk_tabs(tab.get("childTabs", []))
    if not body:
        yield from walk_tabs(document.get("tabs", []))

def normalize_part_headings(lines):
    raw = list(lines)
    normalized = []
    index = 0
    while index < len(raw):
        line = raw[index]
        if line.upper() == "PART" and index + 1 < len(raw):
            following = raw[index + 1]
            if re.match(r"^(?:I|II|III|IV|V)\\s+[—–-]\\s*", following, re.I):
                line = "PART " + following
                index += 1

        match = re.match(r"^(PART\\s+[IVXLCDM]+\\s+[—–-])(?:\\s+(.*))?$", line, re.I)
        if match:
            title = (match.group(2) or "").strip()
            if index + 1 < len(raw):
                following = raw[index + 1].strip()
                is_uppercase_continuation = (
                    following
                    and following.upper() == following
                    and re.search(r"[A-Z]", following)
                    and not re.match(r"^(BOOK|PART|SECTION|RULE)\\b", following, re.I)
                )
                if is_uppercase_continuation:
                    # Some headings split a word across adjacent paragraphs
                    # (for example "OBJE" + "CTS AND POSSESSION").
                    title = (title + following).strip() if title else following
                    index += 1
            line = (match.group(1) + " " + title).strip()

        normalized.append(line)
        index += 1
    return normalized

def parse_rulebook(lines):
    books = []
    current_book = current_part = current_section = None
    for line in normalize_part_headings(lines):
        if BOOK_RE.match(line):
            current_book = {"title": line, "parts": []}
            books.append(current_book)
            current_part = current_section = None
        elif PART_RE.match(line):
            if current_book is None:
                continue
            current_part = {"title": line, "sections": []}
            current_book["parts"].append(current_part)
            current_section = None
        elif SECTION_RE.match(line):
            if current_part is None:
                continue
            current_section = {"title": line, "rules": []}
            current_part["sections"].append(current_section)
        else:
            match = RULE_RE.match(line)
            if match and current_section is not None:
                number = int(match.group(1))
                current_section["rules"].append(f"Rule {number:03d}. {match.group(2)}")
    if not books:
        raise ValueError("No BOOK headings were detected. The published Rulebook was not changed.")
    numbers = []
    parts = sections = 0
    for book in books:
        if not book["parts"]:
            raise ValueError(f'{book["title"]} has no PART headings.')
        parts += len(book["parts"])
        for part in book["parts"]:
            if not part["sections"]:
                raise ValueError(f'{part["title"]} has no Section headings.')
            sections += len(part["sections"])
            for section in part["sections"]:
                if not section["rules"]:
                    raise ValueError(f'{section["title"]} has no numbered Rules.')
                numbers.extend(int(re.match(r"Rule (\d+)", rule).group(1)) for rule in section["rules"])
    if len(numbers) < 770:
        raise ValueError(f"Only {len(numbers)} Rules were parsed; expected at least the current 770.")
    expected = list(range(1, len(numbers) + 1))
    if numbers != expected:
        raise ValueError("Rule numbering is not continuous from 001; refusing to publish.")
    if len(set(numbers)) != len(numbers):
        raise ValueError("Duplicate Rule numbers detected; refusing to publish.")
    if len(books) < 15 or parts < 34 or sections < 77:
        raise ValueError("The parsed hierarchy is unexpectedly incomplete; refusing to publish.")
    return books, {"books": len(books), "parts": parts, "sections": sections, "rules": len(numbers)}

def main():
    info = json.loads(KEY_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/documents.readonly"]
    )
    service = build("docs", "v1", credentials=credentials, cache_discovery=False)
    document = service.documents().get(documentId=DOC_ID, includeTabsContent=True).execute()
    lines = list(document_paragraphs(document))
    books, counts = parse_rulebook(lines)

    existing = json.loads(DATA_PATH.read_text(encoding="utf-8")) if DATA_PATH.exists() else None
    if existing == books and not FORCE:
        print("No Rulebook content changes detected; keeping the current publication.")
        return

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(books, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status = {
        "lastSyncedAt": datetime.now(timezone.utc).isoformat(),
        "sourceRevision": document.get("revisionId"),
        "counts": counts,
        "forced": FORCE,
    }
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Validated and prepared {counts['rules']} Rules across {counts['books']} Books, {counts['parts']} Parts and {counts['sections']} Sections.")

if __name__ == "__main__":
    main()
