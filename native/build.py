"""Build the pooled HTTPX/OpenSSL transport."""

import os
import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent


def build():
    go = os.environ.get("AGY_GO") or shutil.which("go")
    if not go:
        raise RuntimeError("请安装 Go 1.27.4，或设置 AGY_GO 指向它的 go 可执行文件")
    environment = dict(os.environ, GOTOOLCHAIN="local")
    output = ROOT / "bin" / ("agy-http.exe" if os.name == "nt" else "agy-http")
    output.parent.mkdir(exist_ok=True)
    subprocess.run([go, "build", "-o", str(output), "."],
                   cwd=ROOT, env=environment, check=True)
    return output


if __name__ == "__main__":
    print(build())
