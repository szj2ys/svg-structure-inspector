# SVG Structure Inspector

Inspect the XML structure of an SVG before deciding what to examine in an editor.
This small Python command reports SVG element counts, image-reference hints and
local/external `use` references as JSON. It runs offline, has no third-party
dependencies, and does not require an account.

It does **not** render, trace, optimize or sanitize SVGs. Counts cannot establish
visual completeness, editability, cutting suitability or security. A rectangle
is vector markup even when there are zero `path` elements; an `image` element can
reference another SVG, and an embedded bitmap can coexist with vector artwork.

## Run

Use a maintained Python release with Python 3.10+ syntax support. Download the
repository files, open a terminal in this directory, and run:

```sh
python svg_structure_inspector.py fixtures/shapes-only.svg
python svg_structure_inspector.py fixtures/mixed.svg
python svg_structure_inspector.py fixtures/reuse.svg
```

On systems where Python is named `python3`, substitute that command. To inspect
your own file, replace the fixture path with a local SVG filename. You can also
pass UTF-8 bytes through stdin:

```sh
python -c "from pathlib import Path; import sys; sys.stdout.buffer.write(Path('fixtures/mixed.svg').read_bytes())" | python svg_structure_inspector.py -
```

To use it from Python, import the adjacent module:

```python
from pathlib import Path
from svg_structure_inspector import inspect_svg

report = inspect_svg(Path("fixtures/shapes-only.svg").read_bytes())
print(report["svg_element_counts"])
# {'circle': 1, 'rect': 1, 'svg': 1, 'text': 1}
```

The public function raises `InspectionError` for rejected input. The CLI converts
that error into JSON. Both accept at most 1 MiB; the CLI reads only the limit plus
one byte, so an oversized file is rejected without reading the rest.

## Read the result

- `svg_element_counts` counts parsed elements in the SVG namespace, including
  content in definitions and hidden groups. An omitted element name has count
  zero. Comments and foreign-namespace lookalikes are not SVG elements.
- `image_references` covers both `image` and filter `feImage`. Each entry has a
  one-based index in document order, the chosen `href`/`xlink:href` attribute,
  scope, `content_hint` and the evidence behind that hint.
- `content_hint` is `raster`, `svg` or `unknown`. A data URI's declared media type
  or an external filename extension is only a **hint**: payload bytes are not
  decoded and files/URLs are not opened. A misleading name or MIME declaration
  can therefore produce a misleading hint. Unknown types remain unknown.
- Image scope is `data`, `local_fragment`, `external` or `missing`. External
  includes relative filenames, absolute URLs and opaque references such as
  `blob:`. No reference text, payload, element IDs or input filename is echoed.
- `use_references` distinguishes fragment syntax (`#id`) from external resources
  and missing references. `id_present` means a unique SVG element has that ID;
  it does not validate the target type, expand reuse or detect reference cycles.
  Missing, duplicate and foreign-namespace targets are reported separately.
  When any `xml:base` occurs, fragment targets stay unresolved conservatively.
- `signals` records script elements, event attributes and processing instructions
  when present. These are indicators, not a security verdict.
- `unresolved` names work the inventory does not perform, such as evaluating
  appearance/styles/path data, expanding reuse, or loading referenced content.

SVG 2 `href` takes precedence when both forms occur, even if it is empty.
Different values also add `conflicting_href_attributes` to `unresolved`.

For a practical visual check, work on a copy in a trusted vector editor: inspect
object types, hide/remove image objects, and compare the rendered artwork.
Element counts and byte savings alone do not answer what supplied its appearance.

## Input boundaries and exit codes

The input must be UTF-8 XML (an optional UTF-8 BOM is accepted) with an `svg` root
in `http://www.w3.org/2000/svg`. An unqualified `<svg>` without that namespace,
HTML, compressed SVGZ, and a declaration naming another encoding are unsupported.
This is not SVG schema validation: malformed path data and unsupported SVG
features may still be inventoried as XML.

Limits are **1,048,576 bytes**, **10,000 elements** across all namespaces, and
**256 nesting levels**, with the root counting as one level/element. DTDs and
custom entity declarations are rejected by parser callbacks before expansion.
Normal XML escapes such as `&amp;` and numeric character references are allowed.
Resources, scripts, stylesheets and external entities are never loaded or run.
Input files are not changed. These boundaries do not turn this into a sanitizer;
keep the Python runtime and its XML parser updated.

| Exit | Meaning |
| --- | --- |
| 0 | An inventory was produced. No visual or security certification is implied. |
| 1 | The input file or stdin could not be read. |
| 2 | Input rejected: malformed XML, unsupported input, forbidden declaration, or resource limit. Incorrect CLI usage also exits 2 with standard usage text on stderr. |

Inventory and input-error JSON go to stdout. Input errors use stable codes such
as `size_limit`, `malformed_xml` and `forbidden_declaration`, without a traceback
or an expanded filesystem path. `--help` and `--version` print plain text.

## Fixtures and tests

The small fixtures are original examples, not downloaded artwork:

| Fixture | What to observe |
| --- | --- |
| `shapes-only.svg` | Shapes and text with no paths or images. |
| `namespaced.svg` | A prefixed SVG, foreign metadata and tag-looking comments. |
| `mixed.svg` | A rectangle and an original one-pixel embedded PNG. |
| `image-references.svg` | Embedded SVG, `xlink:href`, external type hints and unknown content. |
| `reuse.svg` | An existing local ID, a missing ID and an external `use`. |
| `unresolved-features.svg` | CSS, script, `xml:base`, filter image and foreign content indicators. |
| `malformed.svg` | Deliberately invalid XML; expect exit 2. |
| `dtd-rejected.svg` | Deliberately unsupported DTD/entity declaration; expect exit 2. |

```sh
python -m unittest discover -s tests -v
```

Tests cover real parsing, namespace boundaries, reference precedence, malformed
inputs, parser limits, guarded file/network/process access and CLI file/stdin
journeys. Oversized inputs are generated in memory instead of bundled as large
fixtures. The utility is distinct from tracing benchmarks: it accepts arbitrary
SVGs and does not launch a tracer, measure image quality or compare conversions.

## Related workflow

If you need to trace a raster image first, [pic2svg](https://pic2svg.com/) offers
free hosted SVG tracing. That service processes uploads on its server. This
offline inspector does not call it, and this repository's MIT license covers
only the inspector, tests and original fixtures—not the hosted service.

## Sources and license

Reference semantics follow the W3C SVG specification's [image](https://www.w3.org/TR/SVG2/embedded.html#ImageElement)
and [use](https://www.w3.org/TR/SVG2/struct.html#UseElement) sections. The parser is
Python's standard-library Expat binding; see [Python XML guidance](https://docs.python.org/3/library/xml.html).
Original utility, documentation and fixtures are available under the [MIT license](LICENSE).
