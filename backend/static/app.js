/* ═══════════════════════════════════════════
   智能客服 — 全局 UI 组件
   自定义确认弹窗 + Toast 通知
   ═══════════════════════════════════════════ */

(function() {
    'use strict';

    // ── Toast 通知 ──
    function showToast(message, type) {
        var container = document.getElementById('toast-container');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toast-container';
            container.className = 'toast-container';
            document.body.appendChild(container);
        }
        var toast = document.createElement('div');
        toast.className = 'toast' + (type === 'error' ? ' error' : '');
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(function() {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 3000);
    }

    // ── 自定义确认弹窗 ──
    function showConfirm(title, desc, onConfirm) {
        var overlay = document.createElement('div');
        overlay.className = 'modal-overlay show';
        overlay.innerHTML =
            '<div class="modal-box">' +
                '<div class="modal-icon">⚠️</div>' +
                '<div class="modal-title">' + escHtml(title) + '</div>' +
                '<div class="modal-desc">' + escHtml(desc) + '</div>' +
                '<div class="modal-actions">' +
                    '<button class="btn btn-soft" id="modalCancel">取消</button>' +
                    '<button class="btn" id="modalConfirm" style="background:#111;color:#fff;">确认</button>' +
                '</div>' +
            '</div>';
        document.body.appendChild(overlay);
        document.getElementById('modalCancel').onclick = function() { document.body.removeChild(overlay); };
        document.getElementById('modalConfirm').onclick = function() {
            document.body.removeChild(overlay);
            if (onConfirm) onConfirm();
        };
        overlay.onclick = function(e) { if (e.target === overlay) document.body.removeChild(overlay); };
        return false;
    }

    function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

    // ── 接管所有 onsubmit confirm ──
    document.addEventListener('click', function(e) {
        var target = e.target;
        while (target && target !== document) {
            if (target.tagName === 'FORM' && target.getAttribute('data-confirm')) {
                e.preventDefault();
                var msg = target.getAttribute('data-confirm');
                showConfirm('确认操作', msg, function() {
                    // 临时移除 data-confirm 避免递归
                    target.removeAttribute('data-confirm');
                    target.submit();
                    target.setAttribute('data-confirm', msg);
                });
                return;
            }
            if (target.tagName === 'A' && target.getAttribute('data-confirm')) {
                e.preventDefault();
                var msg2 = target.getAttribute('data-confirm');
                showConfirm('确认操作', msg2, function() { window.location.href = target.href; });
                return;
            }
            target = target.parentNode;
        }
    });

    // ── 暴露全局 ──
    window.showToast = showToast;
    window.showConfirm = showConfirm;

    // ── 转换 Flask flash 消息为 Toast ──
    var flashEl = document.getElementById('flash-data');
    if (flashEl) {
        try {
            var msgs = JSON.parse(flashEl.textContent);
            msgs.forEach(function(m) { showToast(m); });
        } catch(e) {}
    }
})();
