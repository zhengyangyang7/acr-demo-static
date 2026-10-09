/* auth.js - 独立认证模块 */
(function() {
  'use strict';

  var API = '/api';
  var ADMIN_ROLES = ['超管', '管理员', '普通管理员', '操作员', '财务'];
  var isLocal = location.hostname === 'localhost' || location.hostname === '127.0.0.1' || location.hostname.startsWith('192.168.');

  function checkLark() {
    var ua = (navigator.userAgent || '').toLowerCase();
    return ua.indexOf('lark') > -1 || ua.indexOf('feishu') > -1;
  }

  function notify() {
    // Vue 监听的事件
    document.dispatchEvent(new CustomEvent('ams-auth-ready'));
  }

  // 按角色分流：非管理角色进入用户门户(/my.html)，管理角色留在当前页
  // 注意：判定走 ADMIN_ROLES 白名单，role 可能是 '普通用户' / 'user' / '' 等多种历史取值
  function routeByRole(user) {
    if (!user || !user.role) return;            // 角色未知时不猜，避免误伤
    if (isAdminRole(user.role)) return;         // 管理角色：留在当前页
    if (location.pathname === '/my.html') return; // 已在目标页，绝不自我重定向
    window.location.replace('/my.html');
  }

  function isAdminRole(role) {
    return ADMIN_ROLES.indexOf(role) !== -1;
  }

  // ─────────────────────────────────────────
  // 公共守卫：供所有独立页面（stock/repair/dispose/inventory/fields/orders/approval/consumable_stock）调用
  // - 验证 token 有效性
  // - 普通用户重定向到 /my.html（彻底隔离管理后台）
  // - 未登录跳 /login.html
  // 调用方应在页面 <body> 顶部最早位置同步阻塞调用本函数
  // ─────────────────────────────────────────
  window.__amsAuthGuard__ = async function amsAuthGuard(opts) {
    opts = opts || {};
    var requireAdmin = opts.requireAdmin !== false; // 默认要求管理角色
    var allowedRoles = opts.allowedRoles || null;    // 指定允许的角色白名单（优先于 requireAdmin）

    var inLark = checkLark();
    var token = localStorage.getItem('ams_token');

    // 1. Lark 环境（非本机）：先走 OAuth 流程（auth.js 主逻辑在下方异步执行）
    if (inLark && !isLocal) {
      // 等待 __auth_user__ 就绪（Lark 走 OAuth code 登录后会写 window.__auth_user__）
      var user = await new Promise(function(resolve) {
        var done = false;
        function finish() {
          if (done) return;
          done = true;
          resolve(window.__auth_user__ || null);
        }
        if (window.__auth_user__) return finish();
        document.addEventListener('ams-auth-ready', function once() {
          finish();
        }, { once: true });
        // 兜底超时（避免 OAuth 异常导致页面卡死）
        setTimeout(finish, 5000);
      });
      if (!user) {
        window.location.replace('/login.html?lark_err=' + encodeURIComponent('飞书登录失败，请从工作台重新打开本系统'));
        return false;
      }
      return checkRoleAndRedirect(user, requireAdmin, allowedRoles);
    }

    // 2. 非 Lark / 本机：直接调 /api/auth/check 验证 token
    if (!token) {
      window.location.replace('/login.html');
      return false;
    }
    try {
      var res = await fetch(API + '/auth/check', {
        headers: { 'Authorization': 'Bearer ' + token }
      });
      var data = await res.json();
      if (!data.success || !data.is_logged_in) {
        localStorage.removeItem('ams_token');
        localStorage.removeItem('ams_user');
        window.location.replace('/login.html');
        return false;
      }
      window.__auth_passed__ = true;
      window.__auth_user__ = data.user || null;
      return checkRoleAndRedirect(data.user, requireAdmin, allowedRoles);
    } catch (e) {
      localStorage.removeItem('ams_token');
      localStorage.removeItem('ams_user');
      window.location.replace('/login.html');
      return false;
    }
  };

  function checkRoleAndRedirect(user, requireAdmin, allowedRoles) {
    if (!user) {
      window.location.replace('/login.html');
      return false;
    }
    var role = user.role || '';
    if (allowedRoles && allowedRoles.length) {
      if (allowedRoles.indexOf(role) === -1) {
        redirectByRole(role);
        return false;
      }
      return true;
    }
    if (requireAdmin && !isAdminRole(role)) {
      // 非管理角色一律强制进入个人门户（包含普通用户 / 未配置角色）
      redirectByRole(role);
      return false;
    }
    return true;
  }

  function redirectByRole(role) {
    var dest = isAdminRole(role) ? '/main.html' : '/my.html';
    // 已在目标页则直接放行，杜绝自我重定向导致的闪屏/死循环
    if (location.pathname === dest) return;
    window.location.replace(dest);
  }

  async function fetchLarkAppId() {
    try {
      var r = await fetch(API + '/auth/lark-signature?url=' + encodeURIComponent(location.href));
      var d = await r.json();
      if (d && d.success && d.appId) return d.appId;
      var err = (d && (d.error || d.message)) ? (d.error || d.message) : '未配置飞书应用';
      throw new Error(err);
    } catch (e) {
      throw e;
    }
  }

  async function main() {
    var inLark = checkLark();
    
    // 1. OAuth 回调
    var urlParams = new URLSearchParams(location.search);
    var code = urlParams.get('code');
    var state = urlParams.get('state');
    if (code && inLark) {
      // 安全加固（三轮审计#5）：校验 OAuth state 防登录CSRF
      // （无 state 校验时，攻击者可诱导受害者浏览器用攻击者的 code 完成绑定/登录）
      // 修复（四轮N2）：先探测 sessionStorage 是否可用——隐私模式/部分 webview 限制下
      // 读写会抛错或静默失败，此时 _savedState 恒为 null，会把所有正常用户误拒在登录页。
      // 存储不可用时降级放行（state 本来也存不进去，校验无从谈起）并记日志；存储可用则严格校验。
      var _storageOK = false;
      try {
        sessionStorage.setItem('__ams_probe__', '1');
        _storageOK = sessionStorage.getItem('__ams_probe__') === '1';
        sessionStorage.removeItem('__ams_probe__');
      } catch(e) { _storageOK = false; }
      if (!_storageOK) {
        console.warn('[auth] sessionStorage 不可用，跳过 OAuth state 校验（降级放行）');
      } else {
        var _savedState = null;
        try { _savedState = sessionStorage.getItem('lark_oauth_state'); } catch(e) {}
        if (!_savedState || state !== _savedState) {
          window.location.href = '/login.html?lark_err=' + encodeURIComponent('登录状态校验失败，请重新登录');
          return;
        }
        try { sessionStorage.removeItem('lark_oauth_state'); } catch(e) {}
      }
      try {
        var r1 = await fetch(API + '/auth/lark-code-login', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code: code, redirect_uri: location.origin })
        });
        var d1 = await r1.json();
        if (d1.success) {
          localStorage.setItem('ams_token', d1.token);
          localStorage.setItem('ams_user', JSON.stringify(d1.user));
          window.__auth_passed__ = true;
          window.__auth_user__ = d1.user;
          history.replaceState(null, '', '/');
          notify();
          routeByRole(d1.user);
          return;
        }
        // OAuth code 换 token 失败：把飞书返回的错误带回登录页展示，避免无限跳飞书
        var errMsg = (d1 && (d1.error || d1.message)) ? (d1.error || d1.message) : '飞书授权失败';
        // 待绑定申请：给出明确提示
        if (d1 && d1.pending) {
          errMsg = d1.message || '绑定申请已提交，等待管理员审核';
        }
        window.location.href = '/login.html?lark_err=' + encodeURIComponent(errMsg);
        return;
      } catch (e) {
        window.location.href = '/login.html?lark_err=' + encodeURIComponent('飞书授权请求失败，请重试');
        return;
      }
    }

    // 2. 检查本地 token
    var token = localStorage.getItem('ams_token');
    if (token) {
      try {
        var r2 = await fetch(API + '/auth/check', {
          headers: { 'Authorization': 'Bearer ' + token }
        });
        var d2 = await r2.json();
        if (d2.success && d2.is_logged_in) {
          window.__auth_passed__ = true;
          window.__auth_user__ = d2.user;
          // 同步刷新用户缓存，覆盖历史遗留的 role 脏值（避免同步守卫读旧角色误判）
          try { localStorage.setItem('ams_user', JSON.stringify(d2.user)); } catch (e) {}
          notify();
          routeByRole(d2.user);
          return;
        }
      } catch (e) {}
      localStorage.removeItem('ams_token');
      localStorage.removeItem('ams_user');
    }

    // 3. 无有效登录
    if (inLark && !isLocal) {
      // 安全加固（三轮审计#5）：OAuth 跳转生成随机 state 并随授权请求携带，回调时校验（防登录CSRF）
      // 修复（四轮N6）：state 改用加密级随机（crypto.getRandomValues），Math.random 可预测。
      var _state;
      if (window.crypto && window.crypto.getRandomValues) {
        var _buf = new Uint8Array(16);
        window.crypto.getRandomValues(_buf);
        _state = Array.prototype.map.call(_buf, function(b){ return b.toString(16).padStart(2, '0'); }).join('');
      } else {
        // 极老环境兜底：crypto 不可用时退回 Math.random（仍比无 state 强）
        _state = Math.random().toString(36).slice(2) + Date.now().toString(36);
      }
      try { sessionStorage.setItem('lark_oauth_state', _state); } catch(e) {}
      var appId;
      try {
        appId = await fetchLarkAppId();
      } catch (e) {
        var msg = (e && e.message) ? e.message : '未配置飞书应用，请联系管理员';
        window.location.href = '/login.html?lark_err=' + encodeURIComponent(msg);
        return;
      }
      window.location.href = 'https://accounts.larksuite.com/open-apis/authen/v1/authorize'
        + '?app_id=' + encodeURIComponent(appId)
        + '&redirect_uri=' + encodeURIComponent(location.origin)
        + '&state=' + encodeURIComponent(_state);
      return;
    }

    // 非 Lark → 密码登录
    window.location.href = '/login.html';
  }

  // 设置默认状态（让 Vue 显示加载中）
  window.__auth_passed__ = false;
  window.__auth_user__ = null;

  // 仅在 Lark 环境中执行认证流程
  // 非 Lark 环境（含本地）由 main.html 的独立认证逻辑处理
  if (!checkLark()) {
    return;
  }

  // 飞书内：走 OAuth code 免登（本机 localhost 仍走密码，由 main 内 isLocal 分支处理）
  main().catch(function() {
    // 出错则跳登录页
    window.location.href = '/login.html';
  });
})();
