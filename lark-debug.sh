#!/bin/bash
# Lark 调试面板 开关脚本 (Linux)
# 用法：
#   sudo bash lark-debug.sh on
#   sudo bash lark-debug.sh off
#   sudo bash lark-debug.sh status

set -e
PROJECT_DIR="/opt/asset-management-system"

show_help() {
    echo "用法: sudo bash lark-debug.sh [on|off|status]"
}

[ "$EUID" -ne 0 ] && { echo "请用 sudo 执行"; exit 1; }
cd "$PROJECT_DIR" 2>/dev/null || { echo "项目目录不存在: $PROJECT_DIR"; exit 1; }

HTML="frontend/index.html"
MARKER='HOME_LARK_DEBUG'

case "${1:-status}" in
    on)
        if grep -q "$MARKER" "$HTML" 2>/dev/null; then
            echo "调试面板已开启"
            exit 0
        fi
        cp "$HTML" "$HTML.bak-debug" 2>/dev/null || true
        python3 << 'PYEOF'
import re

with open("frontend/index.html") as f:
    content = f.read()

debug_block = '''
<!-- HOME_LARK_DEBUG -->
<div id="_larkDbg_" style="position:fixed;top:4px;left:4px;right:4px;max-height:40vh;overflow-y:auto;background:#fffbea;border:2px solid #e6a23c;border-radius:6px;padding:8px;z-index:99999;font-size:10px;font-family:monospace;line-height:1.4">
  <b style="color:#e6a23c">🔍 LARK DEBUG</b>&nbsp;
  <button onclick="this.parentElement.remove()" style="border:1px solid #ccc;font-size:10px;padding:0 4px;cursor:pointer;float:right">X</button>
  <pre id="_larkLog_" style="margin:4px 0 0;color:#333"></pre>
</div>
<script>
(function(){
  var l=document.getElementById('_larkLog_');
  function log(s){ l.textContent += '\\n' + s; }
  log('UA: ' + navigator.userAgent.slice(0,90));
  log('URL: ' + location.href.slice(0,90));
  log('has_token: ' + (!!localStorage.getItem("ams_token")));
  log('isLarkSave: ' + (navigator.userAgent.toLowerCase().indexOf("lark")>-1||navigator.userAgent.toLowerCase().indexOf("feishu")>-1));
  // 延迟 2 秒后补充
  setTimeout(function(){
    log('2s_later_authLoading: ' + (window.__vue__&&window.__vue__.authLoading));
    log('2s_later_isLoggedIn: ' + (window.__vue__&&window.__vue__.isLoggedIn));
  }, 2000);
})();
</script>
'''

content = re.sub(r'(<body[^>]*>)', r'\1\n' + debug_block, content, count=1)

with open("frontend/index.html", "w") as f:
    f.write(content)
print("Done")
PYEOF
        docker-compose down 2>/dev/null; docker-compose build --no-cache backend 2>&1 | tail -2; docker-compose up -d 2>&1 | tail -1
        echo "✅ 调试面板已开启"
        ;;

    off)
        if ! grep -q "$MARKER" "$HTML" 2>/dev/null; then
            echo "调试面板已关闭"; exit 0
        fi
        if [ -f "$HTML.bak-debug" ]; then
            cp "$HTML.bak-debug" "$HTML"
            echo "✅ 已恢复备份"
        else
            python3 -c "
import re
with open('$HTML') as f: c = f.read()
c = c[:c.find('<!-- HOME_LARK_DEBUG')] + c[c.find('</script>', c.find('<!-- HOME_LARK_DEBUG'))+9:]
with open('$HTML','w') as f: f.write(c)
print('Done')
"
        fi
        docker-compose down 2>/dev/null; docker-compose build --no-cache backend 2>&1 | tail -2; docker-compose up -d 2>&1 | tail -1
        echo "✅ 调试面板已关闭"
        ;;

    status)
        grep -q "$MARKER" "$HTML" 2>/dev/null && echo "状态: 已开启" || echo "状态: 已关闭"
        ;;

    *)
        show_help; exit 1
        ;;
esac
