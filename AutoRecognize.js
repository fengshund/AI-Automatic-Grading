// ==UserScript==
// @name         AI半自动阅卷助手
// @namespace    http://tampermonkey.net/
// @version      0.6
// @description  读取当前题图片，调用本地AI评分服务，半自动打分与提交
// @match        http://sd-pt.cn/web/main.jsp*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      img3.sd-pt.cn
// @connect      sd-pt.cn
// ==/UserScript==

(function () {
  'use strict';

  const API_URL = 'http://127.0.0.1:8000/grade';
  const DEFAULT_QUESTION_ID = '26-2';

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function getAllAccessibleWindows(rootWin = window.top) {
    const wins = [];
    const visited = new Set();

    function walk(win) {
      if (!win || visited.has(win)) return;
      visited.add(win);
      wins.push(win);

      try {
        for (let i = 0; i < win.frames.length; i++) {
          walk(win.frames[i]);
        }
      } catch (e) {}
    }

    walk(rootWin);
    return wins;
  }

  function findElementInAllFrames(selector) {
    const wins = getAllAccessibleWindows();
    for (const win of wins) {
      try {
        const el = win.document.querySelector(selector);
        if (el) return { el, win, doc: win.document };
      } catch (e) {}
    }
    return null;
  }

  function findFunctionInAllFrames(functionName) {
    const wins = getAllAccessibleWindows();
    for (const win of wins) {
      try {
        if (typeof win[functionName] === 'function') {
          return win[functionName].bind(win);
        }
      } catch (e) {}
    }
    return null;
  }

  async function waitForImage(selector = '#mainimg_0', timeout = 8000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      const found = findElementInAllFrames(selector);
      if (found && found.el) {
        const img = found.el;
        if (img.src && img.complete && img.naturalWidth > 0) {
          return img;
        }
      }
      await sleep(200);
    }
    throw new Error(`未找到答题图片 ${selector}`);
  }

  async function getImageUrl() {
    const img = await waitForImage('#mainimg_0');
    return img.src;
  }

  function fetchImageAsBlob(url) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET',
        url,
        responseType: 'blob',
        onload: function (res) {
          if (res.status >= 200 && res.status < 300) {
            resolve(res.response);
          } else {
            reject(new Error(`图片下载失败: HTTP ${res.status}`));
          }
        },
        onerror: function () {
          reject(new Error('下载图片失败，可能是跨域或网络问题'));
        }
      });
    });
  }

  function blobToDataURL(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('图片转 base64 失败'));
      reader.readAsDataURL(blob);
    });
  }

  async function imageToBase64FromImgUrl(url) {
    const blob = await fetchImageAsBlob(url);
    return await blobToDataURL(blob);
  }

  function setScore(score) {
    const fn = findFunctionInAllFrames('dfbtjfs');
    if (!fn) throw new Error('未找到打分函数 dfbtjfs');
    fn('0', String(score));
  }

  function submitScore() {
    const fn = findFunctionInAllFrames('tjfs');
    if (!fn) throw new Error('未找到提交函数 tjfs');
    fn();
  }

  function requestGrade(payload) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST',
        url: API_URL,
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify(payload),
        onload: function (res) {
          try {
            resolve(JSON.parse(res.responseText));
          } catch (e) {
            reject(new Error('后端返回不是合法 JSON'));
          }
        },
        onerror: function () {
          reject(new Error('请求本地评分服务失败'));
        }
      });
    });
  }

  function ensurePanel() {
    let panel = document.querySelector('#ai-mark-panel');
    if (panel) return panel;

    panel = document.createElement('div');
    panel.id = 'ai-mark-panel';
    panel.innerHTML = `
      <div id="ai-mark-header">
        <span>AI阅卷</span>
        <button id="ai-toggle-btn" type="button">展开</button>
      </div>

      <div class="ai-row compact">
        <label>状态</label>
        <div id="ai-status" class="ai-status">待命</div>
      </div>

      <div class="ai-row compact">
        <label>建议分</label>
        <input id="ai-score" value="" />
      </div>

      <div id="ai-extra" class="collapsed">
        <div class="ai-row column">
          <label>识别文本</label>
          <textarea id="ai-text" rows="2"></textarea>
        </div>
        <div class="ai-row column">
          <label>命中得分点</label>
          <textarea id="ai-points" rows="2"></textarea>
        </div>
        <div class="ai-row column">
          <label>评分理由</label>
          <textarea id="ai-reason" rows="2"></textarea>
        </div>
      </div>

      <div class="ai-actions">
        <button id="ai-run-btn" type="button">AI评分</button>
        <button id="ai-fill-btn" type="button">填分</button>
        <button id="ai-submit-btn" type="button">提交</button>
        <button id="ai-debug-btn" type="button">找图</button>
        <button id="ai-close-btn" type="button">关</button>
      </div>
    `;

    const style = document.createElement('style');
    style.textContent = `
      #ai-mark-panel {
        position: fixed;
        left: 10px;
        bottom: 10px;
        width: 220px;
        max-height: 50vh;
        overflow: auto;
        z-index: 999999;
        background: rgba(255,255,255,0.96);
        border: 1px solid #cfcfcf;
        border-radius: 8px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.14);
        padding: 6px;
        font-size: 12px;
        color: #222;
      }

      #ai-mark-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 6px;
        font-size: 13px;
        font-weight: 700;
      }

      #ai-mark-header button {
        background: #7f8c8d;
        color: #fff;
        border: none;
        border-radius: 4px;
        padding: 3px 6px;
        font-size: 11px;
        cursor: pointer;
      }

      #ai-mark-panel .ai-row {
        display: flex;
        align-items: center;
        gap: 6px;
        margin-bottom: 6px;
      }

      #ai-mark-panel .ai-row.compact label {
        min-width: 40px;
      }

      #ai-mark-panel .ai-row.column {
        flex-direction: column;
        align-items: stretch;
      }

      #ai-mark-panel label {
        color: #444;
        font-size: 12px;
      }

      #ai-mark-panel input,
      #ai-mark-panel textarea {
        width: 100%;
        box-sizing: border-box;
        border: 1px solid #ccc;
        border-radius: 5px;
        padding: 4px 6px;
        font-size: 12px;
        line-height: 1.35;
      }

      #ai-mark-panel textarea {
        resize: vertical;
        min-height: 44px;
      }

      #ai-mark-panel .ai-status {
        flex: 1;
        background: #f5f5f5;
        border-radius: 5px;
        padding: 4px 6px;
        min-height: 16px;
        line-height: 1.35;
      }

      #ai-extra.collapsed {
        display: none;
      }

      #ai-extra.expanded {
        display: block;
      }

      #ai-mark-panel .ai-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 5px;
        margin-top: 4px;
      }

      #ai-mark-panel .ai-actions button {
        border: none;
        border-radius: 5px;
        padding: 5px 7px;
        cursor: pointer;
        color: white;
        font-size: 12px;
        min-width: 40px;
      }

      #ai-run-btn { background: #16a085; }
      #ai-fill-btn { background: #2980b9; }
      #ai-submit-btn { background: #8e44ad; }
      #ai-debug-btn { background: #c0392b; }
      #ai-close-btn { background: #7f8c8d; }
    `;
    document.head.appendChild(style);
    document.body.appendChild(panel);

    panel.querySelector('#ai-close-btn').onclick = () => {
      panel.style.display = 'none';
    };

    panel.querySelector('#ai-toggle-btn').onclick = () => {
      const extra = panel.querySelector('#ai-extra');
      const btn = panel.querySelector('#ai-toggle-btn');
      const expanded = extra.classList.contains('expanded');
      extra.className = expanded ? 'collapsed' : 'expanded';
      btn.textContent = expanded ? '展开' : '收起';
    };

    return panel;
  }

  function showPanel() {
    const panel = ensurePanel();
    panel.style.display = 'block';
    return panel;
  }

  function setStatus(text) {
    const panel = ensurePanel();
    panel.querySelector('#ai-status').textContent = text;
  }

  function setResult(result) {
    const panel = ensurePanel();
    panel.querySelector('#ai-score').value = result?.score ?? '';
    panel.querySelector('#ai-text').value = result?.recognized_text || '';
    panel.querySelector('#ai-points').value = (result?.matched_points || []).join('、');
    panel.querySelector('#ai-reason').value = result?.reason || '';
  }

  function getScoreFromPanel() {
    const panel = ensurePanel();
    return panel.querySelector('#ai-score').value.trim();
  }

  function expandDetails() {
    const panel = ensurePanel();
    panel.querySelector('#ai-extra').className = 'expanded';
    panel.querySelector('#ai-toggle-btn').textContent = '收起';
  }

  async function runAiMarking() {
    try {
      showPanel();
      setStatus('查找图片...');
      const imageUrl = await getImageUrl();

      setStatus('下载图片...');
      const imageBase64 = await imageToBase64FromImgUrl(imageUrl);

      setStatus('AI评分中...');
      const payload = {
        question_id: DEFAULT_QUESTION_ID,
        image_base64: imageBase64
      };

      const result = await requestGrade(payload);

      if (!result.success) {
        throw new Error(result.message || '未知错误');
      }

      setResult(result);
      expandDetails();
      setStatus('完成');
    } catch (e) {
      console.error(e);
      setStatus('失败：' + e.message);
    }
  }

  async function debugFindImage() {
    try {
      showPanel();
      setStatus('调试找图...');
      const imageUrl = await getImageUrl();
      setStatus('已找到图片');
      const panel = ensurePanel();
      panel.querySelector('#ai-reason').value = imageUrl;
      expandDetails();
      console.log('找到的图片 URL:', imageUrl);
    } catch (e) {
      console.error(e);
      setStatus('找图失败：' + e.message);
    }
  }

  function fillScoreFromPanel() {
    try {
      const score = getScoreFromPanel();
      if (score === '') throw new Error('没有可填入的分数');
      setScore(score);
      setStatus(`已填分：${score}`);
    } catch (e) {
      console.error(e);
      setStatus('填分失败：' + e.message);
    }
  }

  function submitFromPanel() {
    try {
      submitScore();
      setStatus('已提交');
    } catch (e) {
      console.error(e);
      setStatus('提交失败：' + e.message);
    }
  }

  function bindPanelEvents() {
    const panel = ensurePanel();
    panel.querySelector('#ai-run-btn').onclick = runAiMarking;
    panel.querySelector('#ai-fill-btn').onclick = fillScoreFromPanel;
    panel.querySelector('#ai-submit-btn').onclick = submitFromPanel;
    panel.querySelector('#ai-debug-btn').onclick = debugFindImage;
  }

  function addQuickButton() {
    if (document.querySelector('#ai-mark-open-btn')) return;

    const btn = document.createElement('button');
    btn.id = 'ai-mark-open-btn';
    btn.innerText = 'AI';
    btn.style.position = 'fixed';
    btn.style.left = '10px';
    btn.style.bottom = '10px';
    btn.style.zIndex = '999998';
    btn.style.padding = '5px 8px';
    btn.style.background = '#16a085';
    btn.style.color = '#fff';
    btn.style.border = 'none';
    btn.style.borderRadius = '6px';
    btn.style.cursor = 'pointer';
    btn.style.fontSize = '12px';
    btn.style.boxShadow = '0 2px 6px rgba(0,0,0,0.18)';
    btn.onclick = () => {
      showPanel();
      bindPanelEvents();
    };

    document.body.appendChild(btn);
  }

  window.addEventListener('load', () => {
    setTimeout(() => {
      addQuickButton();
      ensurePanel();
      bindPanelEvents();
    }, 1000);
  });

  setTimeout(() => {
    addQuickButton();
    ensurePanel();
    bindPanelEvents();
  }, 2000);
})();