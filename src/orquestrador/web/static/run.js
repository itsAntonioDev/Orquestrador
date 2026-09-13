// Página de detalhe: recebe o estado e os logs da execução via WebSocket.
// Todo conteúdo vindo do servidor é inserido com textContent (nunca innerHTML).
(() => {
  "use strict";

  const root = document.getElementById("run-root");
  if (!root) return;

  const labels = JSON.parse(document.getElementById("status-labels").textContent);
  const jobsPanel = document.getElementById("jobs");
  const output = document.getElementById("log-output");
  const title = document.getElementById("log-title");
  const autoscroll = document.getElementById("autoscroll");
  const live = document.getElementById("live-indicator");

  const logs = new Map(); // "job:step" -> [{seq, stream, text}]
  let run = null;
  let selected = null;
  let userSelected = false;
  let finished = false;
  let retryDelay = 1000;

  const key = (jobId, index) => `${jobId}:${index}`;

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return "-";
    if (seconds < 10) return `${seconds.toFixed(1)}s`;
    const total = Math.round(seconds);
    if (total < 60) return `${total}s`;
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return `${minutes}m ${String(total % 60).padStart(2, "0")}s`;
    return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function setLive(text, state) {
    live.textContent = text;
    live.dataset.state = state;
  }

  function renderStep(job, step) {
    const item = element("li");
    const button = element("button", "step");
    button.type = "button";
    button.dataset.job = job.job_id;
    button.dataset.step = String(step.index);
    const dot = element("span", `dot status-${step.status}`);
    dot.title = labels[step.status] || step.status;
    button.append(dot, element("span", "step-name", step.name));
    if (step.group) {
      const tag = element("span", "group-tag", `∥ ${step.group}`);
      tag.title = "grupo paralelo";
      button.append(tag);
    }
    button.append(
      element("span", "muted small", step.started_at ? formatDuration(step.duration) : ""),
    );
    item.append(button);
    return item;
  }

  function renderJob(job) {
    const section = element("section", "job");
    section.dataset.job = job.job_id;
    const header = element("header", "job-header");
    header.append(
      element("span", `badge status-${job.status}`, labels[job.status] || job.status),
      element("strong", "", job.name),
      element("span", "muted small", job.started_at ? formatDuration(job.duration) : ""),
    );
    section.append(header);
    if (job.error) section.append(element("p", "error small", job.error));
    const list = element("ul", "steps");
    job.steps.forEach((step) => list.append(renderStep(job, step)));
    section.append(list);
    return section;
  }

  function findStep(selection) {
    if (!run || !selection) return null;
    const [jobId, index] = selection.split(":");
    const job = run.jobs.find((item) => item.job_id === jobId);
    const step = job && job.steps.find((item) => String(item.index) === index);
    return step ? { job, step } : null;
  }

  function autoSelect() {
    const steps = run.jobs.flatMap((job) => job.steps.map((step) => ({ job, step })));
    const target =
      steps.find(({ step }) => step.status === "running") ||
      steps.filter(({ job, step }) => (logs.get(key(job.job_id, step.index)) || []).length).pop() ||
      steps.find(({ step }) => step.status === "failure") ||
      steps[0];
    if (target) select(target.job.job_id, target.step.index, false);
  }

  function highlightSelection() {
    jobsPanel.querySelectorAll("button.step").forEach((button) => {
      const pressed = key(button.dataset.job, button.dataset.step) === selected;
      button.setAttribute("aria-pressed", pressed ? "true" : "false");
    });
  }

  function appendLines(lines) {
    const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 40;
    const fragment = document.createDocumentFragment();
    lines.forEach((line) => fragment.append(element("span", line.stream, `${line.text}\n`)));
    output.append(fragment);
    if (autoscroll.checked && nearBottom) output.scrollTop = output.scrollHeight;
  }

  function select(jobId, index, byUser) {
    const next = key(jobId, index);
    userSelected = userSelected || byUser;
    if (next === selected) return;
    selected = next;
    const found = findStep(next);
    title.textContent = found ? `${found.job.name} › ${found.step.name}` : next;
    output.replaceChildren();
    const lines = logs.get(next) || [];
    if (lines.length) {
      appendLines(lines);
      output.scrollTop = output.scrollHeight;
    } else {
      output.append(element("span", "placeholder", "Sem saída até o momento."));
    }
    highlightSelection();
  }

  function renderRun(data) {
    run = data;
    const status = document.getElementById("run-status");
    status.className = `badge badge-lg status-${data.status}`;
    status.textContent = labels[data.status] || data.status;
    document.getElementById("run-duration").textContent = formatDuration(data.duration);
    const reason = document.getElementById("run-reason");
    reason.textContent = data.reason || "";
    reason.hidden = !data.reason;

    if (data.jobs.length) {
      jobsPanel.replaceChildren(...data.jobs.map(renderJob));
    } else {
      jobsPanel.replaceChildren(element("p", "muted", "Aguardando o início do pipeline…"));
    }
    if (!userSelected) autoSelect();
    highlightSelection();
  }

  function receiveLogs(event) {
    const stepKey = key(event.job_id, event.step_index);
    const stored = logs.get(stepKey) || [];
    stored.push(...event.lines);
    logs.set(stepKey, stored);
    if (stepKey === selected) {
      output.querySelectorAll(".placeholder").forEach((node) => node.remove());
      appendLines(event.lines);
    } else if (!userSelected && run) {
      autoSelect();
    }
  }

  jobsPanel.addEventListener("click", (event) => {
    const button = event.target.closest("button.step");
    if (button) select(button.dataset.job, button.dataset.step, true);
  });

  function connect() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${window.location.host}${root.dataset.wsPath}`);

    socket.addEventListener("open", () => {
      retryDelay = 1000;
      logs.clear();
      selected = null;
      setLive("ao vivo", "on");
    });

    socket.addEventListener("message", (message) => {
      const event = JSON.parse(message.data);
      if (event.type === "run") renderRun(event.run);
      else if (event.type === "logs") receiveLogs(event);
      else if (event.type === "end") {
        finished = true;
        setLive("execução finalizada", "off");
      } else if (event.type === "error") {
        finished = true;
        setLive(event.message, "error");
      }
    });

    socket.addEventListener("close", () => {
      if (finished) return;
      setLive("reconectando…", "warn");
      window.setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 15000);
    });
  }

  connect();
})();
