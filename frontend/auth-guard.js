/* auth-guard.js - 独立页面通用守卫（stock / repair / dispose / inventory /
 *   fields / orders / approval / consumable_stock）
 *
 * 依赖：必须在之前引入 ./ams-role.js
 *
 * 行为：
 *   - 立刻显示全屏遮罩，挡住未授权内容（不闪白屏）
 *   - 用 token 调 /api/auth/check 拿到真实角色（不信任 localStorage 缓存）
 *   - 非管理角色 → 跳 /my.html；未登录 → 跳 /login.html（并记住来路，登录后回跳）
 *   - 校验通过 → 移除遮罩，置 window.__auth_passed__ = true
 *
 * 重要修复（此前版本的两处缺陷）：
 *   1) 旧版在 Lark 环境里盲等 'ams-auth-ready' 事件 6 秒。但独立页面根本没有
 *      加载 auth.js，事件永远不会触发，结果超管也会被踢到登录页再弹回主页，
 *      表现为"闪一下就被弹走"。现在一律直接调 /api/auth/check，不再等事件。
 *   2) 跳转前用 amsGoLanding 判断"是否已在目标页"，杜绝自我重定向死循环。
 */
(function () {
  'use strict';

  var API = '/api';

  function ensureBody() {
    return new Promise(function (resolve) {
      if (document.body) return resolve();
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', resolve, { once: true });
      } else {
        resolve();
      }
    });
  }

  function showMask() {
    if (document.getElementById('ams-auth-mask')) return;
    var mask = document.createElement('div');
    mask.id = 'ams-auth-mask';
    mask.style.cssText = [
      'position:fixed', 'top:0', 'left:0', 'right:0', 'bottom:0',
      'background:#f0f2f5',
      'display:flex', 'align-items:center', 'justify-content:center',
      'z-index:2147483600',
      'font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif',
      'color:#5b6573', 'font-size:14px'
    ].join(';');
    mask.innerHTML = '<div style="text-align:center"><div style="font-size:36px;margin-bottom:12px">🔐</div><div>正在验证身份...</div></div>';
    document.body.appendChild(mask);
  }

  function hideMask() {
    var m = document.getElementById('ams-auth-mask');
    if (m && m.parentNode) m.parentNode.removeChild(m);
  }

  // 未登录时记住来路，登录成功后可回到原页面
  function rememberRedirect() {
    try {
      var p = location.pathname + (location.search || '');
      if (p && p !== '/login.html') {
        sessionStorage.setItem('ams_redirect_after_login', p);
      }
    } catch (e) {}
  }

  function clearTokens() {
    try {
      localStorage.removeItem('ams_token');
      localStorage.removeItem('ams_user');
    } catch (e) {}
  }

  async function guard() {
    await ensureBody();
    showMask();

    var token = '';
    try { token = localStorage.getItem('ams_token') || ''; } catch (e) {}

    if (!token) {
      clearTokens();
      rememberRedirect();
      window.location.replace('/login.html');
      return;
    }

    try {
      var res = await fetch(API + '/auth/check', {
        headers: { 'Authorization': 'Bearer ' + token }
      });
      var data = await res.json();

      if (!data.success || !data.is_logged_in || !data.user) {
        clearTokens();
        rememberRedirect();
        window.location.replace('/login.html');
        return;
      }

      window.__auth_user__ = data.user;
      // 回写缓存，让其它页面的同步守卫读到最新角色
      try { localStorage.setItem('ams_user', JSON.stringify(data.user)); } catch (e) {}

      // 管理角色放行；非管理角色送去 /my.html（已在 /my.html 时 amsRejectIfNotAdmin
      // 返回 false，此时也放行，避免自我重定向）
      var redirected = window.amsRejectIfNotAdmin
        ? window.amsRejectIfNotAdmin(data.user.role)
        : (!window.amsIsAdmin(data.user.role) && location.pathname !== '/my.html'
            ? (window.location.replace('/my.html'), true) : false);
      if (redirected) return;

      window.__auth_passed__ = true;
      hideMask();
    } catch (e) {
      clearTokens();
      rememberRedirect();
      window.location.replace('/login.html');
    }
  }

  guard();
})();
