"use strict";
const $ = (id) => document.getElementById(id);
const labels = {queued:"В очереди",running:"Генерация",cancelling:"Отмена",cancelled:"Отменено",completed:"Готово",failed:"Ошибка",interrupted:"Прервано"};
let selected = localStorage.getItem("sdxl-job"), current = null, submitting = false;
async function api(path, options = {}) {
  const response = await fetch(path, {...options, signal: AbortSignal.timeout(15000)});
  const data = await response.json();
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Проверьте параметры запроса";
    throw new Error(detail);
  }
  return data;
}
function error(message) { $("error").textContent = message; $("error").hidden = !message; }
function restoreForm(job) {
  for (const key of ["prompt","negative_prompt","seed","steps","guidance_scale"]) $(key).value = job.parameters[key];
}
function select(job) {
  selected = job.id;
  localStorage.setItem("sdxl-job", selected);
  render(job);
}
function clearSelection() {
  selected = null;
  current = null;
  localStorage.removeItem("sdxl-job");
  $("image").hidden = true;
  $("empty").hidden = false;
  for (const id of ["download", "cancel", "progress"]) $(id).hidden = true;
  $("result-title").textContent = "Результат";
  $("status").textContent = "Нет изображения";
  $("details").textContent = "";
}
function render(job) {
  current = job;
  const complete = job.status === "completed";
  const busy = ["queued","running","cancelling"].includes(job.status);
  $("image").hidden = !complete;
  $("empty").hidden = complete;
  $("download").hidden = !complete;
  $("cancel").hidden = !busy;
  $("cancel").disabled = job.status === "cancelling";
  $("result-title").textContent = labels[job.status] || job.status;
  $("status").textContent = job.error || labels[job.status] || job.status;
  $("progress").hidden = !busy;
  $("progress").max = job.parameters.steps;
  $("progress").value = job.progress;
  if (complete) {
    const url = `/api/generations/${job.id}/image`;
    if ($("image").getAttribute("src") !== url) $("image").src = url;
    $("download").href = url;
    $("download").download = `sdxl-${job.parameters.seed}.png`;
  }
  const p = job.parameters;
  const seconds = job.metadata?.seconds;
  $("details").textContent = `${p.width} × ${p.height} · Seed ${p.seed} · ${p.steps} шагов · Guidance ${p.guidance_scale}${seconds ? ` · ${seconds.toFixed(1)} с` : ""}`;
}
function renderHistory(jobs) {
  $("history-count").textContent = String(jobs.length);
  $("history-list").replaceChildren(...jobs.map(job => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-item";
    button.setAttribute("aria-pressed", String(job.id === selected));
    button.title = job.parameters.prompt;
    if (job.status === "completed") {
      const image = document.createElement("img");
      image.src = `/api/generations/${job.id}/image`;
      image.alt = job.parameters.prompt;
      image.loading = "lazy";
      button.append(image);
    } else {
      const placeholder = document.createElement("div");
      placeholder.className = "history-placeholder";
      placeholder.textContent = labels[job.status] || job.status;
      button.append(placeholder);
    }
    const caption = document.createElement("span");
    caption.textContent = job.parameters.prompt;
    button.append(caption);
    button.addEventListener("click", () => {
      select(job);
      restoreForm(job);
      renderHistory(jobs);
    });
    return button;
  }));
}
$("generate-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (submitting) return;
  submitting = true;
  $("generate").disabled = true;
  error("");
  try {
    const parameters = {prompt:$("prompt").value,negative_prompt:$("negative_prompt").value,
      seed:$("seed").value === "" ? null : Number($("seed").value),steps:Number($("steps").value),
      guidance_scale:Number($("guidance_scale").value),width:1024,height:1024};
    select(await api("/api/generations", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(parameters)}));
    if (window.matchMedia("(max-width:700px)").matches) document.querySelector(".result").scrollIntoView({behavior:"smooth"});
    renderHistory(await api("/api/generations"));
  } catch (e) { error(e.message); }
  finally { submitting = false; }
});
$("cancel").addEventListener("click", async () => {
  if (!selected) return;
  $("cancel").disabled = true;
  try { render(await api(`/api/generations/${selected}`, {method:"DELETE"})); }
  catch (e) { error(e.message); $("cancel").disabled = false; }
});
$("image").addEventListener("error", () => error("Изображение недоступно или срок хранения истек"));
let cycles = 0;
async function poll() {
  try {
    const response = await fetch("/health/ready", {signal:AbortSignal.timeout(10000)});
    const status = await response.json();
    $("connection").textContent = response.ok ? "Модель готова" : "Модель не готова";
    $("connection").dataset.ready = String(response.ok);
    $("generate").disabled = !response.ok || submitting;
    if (status.pipeline) $("mode").textContent = `${status.pipeline.mode === "dual" ? "2 GPU" : "1 GPU"} · FP16`;
    if (selected) {
      const res = await fetch(`/api/generations/${selected}`, {signal:AbortSignal.timeout(10000)});
      if (res.status === 404) clearSelection();
      else if (res.ok) {
        const job = await res.json();
        if (!current) restoreForm(job);
        render(job);
      }
    }
    if (cycles++ % 5 === 0) {
      const history = await api("/api/generations");
      if (!selected && history.length) { select(history[0]); restoreForm(history[0]); }
      renderHistory(history);
    }
  } catch (e) {
    $("connection").textContent = "Нет соединения";
    $("connection").dataset.ready = "false";
    $("generate").disabled = true;
  } finally { setTimeout(poll, 2000); }
}
poll();
