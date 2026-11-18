"""Regression test for the reported HACS install failure:
'No such file or directory: /config/custom_components/meta_home_link/manifest.json'

HACS extracts a zip_release asset straight into
custom_components/meta_home_link/, so manifest.json must sit at the
zip root. A wrapping meta_home_link/ folder (the v1.0.0/v1.0.1 layout)
lands it one level too deep and Home Assistant cannot find it.
"""
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
INTEGRATION = ROOT / "custom_components" / "meta_home_link"


def test_release_zip_has_manifest_at_root(tmp_path):
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    # The workflow must zip from inside the integration dir, not wrap it.
    assert "cd custom_components/meta_home_link" in workflow

    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert hacs["zip_release"] is True
    archive = tmp_path / hacs["filename"]
    subprocess.run(
        ["zip", "-qr", str(archive), "."],
        cwd=INTEGRATION, check=True,
    )
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert "manifest.json" in names
    assert "meta_home_link/manifest.json" not in names
    assert "__init__.py" in names and "config_flow.py" in names


def test_integration_manifest_where_ha_looks():
    manifest = INTEGRATION / "manifest.json"
    assert manifest.is_file()
    data = json.loads(manifest.read_text())
    assert data["domain"] == "meta_home_link"
    assert data["config_flow"] is True
