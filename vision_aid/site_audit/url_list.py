"""Parse and validate uploaded URL-list files for asynchronous batch audits."""

from __future__ import annotations

import base64
import binascii
import html
import io
import ipaddress
import re
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlparse
from xml.etree import ElementTree

from .crawler import canonicalize_url, validate_public_url


MAX_URLS = 200
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_DOCX_XML_BYTES = 2 * 1024 * 1024
MAX_DOCX_RELS_BYTES = 512 * 1024
MAX_URL_LENGTH = 2_048
SUPPORTED_EXTENSIONS = {".txt", ".docx"}
URL_PATTERN = re.compile(r"https?://[^\s<>\"'“”‘’]+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ".,;:!?)]}"


def safe_upload_name(file_name: str) -> str:
    """Return a short basename suitable for job metadata and reports."""
    name = re.split(r"[\\/]", str(file_name or ""))[-1].strip()
    if not name or len(name) > 255:
        raise ValueError("Select a .txt or .docx file containing URLs")
    return name


def _read_zip_member(
    archive: zipfile.ZipFile,
    member_name: str,
    *,
    max_bytes: int,
    required: bool,
) -> bytes:
    try:
        info = archive.getinfo(member_name)
    except KeyError:
        if required:
            raise ValueError("The uploaded .docx file is missing its document content")
        return b""
    if info.file_size > max_bytes:
        raise ValueError("The uploaded Word document contains too much text")
    return archive.read(info)


def _docx_text(file_bytes: bytes) -> str:
    """Extract visible text and hyperlink targets from a bounded DOCX archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            document_xml = _read_zip_member(
                archive,
                "word/document.xml",
                max_bytes=MAX_DOCX_XML_BYTES,
                required=True,
            )
            relationships_xml = _read_zip_member(
                archive,
                "word/_rels/document.xml.rels",
                max_bytes=MAX_DOCX_RELS_BYTES,
                required=False,
            )
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("The uploaded .docx file could not be read") from exc

    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        raise ValueError("The uploaded .docx file contains invalid document XML") from exc

    paragraphs: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "p":
            continue
        text_parts = [
            child.text or ""
            for child in element.iter()
            if child.tag.rsplit("}", 1)[-1] in {"t", "instrText"}
        ]
        if text_parts:
            paragraphs.append("".join(text_parts))

    relationship_targets: list[str] = []
    if relationships_xml:
        try:
            rels_root = ElementTree.fromstring(relationships_xml)
            for relationship in rels_root.iter():
                target = relationship.attrib.get("Target", "")
                if target.lower().startswith(("http://", "https://")):
                    relationship_targets.append(target)
        except ElementTree.ParseError as exc:
            raise ValueError("The uploaded .docx file contains invalid link data") from exc

    return "\n".join([*paragraphs, *relationship_targets])


def _upload_text(file_name: str, file_bytes: bytes) -> str:
    extension = PurePosixPath(file_name.lower()).suffix
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError("URL lists must be uploaded as a .txt or .docx file")
    if not file_bytes:
        raise ValueError("The uploaded URL-list file is empty")
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("The URL-list file must be 2 MB or smaller")
    if extension == ".docx":
        return _docx_text(file_bytes)
    try:
        return file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return file_bytes.decode("cp1252")


def _normalize_url_syntax(raw_url: str) -> str:
    value = html.unescape(raw_url).strip().rstrip(TRAILING_URL_PUNCTUATION)
    if len(value) > MAX_URL_LENGTH:
        raise ValueError("One of the uploaded URLs is longer than 2,048 characters")
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid URL in uploaded file: {value[:120]}") from exc
    if parsed.username or parsed.password:
        raise ValueError("Uploaded URLs cannot contain embedded credentials")
    normalized = canonicalize_url(value)
    normalized_parsed = urlparse(normalized)
    if (
        normalized_parsed.scheme not in {"http", "https"}
        or not normalized_parsed.hostname
    ):
        raise ValueError(f"Invalid URL in uploaded file: {value[:120]}")
    host = normalized_parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Private, local, or reserved network URLs are not supported")
    try:
        literal_ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise ValueError("Private, local, or reserved network URLs are not supported")
    return normalized


def extract_uploaded_urls(file_name: str, file_bytes: bytes) -> list[str]:
    """Extract unique HTTP(S) URLs in file order without performing any crawl."""
    name = safe_upload_name(file_name)
    text = _upload_text(name, file_bytes)
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_PATTERN.finditer(text):
        normalized = _normalize_url_syntax(match.group(0))
        if normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
        if len(urls) > MAX_URLS:
            raise ValueError("The uploaded file contains more than 200 unique URLs")
    if not urls:
        raise ValueError("No public http:// or https:// URLs were found in the file")
    return urls


def decode_uploaded_urls(file_name: str, encoded_file: str) -> tuple[str, list[str]]:
    """Decode a bounded base64 upload and return its safe name and URL list."""
    name = safe_upload_name(file_name)
    encoded = str(encoded_file or "").strip()
    if not encoded:
        raise ValueError("Select a .txt or .docx file containing URLs")
    max_encoded_length = ((MAX_UPLOAD_BYTES + 2) // 3) * 4
    if len(encoded) > max_encoded_length:
        raise ValueError("The URL-list file must be 2 MB or smaller")
    try:
        file_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("The uploaded URL-list file could not be decoded") from exc
    return name, extract_uploaded_urls(name, file_bytes)


def validate_uploaded_urls(urls: list[str]) -> list[str]:
    """Apply DNS-backed public-network validation to every uploaded URL."""
    if not urls:
        raise ValueError("The uploaded URL list is empty")
    if len(urls) > MAX_URLS:
        raise ValueError("A URL-list audit can contain at most 200 URLs")
    validated: list[str] = []
    validated_origins: set[tuple[str, int | None]] = set()
    for index, url in enumerate(urls, start=1):
        try:
            normalized = _normalize_url_syntax(url)
            parsed = urlparse(normalized)
            origin = (parsed.hostname or "", parsed.port)
            if origin not in validated_origins:
                normalized = validate_public_url(normalized)
                validated_origins.add(origin)
            validated.append(normalized)
        except ValueError as exc:
            raise ValueError(f"URL {index} is not a supported public page: {exc}") from exc
    return validated
