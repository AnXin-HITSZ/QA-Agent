""".doc 老格式转换:LibreOffice headless 转成 .docx 字节(供 extract 复用 docx 抽取)。

为什么不直接转 txt:转成 docx 后接现有的 python-docx 抽取路径,段落 / 表格的切块
语义与 .docx 完全一致;以后要收 .wps / .odt / .rtf 也是同一个入口,只多几个分派项。

约定(与 extract.py 一致):
- 未安装 / 转换失败 / 超时都不抛异常,把原因作为字符串返回,整批摄取不因此中断;
- 转换纯本地,不产生计费;超时按失败记录,不无限等;
- 每次调用独立临时目录 + 独立用户配置目录(UserInstallation):LibreOffice 多实例
  共用配置目录会互相顶掉或直接卡死,这是它最常见的坑;进程内再加一把锁串行化,
  兜住重试 / 多任务线程重叠的情况。

配置:SOFFICE_BIN(见 app/config.py)。留空 = 从 PATH 找 soffice;Windows 本地开发
装了 LibreOffice 但不在 PATH 时,在 .env 里填 soffice.exe 绝对路径。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# 单次转换的最长等待(秒)。LibreOffice 首次启动要几秒,大文件更久;超时按失败记录。
CONVERT_TIMEOUT_SECONDS = 90.0

# 见模块 docstring:串行化转换,避免多个 LibreOffice 实例互相顶掉。
_convert_lock = threading.Lock()


def soffice_bin() -> str:
    """soffice 可执行文件路径:SOFFICE_BIN 优先,其次 PATH;都没有返回空串。"""
    from app.config import get_settings

    configured = (get_settings().soffice_bin or "").strip()
    if configured:
        return configured
    return shutil.which("soffice") or ""


def _brief(raw: bytes) -> str:
    """子进程输出压成一行、截断,给错误信息用(不把整段日志带进任务明细)。"""
    return " ".join((raw or b"").decode("utf-8", errors="replace").split())[:200]


def doc_to_docx(data: bytes) -> tuple[bytes | None, str | None]:
    """把 .doc 字节转成 .docx 字节;成功 (docx, None),失败 (None, 原因),不抛异常。"""
    exe = soffice_bin()
    if not exe:
        return None, (
            "unsupported: .doc 老格式需 LibreOffice 转换,当前机器未安装"
            "(SOFFICE_BIN 未配置且 PATH 里没有 soffice;可另存为 .docx 后上传)"
        )

    with _convert_lock:
        tmp = tempfile.mkdtemp(prefix="qa_doc_")
        try:
            src = Path(tmp) / "src.doc"       # 固定 ASCII 名,避开路径里的非 ASCII 字符
            src.write_bytes(data)
            profile = (Path(tmp) / "profile").as_uri()   # 独立用户配置目录,见模块说明
            cmd = [
                exe,
                "--headless", "--norestore", "--nolockcheck", "--nodefault",
                f"-env:UserInstallation={profile}",
                "--convert-to", "docx",
                "--outdir", tmp,
                str(src),
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=CONVERT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                return None, f"doc 转换失败: LibreOffice 超时(>{CONVERT_TIMEOUT_SECONDS:.0f}s)"
            except OSError as exc:
                return None, f"doc 转换失败: 无法启动 LibreOffice: {exc}"

            out = Path(tmp) / "src.docx"
            if proc.returncode != 0 or not out.is_file():
                detail = _brief(proc.stderr) or _brief(proc.stdout)
                why = f"退出码 {proc.returncode}" + (f": {detail}" if detail else "")
                return None, f"doc 转换失败: LibreOffice {why}"
            docx = out.read_bytes()
            if not docx:
                return None, "doc 转换失败: 转换结果为空"
            return docx, None
        except OSError as exc:                     # 写临时文件 / 读结果失败
            return None, f"doc 转换失败: {exc}"
        finally:
            # 超时被杀时 soffice 子进程可能还没退干净,清不掉不致命(目录在系统临时区)。
            shutil.rmtree(tmp, ignore_errors=True)
