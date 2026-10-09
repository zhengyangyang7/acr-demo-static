"""
WSGI 入口文件 - 用于 Gunicorn 部署
"""
import os
import sys

# 添加backend目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 不再使用dotenv（Docker中直接使用环境变量）
try:
    from app import app
except Exception as _e:
    # 把 import 阶段的真实报错落盘 + 打到 stderr，避免被 gunicorn 吞掉导致只能看到
    # "Worker failed to boot" 而看不到根因。
    import traceback
    _tb = traceback.format_exc()
    sys.stderr.write("WSGI IMPORT FAILED:\n" + _tb + "\n")
    sys.stderr.flush()
    try:
        with open("/tmp/wsgi_boot_error.log", "w") as _f:
            _f.write(_tb)
    except Exception:
        pass
    raise

if __name__ == "__main__":
    app.run()
