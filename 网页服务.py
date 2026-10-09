#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周度更新网页入口。本机默认只听 127.0.0.1:8765。"""
import os

import uvicorn

from webapp.app import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("ZHOUDU_BIND", "127.0.0.1")
    port = int(os.environ.get("ZHOUDU_PORT", "8765"))
    print(f"打开浏览器访问 http://{host}:{port}/")
    uvicorn.run(app, host=host, port=port)
