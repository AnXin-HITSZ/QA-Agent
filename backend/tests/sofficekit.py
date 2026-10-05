"""真 LibreOffice 的测试小工具:定位 soffice、跑一次真格式转换。

只给「装了 soffice 才跑」的集成用例用(开发者本机 / ECS);普通测试一律走 conftest 的
_no_real_libreoffice(视为未安装,不起真子进程)。REAL_SOFFICE 在模块导入期(夹具生效前)
取真路径,空的就由对应用例 skip。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.rag import doc_convert

REAL_SOFFICE = doc_convert.soffice_bin()


def convert(src: Path, to_ext: str, outdir: Path, name: str = "p1") -> Path:
    """用真 soffice 把 src 转成 to_ext(参数与 doc_convert 一致),返回输出文件路径。"""
    subprocess.run(
        [
            REAL_SOFFICE, "--headless", "--norestore",
            f"-env:UserInstallation={(outdir / name).as_uri()}",
            "--convert-to", to_ext, "--outdir", str(outdir), str(src),
        ],
        check=True, capture_output=True, timeout=doc_convert.CONVERT_TIMEOUT_SECONDS,
    )
    return outdir / (src.stem + "." + to_ext)
