from __future__ import annotations

from html.parser import HTMLParser

import yaml
from fastapi.testclient import TestClient

from photo_gateway.config import load_yaml_text
from photo_gateway.http import create_app


class _PageStructure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.classes: list[set[str]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.classes.append(set(values.get("class", "").split()))

    def handle_data(self, data):
        if data.strip():
            self.text.append(data.strip())


def test_help_page_renders_prototype_flow_and_safe_maintenance_layout(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    cfg = load_yaml_text(yaml.safe_dump({
        "version": 1,
        "auth": {"enabled": False},
        "devices": [{"id": "demo", "name": "演示设备", "enabled": True}],
        "storage": {"root": str(root)},
    }, sort_keys=False))
    with TestClient(create_app(cfg)) as client:
        response = client.get("/")

    assert response.status_code == 200
    page = _PageStructure()
    page.feed(response.text)
    assert sum("help-step-card" in classes for classes in page.classes) == 5
    assert sum("help-command" in classes for classes in page.classes) == 9
    assert sum("help-danger-panel" in classes for classes in page.classes) == 1
    text = " ".join(page.text)
    assert all(label in text for label in (
        "上传照片", "检查重复", "按拍摄月份归档", "自动同步到 NAS", "可选的本地历史清理",
    ))
    assert "原始照片不会被压缩或修改" in text
    assert "本地清理需要谨慎" in text
    assert "常见问题" in text
