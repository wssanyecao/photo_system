from __future__ import annotations

from html.parser import HTMLParser
import re

import yaml
from fastapi.testclient import TestClient

from photo_gateway.config import load_yaml_text
from photo_gateway.http import create_app


class _LayoutParser(HTMLParser):
    """Capture element ancestry so layout tests check the rendered page structure."""

    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, str | None, set[str]]] = []
        self.elements: dict[str, dict[str, object]] = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = set(values.get("class", "").split())
        element_id = values.get("id")
        if element_id:
            self.elements[element_id] = {
                "classes": classes,
                "ancestor_classes": [entry[2] for entry in self.stack],
                "ancestor_ids": [entry[1] for entry in self.stack if entry[1]],
            }
        if tag not in self._VOID:
            self.stack.append((tag, element_id, classes))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def _render_page(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    cfg = load_yaml_text(yaml.safe_dump({
        "version": 1,
        "auth": {"enabled": False},
        "devices": [{"id": "demo", "name": "演示设备", "enabled": True}],
        "storage": {"root": str(storage)},
    }, sort_keys=False))
    with TestClient(create_app(cfg)) as client:
        response = client.get("/")
    assert response.status_code == 200
    page = _LayoutParser()
    page.feed(response.text)
    return page, response.text


def test_photo_pagination_stays_with_gallery_instead_of_spanning_the_info_panel(tmp_path):
    page, _ = _render_page(tmp_path)

    assert "photo-gallery" in page.elements["pgBar"]["ancestor_classes"][-1]


def test_upload_flow_is_in_normal_layout_and_recent_imports_has_its_own_scroll_region(tmp_path):
    page, html = _render_page(tmp_path)

    assert any("upload-recent" in classes for classes in page.elements["recentImports"]["ancestor_classes"])
    upload_flow_css = re.search(r"#v-upload \.upload-flow\{([^}]*)\}", html)
    assert upload_flow_css and "position:relative" in upload_flow_css.group(1)
    upload_dropzone_css = re.search(r"#v-upload \.upload-dropzone\{([^}]*)\}", html)
    assert upload_dropzone_css and "flex:1" not in upload_dropzone_css.group(1)
    assert "upload-trust" in page.elements["uploadTrust"]["classes"]
    assert 'class="tblwrap recent-wrap"' in html


def test_sync_page_has_separate_prototype_regions_for_gates_months_and_runs(tmp_path):
    page, _ = _render_page(tmp_path)

    assert "sync-gate-panel" in page.elements["syncGates"]["classes"]
    assert "sync-month-panel" in page.elements["syncTable"]["classes"]
    assert "sync-runs-panel" in page.elements["syncBatches"]["classes"]


def test_system_page_uses_a_dashboard_region_for_live_status_cards(tmp_path):
    page, _ = _render_page(tmp_path)

    assert "system-dashboard" in page.elements["sysCards"]["classes"]


def test_upload_processing_and_reports_stay_in_the_upload_card(tmp_path):
    page, _ = _render_page(tmp_path)
    for region in ("Pdup", "Ptx", "Pres"):
        assert any("upload-main" in classes for classes in page.elements[region]["ancestor_classes"]), region
