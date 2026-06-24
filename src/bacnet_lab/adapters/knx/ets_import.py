"""Standalone ETS group-address import parser for the KNX adapter.

This module parses KNX ETS group-address exports into a uniform list of
:class:`EtsGroupAddress` records. It depends only on the Python standard
library (``csv``, ``xml.etree.ElementTree``, ``zipfile``, ``io``, ``re``,
``logging``).

Supported input formats (dispatched on filename extension):

* ``.csv``  -- ETS "Group Addresses" CSV export. Column layouts vary across
  ETS versions, so the header row is sniffed and matched heuristically.
  Delimiters ``;`` (ETS default), ``,`` and tab are all supported.
* ``.xml``  -- ETS "GroupAddress-Export" XML. Tags are matched by their
  local-name so namespace prefixes do not matter. The ``Address`` attribute
  may be a raw integer KNX address or an already-formatted ``main/middle/sub``
  string.
* ``.knxproj`` -- ETS project archive (a ZIP). Every ``.xml`` member is fed
  through the XML parser and the results are de-duplicated. Encrypted
  (password-protected) archives are rejected with a helpful error.

Datapoint types are normalised to the canonical ``main.sub`` form (for
example ``DPST-5-1`` -> ``5.001``) via :func:`normalize_dpt`.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class EtsGroupAddress:
    name: str          # human name of the group address
    group_address: str  # normalized 3-level "main/middle/sub", e.g. "1/2/3"
    dpt: str           # datapoint type like "5.001"; "" if unknown


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# A "1/2/3" three-level group address.
_GA_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*$")


def _raw_to_ga(addr: int) -> str:
    """Convert a raw 16-bit KNX group address integer to "main/middle/sub"."""
    main = (addr >> 11) & 0x1F
    middle = (addr >> 8) & 0x7
    sub = addr & 0xFF
    return f"{main}/{middle}/{sub}"


def _coerce_ga(raw: str) -> str:
    """Return a normalized "main/middle/sub" string or "" if not parseable.

    Accepts an already-formatted "x/y/z" string or a raw integer (as text).
    """
    if raw is None:
        return ""
    raw = str(raw).strip()
    if not raw:
        return ""
    m = _GA_RE.match(raw)
    if m:
        return f"{int(m.group(1))}/{int(m.group(2))}/{int(m.group(3))}"
    # Maybe a raw integer KNX address.
    try:
        return _raw_to_ga(int(raw))
    except (ValueError, TypeError):
        return ""


_DPT_RE = re.compile(r"(\d+)\s*[.\-]\s*0*(\d+)")
_DPT_MAIN_ONLY_RE = re.compile(r"(\d+)")


def normalize_dpt(raw: str) -> str:
    """Normalize an ETS datapoint-type string to canonical "main.sub" form.

    Examples::

        "DPST-5-1"  -> "5.001"
        "DPT-5"     -> "5"
        "DPST-5"    -> "5"
        "5.001"     -> "5.001"
        "1.001"     -> "1.001"
        ""/garbage  -> ""
    """
    if raw is None:
        return ""
    raw = str(raw).strip()
    if not raw:
        return ""
    # If multiple DPTs are listed (space separated), use the first one.
    raw = raw.split()[0]

    # Try to find "main<sep>sub" where sep is "." or "-".
    m = _DPT_RE.search(raw)
    if m:
        main = int(m.group(1))
        sub = int(m.group(2))
        return f"{main}.{sub:03d}"

    # No subtype -- look for a bare main number (e.g. "DPT-5", "DPST-9").
    m = _DPT_MAIN_ONLY_RE.search(raw)
    if m:
        return str(int(m.group(1)))

    return ""


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #

def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def _sniff_delimiter(header_line: str) -> str:
    """Pick the most likely CSV delimiter from the header line."""
    candidates = {";": header_line.count(";"),
                  "\t": header_line.count("\t"),
                  ",": header_line.count(",")}
    best = max(candidates, key=candidates.get)
    if candidates[best] == 0:
        return ";"  # ETS default
    return best


def parse_ets_csv(content: bytes) -> list[EtsGroupAddress]:
    """Parse an ETS "Group Addresses" CSV export (tolerant of column layout)."""
    text = _decode(content)
    # Strip a possible BOM.
    text = text.lstrip("﻿")
    lines = text.splitlines()
    if not lines:
        return []

    delimiter = _sniff_delimiter(lines[0])
    reader = csv.reader(lines, delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return []

    lowered = [h.strip().lower() for h in header]

    def find(*needles: str) -> int:
        for idx, col in enumerate(lowered):
            for needle in needles:
                if needle in col:
                    return idx
        return -1

    addr_idx = find("address")
    name_idx = find("name")
    dpt_idx = find("datapoint", "dpt", "type")
    main_idx = find("main")
    middle_idx = find("middle")
    sub_idx = find("sub")

    results: list[EtsGroupAddress] = []
    for row in reader:
        try:
            if not row or all(not c.strip() for c in row):
                continue

            def cell(i: int) -> str:
                return row[i].strip() if 0 <= i < len(row) else ""

            ga = ""
            if addr_idx >= 0:
                ga = _coerce_ga(cell(addr_idx))
            if not ga and main_idx >= 0 and middle_idx >= 0 and sub_idx >= 0:
                candidate = "/".join((cell(main_idx), cell(middle_idx), cell(sub_idx)))
                ga = _coerce_ga(candidate)
            if not ga:
                # No parseable numeric address -- skip the row.
                logger.debug("Skipping CSV row without parseable address: %r", row)
                continue

            name = cell(name_idx) if name_idx >= 0 else ""
            dpt = normalize_dpt(cell(dpt_idx)) if dpt_idx >= 0 else ""

            results.append(EtsGroupAddress(name=name, group_address=ga, dpt=dpt))
        except Exception:  # noqa: BLE001 -- never crash on a single bad row
            logger.debug("Skipping malformed CSV row: %r", row, exc_info=True)
            continue

    return results


# --------------------------------------------------------------------------- #
# XML
# --------------------------------------------------------------------------- #

def _local_name(tag: str) -> str:
    """Strip a "{namespace}" prefix from an element tag."""
    if tag and tag[0] == "{":
        return tag.split("}", 1)[1]
    return tag


def _attr(elem: ET.Element, *names: str) -> str:
    """Return the first matching attribute value, ignoring namespace/case."""
    wanted = {n.lower() for n in names}
    for key, value in elem.attrib.items():
        if _local_name(key).lower() in wanted:
            return value
    return ""


def parse_ets_xml(content: bytes) -> list[EtsGroupAddress]:
    """Parse an ETS GroupAddress-Export XML document."""
    results: list[EtsGroupAddress] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        logger.debug("Failed to parse ETS XML", exc_info=True)
        return results

    for elem in root.iter():
        try:
            if _local_name(elem.tag) != "GroupAddress":
                continue
            raw_addr = _attr(elem, "Address")
            ga = _coerce_ga(raw_addr)
            if not ga:
                logger.debug("Skipping GroupAddress without valid Address: %r",
                             elem.attrib)
                continue
            name = _attr(elem, "Name")
            raw_dpt = _attr(elem, "DPTs", "DatapointType", "DPT")
            dpt = normalize_dpt(raw_dpt)
            results.append(EtsGroupAddress(name=name, group_address=ga, dpt=dpt))
        except Exception:  # noqa: BLE001 -- never crash on a single bad element
            logger.debug("Skipping malformed GroupAddress element", exc_info=True)
            continue

    return results


# --------------------------------------------------------------------------- #
# .knxproj (ZIP archive)
# --------------------------------------------------------------------------- #

def parse_knxproj(content: bytes) -> list[EtsGroupAddress]:
    """Parse a .knxproj archive by extracting group addresses from XML members."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid .knxproj archive: {exc}") from exc

    seen: set[str] = set()
    results: list[EtsGroupAddress] = []

    with zf:
        for member in zf.namelist():
            if not member.lower().endswith(".xml"):
                continue
            try:
                data = zf.read(member)
            except RuntimeError as exc:
                # zipfile raises RuntimeError for encrypted entries.
                if "encrypted" in str(exc).lower() or "password" in str(exc).lower():
                    raise ValueError(
                        "Encrypted .knxproj not supported; export Group "
                        "Addresses as CSV/XML instead"
                    ) from exc
                logger.debug("Skipping unreadable member %s: %s", member, exc)
                continue
            except Exception:  # noqa: BLE001
                logger.debug("Skipping unreadable member %s", member, exc_info=True)
                continue

            for ga in parse_ets_xml(data):
                if ga.group_address in seen:
                    continue
                seen.add(ga.group_address)
                results.append(ga)

    return results


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

def parse_ets_file(filename: str, content: bytes) -> list[EtsGroupAddress]:
    """Dispatch to the right parser based on the filename extension."""
    lower = (filename or "").lower()
    if lower.endswith(".csv"):
        return parse_ets_csv(content)
    if lower.endswith(".xml"):
        return parse_ets_xml(content)
    if lower.endswith(".knxproj"):
        return parse_knxproj(content)
    raise ValueError(f"Unsupported ETS file type: {filename}")
