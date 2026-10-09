/* ams-role.js - 统一角色判定与安全跳转
 *
 * 必须在所有页面 <head> 的最前面引入（早于 auth.js / auth-guard.js / 业务 JS）。
 *
 * 背景：employees.role 字段历史遗留多种取值（超管 / 管理员 / 普通管理员 / 操作员 /
 * 财务 / 普通用户 / user）。此前前端一律用 role === '普通用户' 硬判，
 * 导致 role='user' 的近百名员工绕过全部前端隔离、直接看到管理后台。
 * 现在统一收敛为白名单判定：**不在管理白名单内的一律视为普通用户**。
 *
 * 另一项职责：提供"安全跳转"。跳转前先判断当前是否已在目标页，
 * 已在则直接返回 false 不做任何事 —— 这是修复"闪屏 / 不停自动跳转"的关键，
 * 因为多守卫并存时互相把对方当非法访问而反复重定向，观感就是页面疯狂闪烁。
 */
(function () {
  'use strict';

  var ADMIN_ROLES = ['超管', '管理员', '普通管理员', '操作员', '财务'];

  function isAdminRole(role) {
    return ADMIN_ROLES.indexOf(role || '') !== -1;
  }

  function landingFor(role) {
    return isAdminRole(role) ? '/main.html' : '/my.html';
  }

  /* 管理端（main.html）内按角色的默认子页 —— 采购全流程 B 期部门视角 */
  var FULL_MENU_ROLES = ['超管', '管理员'];
  var ROLE_DEFAULT_PAGE = {
    '操作员': 'assets',
    '普通管理员': 'approval',
    '财务': 'dashboard'
  };
  // 菜单键白名单；未列出的管理角色（超管/管理员）视为全开
  var ROLE_MENU_KEYS = {
    '财务': ['dashboard', 'assets', 'orders'],
    '操作员': [
      'dashboard', 'quick_stock', 'quick_repair',
      'assets', 'stock', 'repair', 'orders', 'inventory'
    ],
    '普通管理员': ['dashboard', 'assets', 'approval', 'inventory']
  };
  var PAGE_TO_MENU_KEY = {
    dashboard: 'dashboard',
    assets: 'assets',
    stock: 'stock',
    repair: 'repair',
    dispose: 'dispose',
    consumables: 'consumables',
    consumable_logs: 'consumable_logs',
    approval: 'approval',
    inventory: 'inventory',
    orders: 'orders',
    fields: 'fields',
    employees: 'employees',
    lark: 'lark',
    settings: 'settings',
    stock_op: 'quick_stock',
    repair_op: 'quick_repair',
    dispose_op: 'quick_dispose'
  };

  function defaultAdminPage(role) {
    return ROLE_DEFAULT_PAGE[role || ''] || 'dashboard';
  }

  function canSeeMenu(role, key) {
    if (!isAdminRole(role)) return false;
    if (FULL_MENU_ROLES.indexOf(role || '') !== -1) return true;
    var allowed = ROLE_MENU_KEYS[role || ''];
    if (!allowed) return true;
    return allowed.indexOf(key) !== -1;
  }

  function canSeeAdminPage(role, pageName) {
    var key = PAGE_TO_MENU_KEY[pageName] || pageName;
    return canSeeMenu(role, key);
  }

  // 归一化路径：/index.html 与 / 视为同一页（nginx 默认首页）
  function normalizePath(p) {
    p = String(p || '');
    var q = p.indexOf('?');
    if (q !== -1) p = p.slice(0, q);
    if (p === '/index.html' || p === '') p = '/';
    return p;
  }

  /* 安全跳转到该角色的落地页。
   * 返回 true 表示已发起跳转，false 表示无需跳转（已在目标页）。
   * 调用方拿到 false 就应当继续正常渲染页面，千万不要再自行 reload。 */
  function goLanding(role, extraQuery) {
    var dest = landingFor(role);
    if (normalizePath(location.pathname) === normalizePath(dest)) {
      return false; // 已在正确的落地页，绝不再跳（防止自我重定向死循环）
    }
    window.location.replace(dest + (extraQuery || ''));
    return true;
  }

  /* 越权拦截：用于保护管理后台页面（main.html / stock.html 等）。
   * 语义与 goLanding 不同 —— 管理角色一律「放行」（停在原页面继续渲染），
   * 只有非管理角色才赶去 /my.html。
   * 返回 true 表示已发起跳转，false 表示放行或已在目标页。 */
  function rejectIfNotAdmin(role) {
    if (isAdminRole(role)) return false;   // 管理角色：放行，绝不弹走
    return goLanding(role);                 // 非管理角色：送去个人门户
  }

  window.AMS_ADMIN_ROLES = ADMIN_ROLES;
  window.amsIsAdmin = isAdminRole;
  window.amsLanding = landingFor;
  window.amsGoLanding = goLanding;
  window.amsRejectIfNotAdmin = rejectIfNotAdmin;
  window.amsDefaultAdminPage = defaultAdminPage;
  window.amsCanSeeMenu = canSeeMenu;
  window.amsCanSeeAdminPage = canSeeAdminPage;
})();

/* ─────────────────────────────────────────────
 * 全局 fetch 自动鉴权（与后端全局接口鉴权配套）
 * 后端 since 2026-09: 除 PUBLIC 白名单外，所有 /api/* 一律强制
 *   Authorization: Bearer <token>，否则 401/403。
 * 历史页面中有大量 fetch 调用未带 token（此前接口无锁故能通）。
 * 若逐点修补易漏且难维护，故统一在此包装：
 *   凡同源 /api/* 请求、且尚未携带 Authorization 头 → 自动补上本地 token。
 * 已有 Authorization（含各页 getAuthHeader()/内联 Bearer）则原样放行，
 * 避免重复注入。跨域绝对地址（http/https）一律不处理。
 *
 * 兼容性：不使用 Headers 实例，全部用普通对象拼装 headers —— 部分 WebView
 * （含 Lark 内置浏览器）对 Headers 实例支持不完整，会导致 token 注入丢失。
 *
 * 诊断能力（线上排障用，默认关闭，零开销）：
 *   URL 加 ?ams_debug=1  → 右下角显示请求日志面板（含每个 /api 请求的状态码）
 *   URL 加 ?no_auth_wrap=1 → 完全禁用本包装器（用于快速判定问题是否由包装引起）
 * ───────────────────────────────────────────── */
(function () {
  'use strict';
  if (!window.fetch) return;
  // 防止脚本被重复引入导致多重包装
  if (window.__amsFetchWrapped__) return;
  window.__amsFetchWrapped__ = true;

  var _fetch = window.fetch;

  var qs = new URLSearchParams(location.search);
  var DEBUG = qs.get('ams_debug') === '1';
  var DISABLED = qs.get('no_auth_wrap') === '1';
  try {
    if (localStorage.getItem('ams_disable_fetch_wrap') === '1') DISABLED = true;
    if (localStorage.getItem('ams_fetch_debug') === '1') DEBUG = true;
  } catch (e) {}

  // 请求日志（环形缓冲，最多 40 条）
  window.__ams_fetch_log = [];
  function log(entry) {
    try {
      window.__ams_fetch_log.push(entry);
      if (window.__ams_fetch_log.length > 40) window.__ams_fetch_log.shift();
    } catch (e) {}
  }

  function apiPathOf(input) {
    var url = null;
    if (typeof input === 'string') {
      url = input;
    } else if (input && typeof input === 'object' && typeof input.url === 'string') {
      url = input.url;   // Request 对象
    }
    if (!url) return null;
    // 仅处理同源相对路径：跳过绝对跨域（如 Lark 外部接口）
    if (url.indexOf('://') !== -1) return null;
    if (url.charAt(0) === '/' && url.indexOf('/api/') === 0) return url;
    return null;
  }

  function hasAuthHeader(headers) {
    if (!headers) return false;
    if (typeof Headers !== 'undefined' && headers instanceof Headers) return headers.has('Authorization');
    if (Array.isArray(headers)) {
      for (var i = 0; i < headers.length; i++) {
        var pair = headers[i];
        if (pair && String(pair[0] || '').toLowerCase() === 'authorization') return true;
      }
      return false;
    }
    for (var k in headers) {
      if (Object.prototype.hasOwnProperty.call(headers, k) && String(k).toLowerCase() === 'authorization') return true;
    }
    return false;
  }

  window.fetch = function (input, init) {
    var path = null;
    var injected = false;
    try {
      if (!DISABLED) {
        path = apiPathOf(input);
        if (path) {
          var token = null;
          try { token = localStorage.getItem('ams_token'); } catch (e) { token = null; }
          if (token) {
            init = init || {};
            var hasAuth = hasAuthHeader(init.headers)
              || (input && input.headers && hasAuthHeader(input.headers));
            if (!hasAuth) {
              // 保守策略：始终使用普通对象，避免某些 WebView/polyfill 对 Headers 实例支持不完整
              var newHeaders = {};
              var oldHeaders = init.headers;
              if (oldHeaders) {
                if (typeof Headers !== 'undefined' && oldHeaders instanceof Headers) {
                  oldHeaders.forEach(function(v, k) { newHeaders[k] = v; });
                } else if (Array.isArray(oldHeaders)) {
                  oldHeaders.forEach(function(p) { if (p && p.length >= 2) newHeaders[p[0]] = p[1]; });
                } else {
                  for (var k in oldHeaders) {
                    if (Object.prototype.hasOwnProperty.call(oldHeaders, k)) {
                      newHeaders[k] = oldHeaders[k];
                    }
                  }
                }
              }
              newHeaders['Authorization'] = 'Bearer ' + token;
              init = Object.assign({}, init, { headers: newHeaders });
              injected = true;
            }
          }
        }
      }
    } catch (e) { /* 包装失败不阻塞原 fetch */ }

    var p;
    try {
      p = _fetch.call(this, input, init);
    } catch (e) {
      if (path && DEBUG) log({ url: path, injected: injected, status: 'THROW:' + e });
      throw e;
    }

    if (path) {
      var rec = { url: path, injected: injected, token: injected ? 'auto' : 'caller', status: '...' };
      log(rec);
      try {
        // 仅为记录状态码而派生一条旁路；不改变调用方拿到的 Promise
        Promise.resolve(p).then(function (resp) {
          rec.status = resp && resp.status;
        }, function (err) {
          rec.status = 'ERR:' + (err && err.message ? err.message : err);
        }).catch(function () {});
      } catch (e) {}
    }
    return p;
  };

  // ── 调试面板：?ams_debug=1 时在右下角显示请求日志 ──
  if (DEBUG) {
    function renderPanel() {
      var el = document.getElementById('ams-debug-panel');
      if (!el) {
        el = document.createElement('div');
        el.id = 'ams-debug-panel';
        el.style.cssText = [
          'position:fixed', 'right:8px', 'bottom:8px', 'z-index:2147483600',
          'max-height:42vh', 'width:min(92vw,420px)', 'overflow:auto',
          'background:rgba(0,0,0,0.86)', 'color:#7CFC98', 'font:11px/1.5 monospace',
          'padding:8px 10px', 'border-radius:8px', 'white-space:pre-wrap',
          'word-break:break-all', 'pointer-events:auto'
        ].join(';');
        (document.body || document.documentElement).appendChild(el);
      }
      var lines = ['AMS DEBUG  wrap=' + (DISABLED ? 'OFF' : 'ON') +
                   '  nav=' + (window.performance && performance.getEntriesByType
                     ? performance.getEntriesByType('navigation').length : '?')];
      var token = '';
      try { token = localStorage.getItem('ams_token') || ''; } catch (e) {}
      lines.push('token=' + (token ? token.slice(0, 10) + '…(' + token.length + ')' : 'NONE'));
      lines.push('path=' + location.pathname);
      lines.push('--- requests ---');
      var lg = window.__ams_fetch_log || [];
      for (var i = 0; i < lg.length; i++) {
        var r = lg[i];
        lines.push(String(r.status) + '  ' + (r.injected ? '[auto]' : '[hdr ]') + '  ' + r.url);
      }
      el.textContent = lines.join('\n');
    }
    function boot() {
      renderPanel();
      setInterval(renderPanel, 700);
    }
    if (document.body) boot();
    else document.addEventListener('DOMContentLoaded', boot, { once: true });
  }
})();
