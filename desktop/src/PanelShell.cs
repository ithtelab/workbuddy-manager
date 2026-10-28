namespace WorkBuddyDesktop;

/// <summary>
/// 注入到面板页面里的更新条与更新进度浮层。通过 ExecuteScriptAsync 注入，
/// 不依赖面板的 React 代码，也不修改 server/web/out。
/// </summary>
internal static class PanelShell
{
    public const string Script = """
(function () {
  if (window.__wbShellUpdate) return;
  if (!document.body) return;

  const style = document.createElement('style');
  style.id = 'wb-shell-style';
  style.textContent = `
  #wb-shell-update{position:fixed;inset:0;z-index:2147483000;pointer-events:none;
    font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei UI","PingFang SC",sans-serif;
    color:#18181b;}
  #wb-shell-update *{box-sizing:border-box}
  #wb-shell-update .wb-banner{position:absolute;right:20px;bottom:20px;width:330px;
    display:flex;gap:12px;align-items:flex-start;padding:14px 16px;border-radius:14px;
    background:#fff;border:1px solid rgba(0,0,0,.09);
    box-shadow:0 18px 50px rgba(0,0,0,.18);pointer-events:auto;
    animation:wb-in .22s ease-out;}
  @keyframes wb-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  #wb-shell-update .wb-icon{width:34px;height:34px;border-radius:10px;flex:0 0 auto;
    background:rgba(245,158,11,.14);color:#d97706;display:grid;place-items:center;font-size:18px;font-weight:700}
  #wb-shell-update .wb-grow{flex:1;min-width:0}
  #wb-shell-update .wb-title{font-size:13px;font-weight:600;line-height:1.4}
  #wb-shell-update .wb-sub{font-size:11.5px;color:#71717a;margin-top:2px;line-height:1.5}
  #wb-shell-update .wb-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
  #wb-shell-update button{appearance:none;border:1px solid rgba(0,0,0,.1);background:transparent;
    color:inherit;border-radius:9px;padding:6px 12px;font-size:12px;cursor:pointer;
    font-family:inherit;transition:.15s;white-space:nowrap}
  #wb-shell-update button:hover:not(:disabled){background:rgba(0,0,0,.05)}
  #wb-shell-update button:disabled{opacity:.45;cursor:default}
  #wb-shell-update button.wb-primary{background:#18181b;color:#fff;border-color:transparent;font-weight:500}
  #wb-shell-update button.wb-primary:hover:not(:disabled){opacity:.88;background:#18181b}
  #wb-shell-update button.wb-danger{color:#dc2626;border-color:rgba(220,38,38,.35)}
  #wb-shell-update .wb-overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    background:rgba(0,0,0,.42);backdrop-filter:blur(2px);pointer-events:auto;padding:24px}
  #wb-shell-update .wb-modal{width:min(430px,100%);background:#fff;border:1px solid rgba(0,0,0,.09);
    border-radius:16px;padding:22px;box-shadow:0 30px 80px rgba(0,0,0,.28)}
  #wb-shell-update .wb-modal-head{display:flex;align-items:center;gap:12px}
  #wb-shell-update .wb-modal-title{font-size:15px;font-weight:600}
  #wb-shell-update .wb-modal-sub{font-size:11.5px;color:#71717a;margin-top:2px}
  #wb-shell-update .wb-progress{height:7px;border-radius:99px;background:rgba(0,0,0,.07);
    overflow:hidden;margin:16px 0 10px}
  #wb-shell-update .wb-progress>i{display:block;height:100%;width:0;border-radius:99px;
    background:#f59e0b;transition:width .25s ease}
  #wb-shell-update .wb-progress.wb-indet>i{width:38%;animation:wb-sweep 1.4s cubic-bezier(.4,0,.2,1) infinite}
  @keyframes wb-sweep{0%{transform:translateX(-110%)}100%{transform:translateX(300%)}}
  #wb-shell-update .wb-message{font-size:12.5px;line-height:1.65;color:#3f3f46;
    white-space:pre-wrap;word-break:break-word;max-height:220px;overflow:auto}
  #wb-shell-update .wb-check{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;
    background:rgba(34,197,94,.13);color:#16a34a;font-size:17px;font-weight:700;flex:0 0 auto}
  #wb-shell-update .wb-bad{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;
    background:rgba(239,68,68,.13);color:#dc2626;font-size:17px;font-weight:700;flex:0 0 auto}
  @media (prefers-color-scheme: dark){
    #wb-shell-update{color:#f4f4f5}
    #wb-shell-update .wb-banner,#wb-shell-update .wb-modal{background:#17171a;border-color:rgba(255,255,255,.1)}
    #wb-shell-update .wb-sub,#wb-shell-update .wb-modal-sub{color:#a1a1aa}
    #wb-shell-update .wb-message{color:#d4d4d8}
    #wb-shell-update .wb-progress{background:rgba(255,255,255,.09)}
    #wb-shell-update button{border-color:rgba(255,255,255,.12)}
    #wb-shell-update button:hover:not(:disabled){background:rgba(255,255,255,.06)}
    #wb-shell-update button.wb-primary{background:#f4f4f5;color:#18181b}
    #wb-shell-update button.wb-primary:hover:not(:disabled){background:#f4f4f5}
  }
  `;
  document.documentElement.appendChild(style);

  const host = document.createElement('div');
  host.id = 'wb-shell-update';
  host.innerHTML = `
    <div class="wb-banner" id="wb-banner" style="display:none">
      <div class="wb-icon">↑</div>
      <div class="wb-grow">
        <div class="wb-title">发现新版本 <span id="wb-banner-ver"></span></div>
        <div class="wb-sub">当前 <span id="wb-banner-cur"></span>，更新后会自动重启服务</div>
        <div class="wb-actions">
          <button id="wb-later">稍后</button>
          <button id="wb-now" class="wb-primary">立即更新</button>
        </div>
      </div>
    </div>
    <div class="wb-overlay" id="wb-overlay" style="display:none">
      <div class="wb-modal">
        <div class="wb-modal-head">
          <div id="wb-modal-icon" class="wb-check" style="display:none">✓</div>
          <div>
            <div class="wb-modal-title" id="wb-modal-title">正在更新</div>
            <div class="wb-modal-sub" id="wb-modal-sub"></div>
          </div>
        </div>
        <div class="wb-progress" id="wb-progress"><i id="wb-progress-bar"></i></div>
        <div class="wb-message" id="wb-message"></div>
        <div class="wb-actions" id="wb-modal-actions" style="display:none">
          <button id="wb-rollback" class="wb-danger" style="display:none">回滚</button>
          <button id="wb-close" class="wb-primary">关闭</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(host);

  const $ = (id) => host.querySelector('#' + id);
  const banner = $('wb-banner');
  const overlay = $('wb-overlay');
  const progress = $('wb-progress');
  const bar = $('wb-progress-bar');
  const message = $('wb-message');
  let latest = '';
  let current = '';

  function post(type) {
    try { window.chrome.webview.postMessage({ type: type }); } catch (e) { }
  }

  $('wb-now').addEventListener('click', function () {
    this.disabled = true;
    post('installUpdate');
  });
  $('wb-later').addEventListener('click', function () {
    banner.style.display = 'none';
  });
  $('wb-close').addEventListener('click', function () {
    overlay.style.display = 'none';
    banner.style.display = 'none';
  });
  $('wb-rollback').addEventListener('click', function () {
    this.disabled = true;
    post('rollbackUpdate');
  });

  const stepNames = {
    downloading: '下载发布包', verifying: '校验 SHA-256', extracting: '解压发布包',
    stopping: '停止服务', replacing: '替换程序文件', dependencies: '同步 Python 依赖',
    starting: '重新启动服务'
  };

  function setIcon(kind) {
    const icon = $('wb-modal-icon');
    icon.style.display = kind ? 'grid' : 'none';
    icon.className = kind === 'error' ? 'wb-bad' : 'wb-check';
    icon.textContent = kind === 'error' ? '!' : '✓';
  }

  window.__wbShellUpdate = {
    show: function (info) {
      current = info.current || '';
      latest = info.latest || '';
      $('wb-banner-ver').textContent = latest;
      $('wb-banner-cur').textContent = current;
      $('wb-now').disabled = false;
      banner.style.display = 'flex';
    },
    progress: function (p) {
      banner.style.display = 'none';
      overlay.style.display = 'flex';
      $('wb-modal-title').textContent = '正在更新' + (latest ? '到 ' + latest : '');
      $('wb-modal-sub').textContent = '更新期间服务会短暂停止，完成后自动恢复';
      setIcon(null);
      $('wb-modal-actions').style.display = 'none';
      $('wb-rollback').style.display = 'none';
      const phase = p.phase || '';
      const percent = Number(p.percent) || 0;
      if (phase === 'downloading' && percent > 0) {
        progress.classList.remove('wb-indet');
        bar.style.width = Math.max(0, Math.min(100, percent)) + '%';
        $('wb-modal-sub').textContent = '正在下载 ' + percent.toFixed(0) + '%';
      } else {
        progress.classList.add('wb-indet');
        bar.style.width = '';
        if (stepNames[phase]) $('wb-modal-sub').textContent = stepNames[phase];
      }
      message.textContent = p.message || '';
    },
    done: function (text) {
      overlay.style.display = 'flex';
      banner.style.display = 'none';
      setIcon('ok');
      progress.classList.remove('wb-indet');
      bar.style.width = '100%';
      $('wb-modal-title').textContent = '更新完成';
      $('wb-modal-sub').textContent = '';
      message.textContent = text || '服务已重新启动，正在重新加载面板…';
      $('wb-modal-actions').style.display = 'none';
    },
    error: function (text, canRollback) {
      overlay.style.display = 'flex';
      banner.style.display = 'none';
      setIcon('error');
      progress.classList.remove('wb-indet');
      $('wb-modal-title').textContent = '更新没有完成';
      $('wb-modal-sub').textContent = '';
      message.textContent = text || '未知错误';
      $('wb-modal-actions').style.display = 'flex';
      $('wb-rollback').style.display = canRollback ? '' : 'none';
      $('wb-rollback').disabled = false;
    },
    hide: function () {
      banner.style.display = 'none';
      overlay.style.display = 'none';
    }
  };
  // 登录页：把用户名/密码交给外壳保存（用于下次自动登录）
  try {
    if (location.pathname.indexOf('/login') >= 0 && window.chrome && chrome.webview) {
      const report = function (body) {
        try {
          const data = typeof body === 'string' ? JSON.parse(body) : null;
          if (data && data.username && data.password) {
            chrome.webview.postMessage({
              type: 'saveCreds',
              username: String(data.username),
              password: String(data.password)
            });
          }
        } catch (e) { }
      };
      const origOpen = XMLHttpRequest.prototype.open;
      const origSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function (method, url) {
        this.__wbUrl = String(url || '');
        return origOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function (body) {
        if (this.__wbUrl && this.__wbUrl.indexOf('/api/login') >= 0) report(body);
        return origSend.apply(this, arguments);
      };
      if (window.fetch) {
        const origFetch = window.fetch;
        window.fetch = function (input, init) {
          try {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url.indexOf('/api/login') >= 0 && init && init.body) report(init.body);
          } catch (e) { }
          return origFetch.apply(this, arguments);
        };
      }
    }
  } catch (e) { }
})();
""";
}
