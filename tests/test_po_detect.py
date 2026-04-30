"""Tests for c3.po.detect.detect_po."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

from c3.po.detect import detect_po


def test_detect_po_available_with_version():
    with patch("c3.po.detect.shutil.which", return_value="/usr/bin/parallel-orchestra"), \
         patch("c3.po.detect.version", return_value="0.1.1"):
        available, ver, cli_path = detect_po()
    assert available is True
    assert ver == "0.1.1"
    assert cli_path == "/usr/bin/parallel-orchestra"


def test_detect_po_available_without_metadata():
    with patch("c3.po.detect.shutil.which", return_value="/usr/bin/parallel-orchestra"), \
         patch("c3.po.detect.version", side_effect=PackageNotFoundError):
        available, ver, cli_path = detect_po()
    assert available is True
    assert ver is None
    assert cli_path == "/usr/bin/parallel-orchestra"


def test_detect_po_missing_binary_with_version():
    # Binary not on PATH but metadata exists (rare; unusual install setup).
    with patch("c3.po.detect.shutil.which", return_value=None), \
         patch("c3.po.detect.version", return_value="0.1.1"):
        available, ver, cli_path = detect_po()
    assert available is False
    assert ver == "0.1.1"
    assert cli_path is None


def test_detect_po_completely_missing():
    with patch("c3.po.detect.shutil.which", return_value=None), \
         patch("c3.po.detect.version", side_effect=PackageNotFoundError):
        available, ver, cli_path = detect_po()
    assert available is False
    assert ver is None
    assert cli_path is None
