// Kitchen Genome Chrome MV3 Service Worker
// Uses activeTab + scripting for manual clipping and persists queue/cursor in chrome.storage.local
// so service worker termination never loses queued clips or progress.

const STORAGE_KEY = 'kg_clipper_checkpoint_v1';

const PII_PATTERNS = [
  [/\b\d{6}\s*[-~]\s*[1-4]\d{6}\b/g, '[REDACTED_RRN]'],
  [/\b(?:01[016789]|02|0[3-6][1-5]|070)\s*[-.\s]\s*\d{3,4}\s*[-.\s]\s*\d{4}\b/g, '[REDACTED_PHONE]'],
  [/\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g, '[REDACTED_EMAIL]'],
  [/(?:bearer\s+[a-z0-9_\-.=]{12,}|(?:session|token|password|passwd|secret|cookie)\s*[=:]\s*[^\s,;"']{6,})/gi, '[REDACTED_CREDENTIAL]'],
  [/(?:이\s*명령을\s*실행|이전\s*지시를\s*무시|ignore\s+previous\s+instructions|system\s*prompt\s*:)/gi, '[DATA_LITERAL_NOT_INSTRUCTION]']
];

function sanitizeClientText(raw) {
  let out = String(raw || '');
  for (const [re, rep] of PII_PATTERNS) {
    out = out.replace(re, rep);
  }
  return out.replace(/\s+/g, ' ').trim().slice(0, 1500);
}

async function loadCheckpoint() {
  const data = await chrome.storage.local.get(STORAGE_KEY);
  return data[STORAGE_KEY] || { brand: 'kitchengenome', cursor: 0, queue: [], updatedAt: null };
}

async function saveCheckpoint(state) {
  state.updatedAt = new Date().toISOString();
  await chrome.storage.local.set({ [STORAGE_KEY]: state });
}

chrome.runtime.onInstalled.addListener(async () => {
  const st = await loadCheckpoint();
  await saveCheckpoint(st);
  chrome.alarms.create('kg_checkpoint_sync', { periodInMinutes: 5 });
});

chrome.runtime.onStartup.addListener(async () => {
  await loadCheckpoint();
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== 'kg_checkpoint_sync') return;
  const st = await loadCheckpoint();
  if (st.cursor < st.queue.length) {
    await saveCheckpoint(st);
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === 'KG_CLIP_ACTIVE_TAB') {
    (async () => {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab || !tab.id) {
        sendResponse({ ok: false, error: 'no_active_tab' });
        return;
      }
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          const clone = (document.querySelector('article, main') || document.body).cloneNode(true);
          clone.querySelectorAll('script, style, form, input, textarea, select, button, nav, footer, header, aside, iframe').forEach(el => el.remove());
          const sel = window.getSelection ? String(window.getSelection()) : '';
          return {
            title: document.title || '',
            url: location.href,
            snippet: (sel && sel.trim().length > 10 ? sel : clone.innerText || '').slice(0, 2000)
          };
        }
      });
      const extracted = (results && results[0] && results[0].result) || { title: tab.title, url: tab.url, snippet: '' };
      const clipItem = {
        brand: 'kitchengenome',
        collection_path: 'manual_clip',
        url: extracted.url,
        title: sanitizeClientText(extracted.title),
        selection_or_main: sanitizeClientText(extracted.snippet),
        clipped_at: new Date().toISOString()
      };
      const st = await loadCheckpoint();
      st.queue.push(clipItem);
      await saveCheckpoint(st);
      sendResponse({ ok: true, queued: st.queue.length, cursor: st.cursor, item: clipItem });
    })();
    return true;
  }
});
