#!/usr/bin/env python3
"""Bounded, offline SVG structure inventory. No rendering or resource loading."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import PurePosixPath
import sys
from urllib.parse import unquote, urlsplit
from xml.parsers import expat

VERSION = "0.1.0"
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"
MAX_INPUT_BYTES = 1_048_576
MAX_ELEMENTS = 10_000
MAX_DEPTH = 256
RASTER_MEDIA = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    "image/tiff", "image/avif", "image/x-icon", "image/vnd.microsoft.icon",
}
RASTER_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".ico",
}


class InspectionError(Exception):
    """A rejected input, without echoing document contents or filesystem paths."""

    def __init__(self, code: str, message: str, **details: int):
        super().__init__(message)
        self.error = {"code": code, "message": message, **details}


def _name(expanded: str) -> tuple[str, str]:
    namespace, separator, local = expanded.rpartition("}")
    return (namespace, local) if separator else ("", expanded)


def _reference(attrs: dict[str, str]) -> tuple[str, str, bool]:
    legacy = attrs.get(f"{XLINK_NS}}}href")
    if "href" in attrs:
        return attrs["href"].strip(), "href", legacy is not None and legacy != attrs["href"]
    if legacy is not None:
        return legacy.strip(), "xlink:href", False
    return "", "missing", False


def _image_hint(reference: str) -> dict[str, str]:
    if not reference:
        return {"scope": "missing", "content_hint": "unknown", "basis": "none"}
    if reference.startswith("#"):
        return {"scope": "local_fragment", "content_hint": "unknown", "basis": "none"}
    if reference.lower().startswith("data:"):
        header, comma, _ = reference[5:].partition(",")
        media = header.split(";", 1)[0].strip().lower() if comma else ""
        hint = "svg" if media == "image/svg+xml" else "raster" if media in RASTER_MEDIA else "unknown"
        return {"scope": "data", "content_hint": hint, "basis": "declared_media_type" if comma else "none"}
    try:
        parts = urlsplit(reference)
        if parts.scheme.lower() not in {"", "http", "https", "file"}:
            return {"scope": "external", "content_hint": "unknown", "basis": "none"}
        suffix = PurePosixPath(unquote(parts.path, errors="strict").replace("\\", "/")).suffix.lower()
    except (ValueError, UnicodeError):
        return {"scope": "external", "content_hint": "unknown", "basis": "none"}
    hint = "svg" if suffix in {".svg", ".svgz"} else "raster" if suffix in RASTER_SUFFIXES else "unknown"
    return {"scope": "external", "content_hint": hint, "basis": "filename_extension" if hint != "unknown" else "none"}


class _Inventory:
    def __init__(self):
        self.depth = 0
        self.total = 0
        self.foreign = 0
        self.elements: Counter[str] = Counter()
        self.ids: dict[str, list[str]] = {}
        self.images: list[dict] = []
        self.uses: list[dict] = []
        self.use_fragments: list[str] = []
        self.signals: Counter[str] = Counter()
        self.unresolved = {"appearance_not_evaluated", "hidden_and_definition_elements_included"}
        self.has_xml_base = False

    def start(self, expanded: str, attrs: dict[str, str]) -> None:
        self.total += 1
        self.depth += 1
        if self.total > MAX_ELEMENTS:
            raise InspectionError("element_limit", f"Input exceeds {MAX_ELEMENTS} elements.")
        if self.depth > MAX_DEPTH:
            raise InspectionError("depth_limit", f"Input exceeds {MAX_DEPTH} nesting levels.")
        namespace, tag = _name(expanded)
        if self.total == 1 and (namespace, tag) != (SVG_NS, "svg"):
            raise InspectionError("not_svg", "Root must be svg in the http://www.w3.org/2000/svg namespace.")
        identifier = attrs.get("id")
        if identifier:
            self.ids.setdefault(identifier, []).append(namespace)
        if f"{XML_NS}}}base" in attrs:
            self.has_xml_base = True
            self.unresolved.add("xml_base_not_resolved")
        if namespace != SVG_NS:
            self.foreign += 1
            self.unresolved.add("foreign_content_not_inspected")
            return
        self.elements[tag] += 1
        if tag == "path":
            self.unresolved.add("path_data_not_validated")
        if tag in {"text", "tspan", "textPath"}:
            self.unresolved.add("text_layout_not_evaluated")
        if tag == "style" or "style" in attrs:
            self.unresolved.add("styles_not_evaluated")
        if tag == "script":
            self.signals["script_elements"] += 1
            self.unresolved.add("scripts_not_executed")
        if tag == "foreignObject":
            self.unresolved.add("foreign_content_not_inspected")
        for attribute in attrs:
            if _name(attribute)[0] == "" and attribute.lower().startswith("on"):
                self.signals["event_attributes"] += 1
                self.unresolved.add("event_handlers_not_executed")
        reference, attribute, conflict = _reference(attrs)
        if conflict:
            self.unresolved.add("conflicting_href_attributes")
        if tag in {"image", "feImage"}:
            item = {"element": tag, "index": len(self.images) + 1, "attribute": attribute, **_image_hint(reference)}
            self.images.append(item)
            if item["scope"] == "data":
                self.unresolved.add("embedded_image_contents_not_decoded")
            elif item["scope"] == "external":
                self.unresolved.add("external_resources_not_loaded")
            else:
                self.unresolved.add("image_reference_not_resolved")
            if item["basis"] == "filename_extension":
                self.unresolved.add("image_type_inferred_from_filename")
            if item["content_hint"] == "unknown":
                self.unresolved.add("unknown_image_content")
        elif tag == "use":
            scope = "local_fragment" if reference.startswith("#") else "external" if reference else "missing"
            self.uses.append({"index": len(self.uses) + 1, "attribute": attribute, "scope": scope})
            self.use_fragments.append(reference[1:] if scope == "local_fragment" else "")
            self.unresolved.add("use_targets_not_expanded")
            if scope == "external":
                self.unresolved.add("external_resources_not_loaded")
        elif attribute != "missing":
            self.unresolved.add("other_href_references_not_inspected")

    def end(self, _expanded: str) -> None:
        self.depth -= 1

    def processing_instruction(self, target: str, _data: str) -> None:
        self.signals["processing_instructions"] += 1
        self.unresolved.add("stylesheets_not_loaded" if target == "xml-stylesheet" else "processing_instructions_not_executed")

    def finish(self, input_bytes: int) -> dict:
        duplicates = sum(len(namespaces) > 1 for namespaces in self.ids.values())
        if duplicates:
            self.unresolved.add("duplicate_ids")
        for item, fragment in zip(self.uses, self.use_fragments):
            if item["scope"] == "external":
                status = "not_loaded"
            elif item["scope"] == "missing":
                status = "missing_reference"
            elif self.has_xml_base:
                status = "not_checked_with_xml_base"
            else:
                try:
                    matches = self.ids.get(unquote(fragment, errors="strict"), [])
                except UnicodeError:
                    matches = []
                if not matches:
                    status = "missing_id"
                elif len(matches) > 1:
                    status = "ambiguous_id"
                else:
                    status = "id_present" if matches[0] == SVG_NS else "foreign_namespace_id"
            item["target_status"] = status
            if status != "id_present":
                self.unresolved.add("unresolved_use_reference")
        return {
            "schema_version": 1,
            "inspection": "structure_only",
            "input_bytes": input_bytes,
            "total_elements": self.total,
            "svg_element_counts": dict(sorted(self.elements.items())),
            "non_svg_elements": self.foreign,
            "image_references": self.images,
            "use_references": self.uses,
            "duplicate_id_count": duplicates,
            "signals": dict(sorted(self.signals.items())),
            "unresolved": sorted(self.unresolved),
            "limits": {"input_bytes": MAX_INPUT_BYTES, "elements": MAX_ELEMENTS, "depth": MAX_DEPTH},
        }


def inspect_svg(data: bytes) -> dict:
    """Inventory UTF-8 SVG bytes or raise InspectionError. Never resolve resources."""
    if len(data) > MAX_INPUT_BYTES:
        raise InspectionError("size_limit", f"Input exceeds {MAX_INPUT_BYTES} bytes (1 MiB).")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InspectionError("invalid_utf8", "Input must be UTF-8 text.") from None
    inventory = _Inventory()
    parser = expat.ParserCreate(namespace_separator="}")

    def reject_declaration(*_args):
        raise InspectionError("forbidden_declaration", "DTD and custom entity declarations are not supported.")

    def xml_declaration(_version, encoding, _standalone):
        if encoding and encoding.lower().replace("-", "") != "utf8":
            raise InspectionError("unsupported_encoding", "The XML declaration must specify UTF-8 when an encoding is given.")

    parser.StartElementHandler = inventory.start
    parser.EndElementHandler = inventory.end
    parser.StartDoctypeDeclHandler = reject_declaration
    parser.EntityDeclHandler = reject_declaration
    parser.ExternalEntityRefHandler = reject_declaration
    parser.XmlDeclHandler = xml_declaration
    parser.ProcessingInstructionHandler = inventory.processing_instruction
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(text, True)
    except expat.ExpatError as error:
        raise InspectionError("malformed_xml", "XML could not be parsed.", line=error.lineno, column=error.offset) from None
    return inventory.finish(len(data))


def main(argv: list[str] | None = None) -> int:
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("input", help="SVG file, or - for UTF-8 bytes on stdin")
    arguments.add_argument("--version", action="version", version=VERSION)
    args = arguments.parse_args(argv)
    try:
        if args.input == "-":
            data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        else:
            with open(args.input, "rb") as handle:
                data = handle.read(MAX_INPUT_BYTES + 1)
        result = inspect_svg(data)
        code = 0
    except OSError:
        result = {"error": {"code": "read_error", "message": "Could not read the input file or stdin."}}
        code = 1
    except InspectionError as error:
        result = {"error": error.error}
        code = 2
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
