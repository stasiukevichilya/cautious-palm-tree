// Context ring next to the model selector and per-message token/time lines, like the llama.cpp web UI.
// Served as /static/loader.js (Open WebUI's custom script hook, see compose.yaml). Data comes from
// message.contextStats, written by the Context usage filter (agent/functions/context_usage.py).
// While a message streams, the line is live: llama.cpp timings_per_token (requested by that filter)
// reach the browser as socket.io "events" / chat:completion usage, read here off the WebSocket.
// Reasoning effort selector right after the model selector: one choice for every chat and model, kept in localStorage and
// added to each chat request as params.reasoning_effort; the Reasoning effort filter
// (agent/functions/reasoning_effort.py) turns it into chat template kwargs. "Авто" adds nothing.
(() => {
  'use strict';

  // Must run before Open WebUI opens its socket (loader.js runs at DOMContentLoaded, the socket later).
  const live = new Map(); // assistant message id -> { usage, seenAt, genStartAt, done }
  const NativeWebSocket = window.WebSocket;
  window.WebSocket = class extends NativeWebSocket {
    constructor(...args) {
      super(...args);
      this.addEventListener('message', (event) => onSocketFrame(event.data));
    }
  };

  const EFFORT_KEY = 'reasoning-effort';
  const EFFORTS = [
    ['', 'Авто', 'Как задано у модели'],
    ['none', 'Выкл', 'Без рассуждений'],
    ['low', 'Низкий', 'Короткие рассуждения'],
    ['medium', 'Средний', 'Средние рассуждения'],
    ['high', 'Высокий', 'Максимальные рассуждения']
  ];
  const effortValue = () => {
    const value = localStorage.getItem(EFFORT_KEY) || '';
    return EFFORTS.some(([v]) => v === value) ? value : '';
  };

  // Open WebUI posts every chat turn to /api/chat/completions; its "params" win over the model's own.
  const nativeFetch = window.fetch;
  window.fetch = function (input, init) {
    const effort = effortValue();
    if (effort && init && typeof init.body === 'string' && String(init.method || 'GET').toUpperCase() === 'POST') {
      try {
        const url = new URL(typeof input === 'string' ? input : input.url, location.href);
        if (url.pathname === '/api/chat/completions') {
          const body = JSON.parse(init.body);
          body.params = { ...(body.params || {}), reasoning_effort: effort };
          init = { ...init, body: JSON.stringify(body) };
        }
      } catch {
        // not JSON: leave the request as it is
      }
    }
    return nativeFetch.call(this, input, init);
  };

  function onSocketFrame(data) {
    if (typeof data !== 'string' || !data.startsWith('42')) return;
    const match = data.match(/^42(?:\/[^,]*,)?\d*(\[[\s\S]*)$/);
    if (!match) return;
    let packet;
    try {
      packet = JSON.parse(match[1]);
    } catch {
      return;
    }
    const [name, payload] = packet;
    const event = payload?.data;
    if (name !== 'events' || event?.type !== 'chat:completion' || !payload.message_id) return;
    const entry = live.get(payload.message_id) || { usage: null, seenAt: performance.now(), genStartAt: null, done: false };
    const usage = event.data?.usage;
    if (usage && usage.predicted_n != null) {
      if (usage.predicted_n > 0 && entry.genStartAt == null) entry.genStartAt = performance.now() - (usage.predicted_ms || 0);
      entry.usage = usage;
    }
    if (event.data?.done) entry.done = true;
    live.set(payload.message_id, entry);
    startLiveTimer();
  }

  const WIDGET_ID = 'ctx-ring';
  const LINE_CLASS = 'msg-stats';
  const ICONS = {
    tokens: '<path d="M12 3 3 8l9 5 9-5-9-5Z"/><path d="m3 13 9 5 9-5"/>',
    time: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    speed: '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    model: '<path d="M21 8 12 3 3 8v8l9 5 9-5V8Z"/><path d="m3 8 9 5 9-5"/><path d="M12 13v8"/>'
  };
  const TIPS = {
    user: { tokens: 'Токены промпта', time: 'Время обработки промпта', speed: 'Скорость обработки промпта' },
    assistant: { tokens: 'Сгенерировано токенов', time: 'Время генерации', speed: 'Скорость генерации' }
  };

  // "qwen3.8-27b-mtp" -> name "qwen3.8", badges ["27B", "mtp"], like the llama.cpp model chip.
  function modelChip(model, bili, headroom) {
    if (!model) return null;
    const [name, ...rest] = model.split('-');
    const chip = el('span', { class: 'msg-model', 'data-tip': model });
    chip.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS.model}</svg>`;
    chip.append(el('span', { class: 'msg-model-name', text: name }));
    for (const part of rest) chip.append(el('span', { class: 'msg-badge', text: /^\d+(\.\d+)?b$/i.test(part) ? part.toUpperCase() : part }));
    if (bili) chip.append(el('span', { class: 'msg-badge msg-badge-bili', text: 'billion-context' }));
    if (headroom) chip.append(el('span', { class: 'msg-badge msg-badge-bili', text: 'Headroom' }));
    return chip;
  }
  const RING = 2 * Math.PI * 7;
  let state = { chatId: null, stats: null, totals: null, lines: {}, loading: false };
  let popoverOpen = false;
  let refreshTimer = null;
  let lastPath = location.pathname;

  const compact = (n) => {
    if (n == null || Number.isNaN(n)) return '—';
    if (n >= 1e6) return `${+(n / 1e6).toFixed(2)}M`;
    if (n >= 1e3) return `${+(n / 1e3).toFixed(2)}K`;
    return String(Math.round(n));
  };
  const tok = (n) => (n == null ? '—' : `${Math.round(n).toLocaleString('ru-RU')} tok`);
  const chatIdFromPath = () => (location.pathname.match(/^\/c\/([^/?#]+)/) || [])[1] || null;

  function branch(history) {
    const out = [];
    const messages = history?.messages || {};
    let id = history?.currentId;
    while (id && messages[id]) {
      out.unshift(messages[id]);
      id = messages[id].parentId;
    }
    return out;
  }

  const seconds = (ms) => (ms == null ? null : `${(ms / 1000).toFixed(1)}s`);
  const speed = (v) => (v ? `${v.toFixed(2)} t/s` : null);

  // Per-message lines: the user message gets the prompt processing of the answer that follows it,
  // the answer gets its generation. Without llama.cpp timings (billion-context) only token counts.
  function messageLines(messages) {
    const lines = {};
    for (const m of Object.values(messages || {})) {
      const s = m.role === 'assistant' ? m.contextStats : null;
      if (!s) continue;
      const exact = s.prompt_n != null && s.predicted_n != null;
      lines[m.id] = {
        role: 'assistant',
        model: s.model || m.model,
        bili: s.bili,
        headroom: s.headroom,
        parts: exact
          ? [['tokens', `${s.completion} tokens`], ['time', seconds(s.predicted_ms)], ['speed', speed(s.speed)]]
          : [['tokens', `${s.completion} tokens`]]
      };
      if (m.parentId && messages[m.parentId]?.role === 'user' && !lines[m.parentId]) {
        lines[m.parentId] = {
          role: 'user',
          parts: exact
            ? [['tokens', `${s.prompt_n} tokens`], ['time', seconds(s.prompt_ms)], ['speed', speed(s.prompt_speed)]]
            : [['tokens', `${s.evaluated} tokens`]]
        };
      }
    }
    return lines;
  }

  function summarize(chat) {
    const assistants = branch(chat?.history).filter((m) => m.role === 'assistant');
    let stats = null;
    let evaluated = 0;
    let generated = 0;
    let speedTokens = 0;
    let speedTime = 0;
    for (const m of assistants) {
      const s = m.contextStats;
      const u = m.usage || m.info?.usage;
      if (s) {
        stats = s;
        evaluated += s.evaluated || 0;
        generated += s.completion || 0;
        if (s.speed && s.generated) {
          speedTokens += s.generated;
          speedTime += s.generated / s.speed;
        }
      } else if (u) {
        evaluated += Math.max(0, (u.prompt_tokens || 0) - (u.prompt_tokens_details?.cached_tokens || 0));
        generated += u.completion_tokens || 0;
      }
    }
    if (stats?.window) localStorage.setItem(`ctx-window:${stats.model}`, String(stats.window));
    return {
      stats,
      lines: messageLines(chat?.history?.messages),
      totals: { evaluated, generated, speed: speedTime > 0 ? speedTokens / speedTime : null, turns: assistants.length }
    };
  }

  async function refresh() {
    const chatId = chatIdFromPath();
    if (!chatId) {
      state = { chatId: null, stats: null, totals: null, lines: {}, loading: false };
      render();
      return;
    }
    const token = localStorage.token;
    if (!token) return;
    state.loading = true;
    try {
      const response = await fetch(`/api/v1/chats/${chatId}`, { headers: { Authorization: `Bearer ${token}` } });
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json();
      if (chatIdFromPath() !== chatId) return; // the user switched chats meanwhile
      state = { chatId, ...summarize(data.chat), loading: false };
    } catch {
      state.loading = false;
    }
    render();
  }

  function scheduleRefresh(delay = 1200) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, delay);
  }

  function anchor() {
    const container = document.getElementById('message-input-container');
    if (!container) return null;
    return [...container.querySelectorAll('div.self-end.flex')].find((el) => el.classList.contains('mr-1')) || null;
  }

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else node.setAttribute(k, v);
    }
    for (const child of children) if (child) node.append(child);
    return node;
  }

  const row = (label, value, strong = false) =>
    el('div', { class: `ctx-row${strong ? ' ctx-strong' : ''}` }, [el('span', { text: label }), el('span', { class: 'ctx-num', text: value })]);

  function popover() {
    const { stats, totals } = state;
    const window_ = stats?.window || null;
    const used = stats?.kv ?? null;
    const pct = window_ && used != null ? Math.min(100, (100 * used) / window_) : 0;
    const pop = el('div', { class: 'ctx-pop', role: 'dialog' });
    pop.append(
      el('div', { class: 'ctx-title' }, [
        el('b', { text: 'Контекст' }),
        el('span', { class: 'ctx-num', text: ` · ${compact(used)} / ${compact(window_)}` }),
        stats?.bili ? el('span', { class: 'ctx-badge', text: 'billion-context' }) : null,
        stats?.headroom ? el('span', { class: 'ctx-badge', text: 'Headroom' }) : null
      ]),
      el('div', { class: 'ctx-bar' }, [el('div', { class: 'ctx-fill', style: `width:${pct}%` })]),
      el('div', { class: 'ctx-row ctx-muted' }, [
        el('span', { text: `${pct.toFixed(0)}% занято` }),
        el('span', { text: window_ && used != null ? `${compact(window_ - used)} осталось` : '' })
      ])
    );
    if (!stats) {
      pop.append(el('div', { class: 'ctx-empty', text: state.chatId ? 'Нет данных: ответов с usage ещё не было' : 'Новый чат' }));
      return pop;
    }
    pop.append(
      el('div', { class: 'ctx-sep' }),
      el('div', { class: 'ctx-head', text: `За весь чат · ответов: ${totals.turns}` }),
      row('Обработано токенов промпта', tok(totals.evaluated)),
      row('Сгенерировано токенов', tok(totals.generated)),
      el('div', { class: 'ctx-head', text: stats.exact ? 'Этот ход · KV-кэш' : 'Этот ход · оценка (без timings)' }),
      row('Промпт', tok(stats.prompt)),
      row('Сгенерировано', tok(stats.generated)),
      el('div', { class: 'ctx-sep' }),
      row('Всего в KV-кэше', tok(stats.kv), true),
      el('div', { class: 'ctx-sep' }),
      row('Средняя скорость', totals.speed ? `${totals.speed.toFixed(1)} t/s` : stats.speed ? `${stats.speed.toFixed(1)} t/s` : '—')
    );
    return pop;
  }

  function placeRow(message, role, parts, isLive = false, model = null, bili = false, headroom = false) {
    const text = [model, bili, headroom, ...parts.filter(([, v]) => v).map(([, v]) => v)].join('|');
    const existing = message.querySelector(`.${LINE_CLASS}`);
    if (existing?.dataset.text === text) return;
    existing?.remove();
    const row = el('div', { class: `${LINE_CLASS} ${LINE_CLASS}-${role}${isLive ? ` ${LINE_CLASS}-live` : ''}` });
    row.dataset.text = text;
    if (role === 'assistant') row.append(modelChip(model, bili, headroom) || '');
    for (const [icon, value] of parts) {
      if (!value) continue;
      const item = el('span', { class: 'msg-stat', 'data-tip': TIPS[role][icon] });
      item.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[icon]}</svg>`;
      item.append(value);
      row.append(item);
    }
    const buttons = message.querySelector('.buttons');
    if (buttons) buttons.before(row);
    else (message.querySelector('.chat-user, .chat-assistant') || message).append(row);
  }

  function renderLines() {
    for (const [id, line] of Object.entries(state.lines || {})) {
      const message = document.getElementById(`message-${id}`);
      if (message) placeRow(message, line.role, line.parts, false, line.model, line.bili, line.headroom);
    }
    renderLive();
  }

  // The message element just above an answer is the user message whose prompt it processes.
  function previousMessage(message) {
    const all = [...document.querySelectorAll('[id^="message-"]')].filter((m) => m.id !== 'message-input-container');
    const index = all.indexOf(message);
    return index > 0 ? all[index - 1] : null;
  }

  function renderLive() {
    const now = performance.now();
    for (const [id, entry] of live) {
      if (state.lines?.[id]) {
        live.delete(id); // final numbers from the server have arrived
        continue;
      }
      const message = document.getElementById(`message-${id}`);
      if (!message || !entry.usage) continue;
      const u = entry.usage;
      const generating = u.predicted_n > 0;
      const elapsed = entry.done || entry.genStartAt == null ? u.predicted_ms : now - entry.genStartAt;
      if (generating) {
        const model = message.querySelector('#response-message-model-name')?.textContent?.trim();
        placeRow(message, 'assistant', [
          ['tokens', `${u.predicted_n} tokens`],
          ['time', seconds(elapsed)],
          ['speed', speed(u.predicted_per_second)]
        ], !entry.done, model);
      }
      const user = previousMessage(message);
      const userId = user?.id.slice('message-'.length);
      if (user && !state.lines?.[userId] && (u.prompt_n || u.prompt_ms)) {
        placeRow(user, 'user', [
          ['tokens', `${u.prompt_n} tokens`],
          ['time', seconds(u.prompt_ms)],
          ['speed', speed(u.prompt_per_second)]
        ], !generating);
      }
    }
  }

  let liveTimer = null;
  function startLiveTimer() {
    if (liveTimer) return;
    liveTimer = setInterval(() => {
      renderLive();
      if (![...live.values()].some((e) => !e.done)) {
        clearInterval(liveTimer);
        liveTimer = null;
      }
    }, 200);
  }

  function linesMissing() {
    return Object.keys(state.lines || {}).some((id) => {
      const message = document.getElementById(`message-${id}`);
      return message && !message.querySelector(`.${LINE_CLASS}`);
    });
  }

  const EFFORT_ID = 'effort-pick';
  let effortOpen = false;

  function renderEffort(target, ring) {
    let widget = document.getElementById(EFFORT_ID);
    if (!widget) {
      widget = el('div', { id: EFFORT_ID, class: 'ctx-wrap' });
      const button = el('button', { type: 'button', class: 'eff-btn', 'aria-label': 'Уровень рассуждений' });
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        effortOpen = !effortOpen;
        popoverOpen = false;
        render();
      });
      widget.append(button);
    }
    // Right after the model selector; before the context ring if the selector is not in this bar.
    const selector = document.getElementById('model-selector-model-button')?.closest('#message-input-container div.self-end.flex > *');
    if (selector?.parentElement === target) {
      if (selector.nextSibling !== widget) selector.after(widget);
    } else if (widget.parentElement !== target || widget.nextSibling !== ring) {
      target.insertBefore(widget, ring);
    }

    const value = effortValue();
    const [, label, tip] = EFFORTS.find(([v]) => v === value);
    const button = widget.querySelector('.eff-btn');
    button.className = `eff-btn${value ? ' eff-set' : ''}`;
    button.title = `Рассуждения: ${label} (${tip})`;
    button.innerHTML =
      `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M9 18h6"/><path d="M10 22h4"/>` +
      `<path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.3h6c0-1 .4-1.8 1-2.3A7 7 0 0 0 12 2Z"/></svg>` +
      `<span>${label}</span>`;

    widget.querySelector('.eff-pop')?.remove();
    if (!effortOpen) return;
    const pop = el('div', { class: 'ctx-pop eff-pop', role: 'menu' }, [el('div', { class: 'ctx-head', text: 'Рассуждения · все чаты и модели' })]);
    for (const [v, name, hint] of EFFORTS) {
      const item = el('button', { type: 'button', class: `eff-item${v === value ? ' eff-active' : ''}`, role: 'menuitemradio' }, [
        el('span', { text: name }),
        el('span', { class: 'ctx-muted', text: hint })
      ]);
      item.addEventListener('click', (event) => {
        event.stopPropagation();
        if (v) localStorage.setItem(EFFORT_KEY, v);
        else localStorage.removeItem(EFFORT_KEY);
        effortOpen = false;
        render();
      });
      pop.append(item);
    }
    widget.append(pop);
  }

  function render() {
    renderLines();
    const target = anchor();
    let widget = document.getElementById(WIDGET_ID);
    if (!target) return;
    if (!widget) {
      widget = el('div', { id: WIDGET_ID, class: 'ctx-wrap' });
      const button = el('button', { type: 'button', class: 'ctx-btn', 'aria-label': 'Использование контекста' });
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        popoverOpen = !popoverOpen;
        effortOpen = false;
        render();
        if (popoverOpen) refresh();
      });
      widget.append(button);
    }
    if (widget.parentElement !== target) target.prepend(widget);
    renderEffort(target, widget);

    const { stats } = state;
    const pct = stats?.window ? Math.min(100, (100 * stats.kv) / stats.window) : 0;
    const level = pct >= 90 ? 'ctx-hot' : pct >= 70 ? 'ctx-warm' : '';
    const button = widget.querySelector('.ctx-btn');
    button.className = `ctx-btn ${level}`;
    button.title = stats?.window ? `Контекст: ${compact(stats.kv)} / ${compact(stats.window)} (${pct.toFixed(0)}%)` : 'Контекст';
    button.innerHTML =
      `<svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true">` +
      `<circle cx="9" cy="9" r="7" class="ctx-track"/>` +
      `<circle cx="9" cy="9" r="7" class="ctx-arc" stroke-dasharray="${(RING * pct) / 100} ${RING}" transform="rotate(-90 9 9)"/>` +
      `</svg>`;

    widget.querySelector('.ctx-pop')?.remove();
    if (popoverOpen) widget.append(popover());
  }

  document.addEventListener('click', (event) => {
    if ((popoverOpen || effortOpen) && !event.target.closest?.(`#${WIDGET_ID}, #${EFFORT_ID}`)) {
      popoverOpen = false;
      effortOpen = false;
      render();
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && (popoverOpen || effortOpen)) {
      popoverOpen = false;
      effortOpen = false;
      render();
    }
  });

  // Svelte re-renders the input bar: re-attach the widget, and refresh once a response settles.
  new MutationObserver((mutations) => {
    if (!document.getElementById(WIDGET_ID) || document.getElementById(WIDGET_ID).parentElement !== anchor() ||
        document.getElementById(EFFORT_ID)?.parentElement !== anchor() || linesMissing()) render();
    if (location.pathname !== lastPath) {
      lastPath = location.pathname;
      popoverOpen = false;
      effortOpen = false;
      state.lines = {};
      scheduleRefresh(300);
      return;
    }
    const relevant = mutations.some((m) => {
      const node = m.target.nodeType === 1 ? m.target : m.target.parentElement;
      return node && !node.closest(`#${WIDGET_ID}, #${EFFORT_ID}, #chat-input, .${LINE_CLASS}`);
    });
    if (relevant && chatIdFromPath()) scheduleRefresh();
  }).observe(document.documentElement, { childList: true, subtree: true, characterData: true });

  scheduleRefresh(500);
})();
