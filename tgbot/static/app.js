const $ = (id) => document.getElementById(id);
const NUMBERS = ["max_history_messages", "max_context_chars", "max_prompt_chars", "max_tokens", "temperature",
  "request_timeout", "rate_limit_per_min"];
let key = "";
try { key = sessionStorage.getItem("tgbot-key") || ""; } catch {}

function show(text, error = false) {
  const box = $("message");
  box.textContent = text;
  box.dataset.error = error;
  box.hidden = false;
  clearTimeout(show.timer);
  show.timer = setTimeout(() => { box.hidden = true; }, 5000);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
  });
  if (response.status === 401) {
    logout();
    throw new Error("Неверный ключ администратора");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((x) => `${x.loc.at(-1)}: ${x.msg}`).join("; ") : body.detail;
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return body;
}

const lines = (map) => Object.entries(map).map(([name, url]) => `${name}=${url}`).join("\n");
const parseLines = (text) => Object.fromEntries(text.split("\n").map((x) => x.trim()).filter(Boolean)
  .map((x) => { const i = x.indexOf("="); return [x.slice(0, i).trim(), x.slice(i + 1).trim()]; }));

function fill(values) {
  $("token-current").textContent = values.bot_token ? `(сейчас ${values.bot_token})` : "(не задан)";
  $("admins").value = values.admins.join(", ");
  $("system_prompt").value = values.system_prompt;
  for (const name of NUMBERS) $(name).value = values[name];
  $("llm_backends").value = lines(values.llm_backends);
  $("image_backends").value = lines(values.image_backends);
}

async function refreshStatus() {
  const data = await api("/api/status");
  const bot = data.bot;
  $("bot-status").textContent = bot.polling ? `работает, @${bot.username}` : `остановлен${bot.error ? `: ${bot.error}` : " (нет токена)"}`;
  $("llm-status").textContent = data.backends.llm || data.backends.llm_error;
  $("image-status").textContent = data.backends.image || data.backends.image_error;
  $("active-requests").textContent = data.active_requests;
  $("connection").textContent = bot.polling ? `@${bot.username}` : "Бот остановлен";
  $("connection").dataset.ready = bot.polling;
}

async function refreshUsers() {
  const users = await api("/api/users");
  const body = $("users");
  body.replaceChildren();
  for (const user of users) {
    const row = body.insertRow();
    const name = [user.full_name, user.username && `@${user.username}`].filter(Boolean).join(" ");
    const status = user.admin ? "admin" : user.status;
    for (const value of [user.tg_id, name, status, user.sessions]) row.insertCell().textContent = value;
    row.cells[2].className = `status-${user.status}`;
    const action = user.status === "allowed" ? "block" : "allow";
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = action === "allow" ? "Одобрить" : "Заблокировать";
    button.hidden = user.admin;
    button.onclick = () => run(async () => {
      await api(`/api/users/${user.tg_id}/${action}`, { method: "POST" });
      await refreshUsers();
    });
    row.insertCell().append(button);
  }
  if (!users.length) body.insertRow().insertCell().textContent = "Пока никто не отправил /start";
}

async function run(work, success) {
  try {
    await work();
    if (success) show(success);
  } catch (error) {
    show(error.message, true);
  }
}

function logout() {
  key = "";
  try { sessionStorage.removeItem("tgbot-key"); } catch {}
  $("app").hidden = true;
  $("login").hidden = false;
  $("connection").textContent = "Нет ключа";
}

async function load() {
  fill(await api("/api/settings"));
  $("login").hidden = true;
  $("app").hidden = false;
  await Promise.all([refreshStatus(), refreshUsers()]);
}

$("login-form").onsubmit = (event) => {
  event.preventDefault();
  key = $("admin-key").value;
  run(async () => {
    await load();
    try { sessionStorage.setItem("tgbot-key", key); } catch {}
  });
};

$("token-form").onsubmit = (event) => {
  event.preventDefault();
  run(async () => {
    const result = await api("/api/settings/token", { method: "PUT", body: JSON.stringify({ token: $("token").value.trim() }) });
    $("token").value = "";
    fill(await api("/api/settings"));
    await refreshStatus();
    show(`Бот @${result.username} запущен`);
  });
};

$("token-delete").onclick = () => run(async () => {
  await api("/api/settings/token", { method: "DELETE" });
  fill(await api("/api/settings"));
  await refreshStatus();
}, "Бот остановлен, токен удален");

$("settings-form").onsubmit = (event) => {
  event.preventDefault();
  run(async () => {
    const values = {
      admins: $("admins").value.split(/[\s,]+/).filter(Boolean).map(Number),
      system_prompt: $("system_prompt").value,
      llm_backends: parseLines($("llm_backends").value),
      image_backends: parseLines($("image_backends").value),
    };
    for (const name of NUMBERS) values[name] = Number($(name).value);
    fill(await api("/api/settings", { method: "PUT", body: JSON.stringify(values) }));
    await refreshStatus();
  }, "Настройки сохранены");
};

$("refresh").onclick = () => run(() => Promise.all([refreshStatus(), refreshUsers()]));

if (key) run(load);
setInterval(() => { if (key && !$("app").hidden) refreshStatus().catch(() => {}); }, 15000);
