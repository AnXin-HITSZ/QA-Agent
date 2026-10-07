"""知识库原件下载:普通用户与管理员都可用;签名里带 attachment 与中文原名。

- 接口挂在普通 router(不是 /admin):「普通用户也能取走原件」正是需求本身,用真身份
  (role=user 的桩)钉住这条;
- 签名参数(response-content-disposition / RFC 5987 编码)在 OssStore.sign_url 单测里
  用捕获实参的假 bucket 钉死 —— 跨域下 HTML 的 download 属性会被忽略,「下载而不是
  打开」只能靠响应头;
- 错误口径复用 _dep_503:未配置(RuntimeError)→ 503;key 非法(空 / 以 / 结尾)→ 400。
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rag import oss

API = "/api/v1/knowledge"


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


class _CapturingBucket:
    """捕获 sign_url 实参的假 bucket(不真连 OSS)。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def sign_url(self, method, key, expires, params=None, slash_safe=False) -> str:
        self.calls.append({"method": method, "key": key, "expires": expires,
                           "params": params, "slash_safe": slash_safe})
        return "https://signed.test/url"


# ---- OssStore.sign_url:attachment + RFC 5987 中文文件名 ----

def test_sign_url_encodes_attachment_filename(monkeypatch):
    bucket = _CapturingBucket()
    monkeypatch.setattr(oss, "get_bucket", lambda: bucket)

    url = oss.OssStore("knowledge/").sign_url("d/发票 6100.pdf", expires=300,
                                              filename="发票 6100.pdf")

    assert url == "https://signed.test/url"
    call = bucket.calls[0]
    assert call["key"] == "knowledge/d/发票 6100.pdf"   # 根前缀由 store 内部拼接
    assert call["expires"] == 300 and call["slash_safe"] is True
    disp = call["params"]["response-content-disposition"]
    assert disp == "attachment; filename*=UTF-8''" + quote("发票 6100.pdf", safe="")


def test_sign_url_without_filename_stays_plain(monkeypatch):
    """不给 filename 的既有调用(引用来源预览)行为不变:不签任何 params。"""
    bucket = _CapturingBucket()
    monkeypatch.setattr(oss, "get_bucket", lambda: bucket)

    oss.OssStore("knowledge/").sign_url("a.pdf")

    assert bucket.calls[0]["params"] is None


# ---- 路由契约 ----

def test_download_returns_signed_url(client, kb_env):
    kb_env.kb.put_object("d/发票 6100.pdf", b"x")
    r = client.get(f"{API}/download", params={"key": "d/发票 6100.pdf"})

    assert r.status_code == 200
    url = r.json()["url"]
    assert "expires=300" in url                # 短时效:5 分钟
    assert "disposition=attachment" in url     # 强制下载(假仓库把实参带进串里)
    assert "filename*=发票 6100.pdf" in url    # 原名(含中文)原样传给签名


def test_download_allows_plain_user(client, kb_env, _stub_auth):
    """普通用户(role=user)也能下载 —— 这正是需求:下载不看管理权限。"""
    from tests.conftest import stub_principal

    _stub_auth.principal = stub_principal(role="user")
    kb_env.kb.put_object("a.pdf", b"x")

    assert client.get(f"{API}/download", params={"key": "a.pdf"}).status_code == 200


def test_download_rejects_empty_and_folder_keys(client, kb_env):
    assert client.get(f"{API}/download", params={"key": ""}).status_code == 400
    assert client.get(f"{API}/download", params={"key": "d/"}).status_code == 400


def test_download_503_when_oss_unconfigured(client, monkeypatch):
    def boom():
        raise RuntimeError("未配置 OSS_ENDPOINT:请在 backend/.env 填好阿里云 OSS 连接信息。")

    monkeypatch.setattr(oss, "knowledge_store", boom)
    r = client.get(f"{API}/download", params={"key": "a.pdf"})

    assert r.status_code == 503 and "OSS" in r.json()["detail"]
