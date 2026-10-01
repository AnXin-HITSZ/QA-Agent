"""图片上传和稳定链接的离线集成测试,不触真实 OSS。"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api.routes import sops
from app.main import app
from app.skills import loader


@pytest.fixture
def image_store(monkeypatch):
    class Store:
        prefix = "sops/"

        def __init__(self):
            self.files = {}
            self.types = {}
            self.signs = 0

        def put_object(self, key, data, *, content_type=None, meta=None):
            self.files[key] = data
            self.types[key] = content_type

        def object_exists(self, key):
            return key in self.files

        def sign_url(self, key):
            self.signs += 1
            return f"https://example.test/{key}?signature={self.signs}"

        def list_all(self):
            return [{"key": key} for key in self.files]

        def get_object(self, key):
            return self.files[key]

    store = Store()
    monkeypatch.setattr(sops.oss, "sops_store", lambda: store)
    yield store
    loader._load.cache_clear()


def image_bytes(fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(out, format=fmt)
    return out.getvalue()


@pytest.mark.parametrize("fmt,ext,mime", [
    ("PNG", "png", "image/png"), ("JPEG", "jpg", "image/jpeg"),
    ("GIF", "gif", "image/gif"), ("WEBP", "webp", "image/webp"),
])
def test_upload_and_fresh_redirect(image_store, fmt, ext, mime):
    client = TestClient(app)
    data = image_bytes(fmt)
    # 实际内容决定类型，不信任文件名和浏览器 MIME。
    response = client.post(f"{sops.router.prefix}/images", files={"file": ("wrong.txt", data, "text/plain")})
    assert response.status_code == 201
    result = response.json()
    assert result["key"].startswith("images/") and result["key"].endswith(f".{ext}")
    assert image_store.files[result["key"]] == data
    assert image_store.types[result["key"]] == mime
    first = client.get(result["url"], follow_redirects=False)
    second = client.get(result["url"], follow_redirects=False)
    assert first.status_code == second.status_code == 307
    assert first.headers["location"] != second.headers["location"]
    assert first.headers["cache-control"] == "no-store"
    # 图片与 Markdown 同前缀时，不能成为 SOP 目录项。
    assert client.get(sops.router.prefix).json() == []
    assert loader.reload() == 0


@pytest.mark.parametrize("data", [b"", b"not an image", b'<svg xmlns="http://www.w3.org/2000/svg"/>', image_bytes("BMP")])
def test_reject_invalid_or_unsupported_image(image_store, data):
    response = TestClient(app).post(f"{sops.router.prefix}/images", files={"file": ("fake.png", data, "image/png")})
    assert response.status_code == 400
    assert not image_store.files


def test_reject_large_image(image_store, monkeypatch):
    monkeypatch.setattr(sops, "_IMAGE_MAX_BYTES", 8)
    response = TestClient(app).post(f"{sops.router.prefix}/images", files={"file": ("large.png", image_bytes())})
    assert response.status_code == 413
    assert not image_store.files


def test_missing_and_invalid_image_id(image_store):
    client = TestClient(app)
    assert client.get(f"{sops.router.prefix}/images/{'a' * 32}.png").status_code == 404
    assert client.get(f"{sops.router.prefix}/images/travel.md").status_code == 400


def test_unconfigured_storage(monkeypatch):
    def unavailable():
        raise RuntimeError("OSS 未配置")
    monkeypatch.setattr(sops.oss, "sops_store", unavailable)
    response = TestClient(app).post(f"{sops.router.prefix}/images", files={"file": ("image.png", image_bytes())})
    assert response.status_code == 503
