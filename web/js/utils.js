/* ============================================================
   BiliMix — 工具函数模块
   ============================================================ */

function formatTime(seconds) {
    if (seconds == null || isNaN(seconds)) return '--:--';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return m.toString().padStart(2, '0') + ':' + s.toString().padStart(2, '0');
}

function escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function escapeAttr(str) {
    return (str || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

function shakeElement(el) {
    el.style.animation = 'none';
    el.offsetHeight;
    el.style.animation = 'shake 0.4s ease';
    setTimeout(() => { el.style.animation = ''; }, 400);
}

// Inject shake animation
(function() {
    const style = document.createElement('style');
    style.textContent = `
        @keyframes shake {
            0%, 100% { transform: translateX(0); }
            25% { transform: translateX(-6px); }
            75% { transform: translateX(6px); }
        }
    `;
    document.head.appendChild(style);
})();

function showToast(message) {
    let toast = document.getElementById('settings-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'settings-toast';
        toast.className = 'settings-toast';
        document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 2500);
}


// ============================================================
// 登录认证
// ============================================================

/** 退出登录 */
async function doLogout() {
    if (!confirm('确定要退出登录吗？')) return;
    try {
        await fetch('/api/logout', { method: 'POST' });
    } catch(e) {}
    window.location.href = '/login';
}

/** 检查认证状态，显示/隐藏退出按钮 */
async function checkAuthStatus() {
    try {
        const resp = await fetch('/api/auth/check');
        const data = await resp.json();
        const logoutBtn = document.getElementById('logout-btn');
        if (logoutBtn) {
            if (data.auth_enabled && data.authenticated) {
                logoutBtn.style.display = '';
                logoutBtn.title = `退出登录 (${data.username || ''})`;
            } else if (!data.auth_enabled) {
                logoutBtn.style.display = 'none';
            }
        }
    } catch(e) {}
}

// 页面加载时检查认证状态
document.addEventListener('DOMContentLoaded', checkAuthStatus);

/** 全局 fetch 包装：自动处理 401 认证过期 */
const _originalFetch = window.fetch;
window.fetch = async function(...args) {
    const resp = await _originalFetch.apply(this, args);
    if (resp.status === 401) {
        try {
            const data = await resp.clone().json();
            if (data.code === 'AUTH_REQUIRED') {
                window.location.href = '/login';
                return resp;
            }
        } catch(e) {}
    }
    return resp;
};
