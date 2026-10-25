"""Behavioral tests for the parser and real command-line entry point."""

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from svg_structure_inspector import InspectionError, inspect_svg  # noqa: E402

FIXTURES = ROOT / "fixtures"


def document(body: str, attributes: str = "") -> bytes:
    return f'<svg xmlns="http://www.w3.org/2000/svg" {attributes}>{body}</svg>'.encode()


class InventoryTests(unittest.TestCase):
    def fixture(self, name):
        return inspect_svg((FIXTURES / name).read_bytes())

    def test_non_path_shapes_and_text_are_reported(self):
        report = self.fixture("shapes-only.svg")
        self.assertEqual(report["svg_element_counts"], {"svg": 1, "rect": 1, "circle": 1, "text": 1})
        self.assertEqual(report["image_references"], [])
        self.assertEqual(report["inspection"], "structure_only")
        self.assertIn("appearance_not_evaluated", report["unresolved"])

    def test_namespaces_comments_and_builtin_escapes(self):
        report = self.fixture("namespaced.svg")
        self.assertEqual(report["svg_element_counts"], {"svg": 1, "path": 1, "text": 1})
        self.assertEqual(report["non_svg_elements"], 1)
        self.assertEqual(report["image_references"], [])
        self.assertEqual(report["total_elements"], 4)

    def test_namespace_reset_is_not_mistaken_for_svg(self):
        report = inspect_svg(document('<g xmlns=""><path/><image/></g><rect/>'))
        self.assertEqual(report["svg_element_counts"], {"svg": 1, "rect": 1})
        self.assertEqual(report["non_svg_elements"], 3)

    def test_prefixed_image_and_aliased_xlink_namespace(self):
        report = inspect_svg(document(
            '<s:image xmlns:s="http://www.w3.org/2000/svg" '
            'xmlns:link="http://www.w3.org/1999/xlink" link:href="drawing.svg"/>',
        ))
        self.assertEqual(report["svg_element_counts"]["image"], 1)
        self.assertEqual(report["image_references"][0]["attribute"], "xlink:href")
        self.assertEqual(report["image_references"][0]["content_hint"], "svg")

    def test_mixed_content_has_no_visual_verdict(self):
        report = self.fixture("mixed.svg")
        self.assertEqual(report["svg_element_counts"], {"svg": 1, "image": 1, "rect": 1})
        image = report["image_references"][0]
        self.assertEqual((image["scope"], image["content_hint"], image["basis"]), ("data", "raster", "declared_media_type"))
        self.assertIn("embedded_image_contents_not_decoded", report["unresolved"])
        self.assertNotIn("verdict", report)
        self.assertNotIn("safe", report)

    def test_image_reference_hints_are_qualified(self):
        images = self.fixture("image-references.svg")["image_references"]
        self.assertEqual([item["content_hint"] for item in images], ["svg", "svg", "raster", "unknown", "unknown"])
        self.assertEqual([item["scope"] for item in images], ["data", "external", "external", "external", "data"])
        self.assertEqual(images[1]["attribute"], "xlink:href")
        self.assertEqual(images[1]["basis"], "filename_extension")

    def test_media_declaration_does_not_validate_payload(self):
        report = inspect_svg(document('<image href="data:image/png;base64,this-is-not-a-png"/>'))
        self.assertEqual(report["image_references"][0]["basis"], "declared_media_type")
        self.assertIn("embedded_image_contents_not_decoded", report["unresolved"])

    def test_href_takes_precedence_including_empty_value(self):
        report = inspect_svg(document(
            '<image href="" xlink:href="image.png"/><image href="nested.svg" xlink:href="image.png"/>',
            'xmlns:xlink="http://www.w3.org/1999/xlink"',
        ))
        images = report["image_references"]
        self.assertEqual(images[0]["scope"], "missing")
        self.assertEqual(images[1]["content_hint"], "svg")
        self.assertTrue(all(item["attribute"] == "href" for item in images))
        self.assertIn("conflicting_href_attributes", report["unresolved"])

    def test_unknown_and_opaque_references_do_not_become_raster(self):
        for value in ["", "#picture", "blob:example-image.png", "data:image/png", "https://[invalid", "data:application/x-custom,abc"]:
            with self.subTest(reference=value):
                item = inspect_svg(document(f'<image href="{value}"/>'))["image_references"][0]
                self.assertEqual(item["content_hint"], "unknown")

    def test_local_and_external_use_targets(self):
        report = self.fixture("reuse.svg")
        uses = report["use_references"]
        self.assertEqual([item["scope"] for item in uses], ["local_fragment", "local_fragment", "external"])
        self.assertEqual([item["target_status"] for item in uses], ["id_present", "missing_id", "not_loaded"])
        self.assertIn("use_targets_not_expanded", report["unresolved"])
        self.assertEqual(report["svg_element_counts"]["path"], 1)

    def test_forward_percent_encoded_fragment_and_cycle_are_not_expanded(self):
        report = inspect_svg(document('<use href="#sh%61pe"/><g id="shape"><use href="#shape"/></g>'))
        self.assertEqual([item["target_status"] for item in report["use_references"]], ["id_present", "id_present"])
        self.assertEqual(report["svg_element_counts"]["use"], 2)
        self.assertIn("use_targets_not_expanded", report["unresolved"])

    def test_duplicate_foreign_and_missing_ids(self):
        report = inspect_svg(document(
            '<rect id="a"/><circle id="a"/><other:item xmlns:other="urn:example" id="b"/>'
            '<use href="#a"/><use href="#b"/><use/>',
        ))
        self.assertEqual(report["duplicate_id_count"], 1)
        self.assertEqual([item["target_status"] for item in report["use_references"]], ["ambiguous_id", "foreign_namespace_id", "missing_reference"])

    def test_scripts_styles_xml_base_and_filter_images_remain_unresolved(self):
        report = self.fixture("unresolved-features.svg")
        self.assertEqual(report["signals"]["script_elements"], 1)
        self.assertEqual(report["signals"]["event_attributes"], 1)
        self.assertEqual(report["signals"]["processing_instructions"], 1)
        self.assertEqual(report["image_references"][0]["element"], "feImage")
        self.assertEqual(report["use_references"][0]["target_status"], "not_checked_with_xml_base")
        self.assertTrue({"styles_not_evaluated", "stylesheets_not_loaded", "scripts_not_executed", "foreign_content_not_inspected"} <= set(report["unresolved"]))

    def test_document_content_does_not_open_files_network_or_processes(self):
        data = (FIXTURES / "unresolved-features.svg").read_bytes()
        inspect_svg(document(""))  # Load standard-library codecs before applying guards.
        with ExitStack() as stack:
            guards = [stack.enter_context(patch(name, side_effect=AssertionError(name))) for name in [
                "builtins.open", "socket.socket", "socket.create_connection",
                "urllib.request.urlopen", "subprocess.Popen",
            ]]
            report = inspect_svg(data)
            for guard in guards:
                guard.assert_not_called()
        self.assertIn("external_resources_not_loaded", report["unresolved"])

    def test_report_does_not_echo_reference_payload_ids_or_paths(self):
        secret = "example-private-identifier"
        report = inspect_svg(document(f'<rect id="{secret}"/><use href="#{secret}"/><image href="file:///private/{secret}.png?token={secret}"/>'))
        self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn("file:///private", json.dumps(report))

    def test_utf8_bom_and_numeric_escapes(self):
        data = b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>' + document('<text>&#x41; &amp; B</text>')
        self.assertEqual(inspect_svg(data)["svg_element_counts"]["text"], 1)


class RejectionTests(unittest.TestCase):
    def reject(self, data, code):
        with self.assertRaises(InspectionError) as caught:
            inspect_svg(data)
        self.assertEqual(caught.exception.error["code"], code)

    def test_malformed_and_non_svg(self):
        self.reject((FIXTURES / "malformed.svg").read_bytes(), "malformed_xml")
        for data in [b'<html/>', b'<svg/>', b'<svg xmlns="urn:not-svg"/>']:
            with self.subTest(data=data):
                self.reject(data, "not_svg")
        for data in [b'', document('<path>'), document('<text>&undeclared;</text>')]:
            with self.subTest(data=data):
                self.reject(data, "malformed_xml")

    def test_dtd_and_entity_declarations_rejected_before_resolution(self):
        samples = [
            (FIXTURES / "dtd-rejected.svg").read_bytes(),
            b'<!DOCTYPE svg SYSTEM "https://example.invalid/external.dtd">' + document(''),
            b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///must-not-read">]>' + document('<text>&x;</text>'),
            b'<!DOCTYPE svg [<!ENTITY % x SYSTEM "https://example.invalid/parameter.dtd">%x;]>' + document(''),
            b'<!DOCTYPE svg [<!ENTITY a "1234567890"><!ENTITY b "&a;&a;&a;&a;">]>' + document('<text>&b;</text>'),
        ]
        for data in samples:
            with self.subTest(sample=data[:60]):
                with patch("builtins.open", side_effect=AssertionError("external read")), patch("socket.socket", side_effect=AssertionError("network")):
                    self.reject(data, "forbidden_declaration")

    def test_invalid_and_conflicting_encoding(self):
        self.reject(b'\xff\xfe\xff', "invalid_utf8")
        self.reject(b'<?xml version="1.0" encoding="UTF-16"?>' + document(''), "unsupported_encoding")

    def test_exact_size_limit_and_one_byte_over(self):
        shell = document('<!-- -->')
        exact = shell.replace(b'<!-- -->', b'<!--' + b' ' * (1_048_576 - len(shell) + 1) + b'-->')
        self.assertEqual(len(exact), 1_048_576)
        self.assertEqual(inspect_svg(exact)["total_elements"], 1)
        self.reject(exact + b' ', "size_limit")

    def test_depth_boundary(self):
        self.assertEqual(inspect_svg(document('<g>' * 255 + '</g>' * 255))["total_elements"], 256)
        self.reject(document('<g>' * 256 + '</g>' * 256), "depth_limit")

    def test_element_boundary(self):
        self.assertEqual(inspect_svg(document('<rect/>' * 9_999))["total_elements"], 10_000)
        self.reject(document('<rect/>' * 10_000), "element_limit")


class CliTests(unittest.TestCase):
    def run_cli(self, argument, data=None):
        return subprocess.run(
            [sys.executable, str(ROOT / "svg_structure_inspector.py"), argument],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )

    def test_documented_file_journeys_leave_inputs_unchanged(self):
        for name in ["shapes-only.svg", "namespaced.svg", "mixed.svg", "image-references.svg", "reuse.svg", "unresolved-features.svg"]:
            with self.subTest(fixture=name):
                path = FIXTURES / name
                before = hashlib.sha256(path.read_bytes()).hexdigest()
                result = self.run_cli(str(path))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b'')
                self.assertEqual(json.loads(result.stdout)["inspection"], "structure_only")
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_stdin_success_and_rejections(self):
        for data, exit_code, error in [
            (document('<rect/>'), 0, None),
            (b'<svg', 2, 'malformed_xml'),
            (b'\xff', 2, 'invalid_utf8'),
            ((FIXTURES / 'dtd-rejected.svg').read_bytes(), 2, 'forbidden_declaration'),
            (b'x' * 1_048_600, 2, 'size_limit'),
        ]:
            with self.subTest(error=error):
                result = self.run_cli('-', data)
                self.assertEqual(result.returncode, exit_code)
                self.assertEqual(result.stderr, b'')
                output = json.loads(result.stdout)
                if error:
                    self.assertEqual(output['error']['code'], error)
                else:
                    self.assertEqual(output['svg_element_counts']['rect'], 1)

    def test_unreadable_file_is_clear_without_exposing_path(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / 'private-filename.svg')
            result = self.run_cli(missing)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)['error']['code'], 'read_error')
            self.assertNotIn(missing.encode(), result.stdout + result.stderr)
            self.assertNotIn(b'Traceback', result.stdout + result.stderr)

    def test_help_and_version(self):
        self.assertEqual(self.run_cli('--help').returncode, 0)
        self.assertEqual(self.run_cli('--version').stdout.strip(), b'0.1.0')


if __name__ == '__main__':
    unittest.main()
