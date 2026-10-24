/* All data stays on this host. The monitor API is deliberately read only. */
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const e = escapeHTML;
const colors = [
  "#58a6ff",
  "#63cf8c",
  "#d2a8ff",
  "#f0b65f",
  "#f778ba",
  "#56d4dd",
  "#ff7b72",
  "#a5d6ff",
  "#b6dc7b",
  "#c9b1fa",
  "#e8ce97",
  "#9cd5cb",
];
const state = {
  snapshot: null,
  history: null,
  hours: 24,
  metric: "memory_bytes",
  entity: "all",
  view: "overview",
  collapsed: new Set(),
  selected: null,
  historyRequest: 0,
  historyLoading: false,
  historyError: false,
  hiddenSeries: new Set(),
  projectColors: new Map(),
  online: true,
};
const views = {
  overview: "Overview",
  projects: "Projects",
  containers: "Containers",
  codex: "Codex",
};

function projectColor(id) {
  if (!state.projectColors.has(id)) {
    const index = state.projectColors.size;
    state.projectColors.set(
      id,
      colors[index] || `hsl(${(index * 137.5) % 360} 70% 70%)`,
    );
  }
  return state.projectColors.get(id);
}

function bytes(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  if (value < 1024 ** 2) return `${Math.round(value / 1024)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(digits)} MiB`;
  return `${(value / 1024 ** 3).toFixed(digits)} GiB`;
}
const percent = (value) =>
  value === null || value === undefined ? "—" : `${value.toFixed(1)}%`;
const plural = (count, word) =>
  `${count} ${count === 1 ? word : word === "process" ? "processes" : word + "s"}`;
function ago(timestamp) {
  if (!timestamp) return "No recent activity";
  const seconds = Math.max(0, Date.now() / 1000 - timestamp);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    $("toast").hidden = true;
  }, 3500);
}
async function copy(text, success) {
  try {
    await navigator.clipboard.writeText(text);
    toast(success);
  } catch {
    // Clipboard access can be disabled on plain HTTP or by browser policy.
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.className = "detail-text";
    textarea.readOnly = true;
    const parent = $("detail-dialog").open
      ? $("detail-content")
      : $("warnings");
    parent.appendChild(textarea);
    textarea.focus();
    textarea.select();
    toast("Select and copy the report below.");
  }
}
async function api(path) {
  const response = await fetch(path, {
    cache: "no-store",
    signal: AbortSignal.timeout(12000),
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.error || `Monitor returned ${response.status}`);
  return data;
}
function metric(label, value, unit, caption, fill) {
  return `<article class="metric"><div class="metric-label">${label}</div><div class="metric-value numeric">${value}<small>${unit}</small></div><div class="metric-caption">${caption}</div>${fill !== undefined ? `<div class="mini-meter"><span style="width:${Math.max(0, Math.min(100, fill))}%"></span></div>` : ""}</article>`;
}
function renderMetrics() {
  const { system, totals } = state.snapshot;
  const used = system.memory_used / 1024 ** 3;
  $("metrics").innerHTML =
    metric(
      "Memory in use",
      used.toFixed(1),
      "GiB",
      `of ${(system.memory_total / 1024 ** 3).toFixed(1)} GiB <span class="muted">·</span> ${bytes(system.memory_total - system.memory_used)} available`,
      (system.memory_used / system.memory_total) * 100,
    ) +
    metric(
      "CPU usage",
      system.cpu_percent.toFixed(1),
      "%",
      `${system.cores} logical cores <span class="muted">·</span> whole machine`,
      system.cpu_percent,
    ) +
    metric(
      "Running worktrees",
      totals.running_worktrees,
      `<span class="muted">/ ${totals.worktree_count}</span>`,
      `<strong>${plural(totals.running_projects, "project")} active</strong> · ${plural(totals.process_count, "process")}`,
      totals.worktree_count
        ? (totals.running_worktrees / totals.worktree_count) * 100
        : 0,
    ) +
    metric(
      "Docker containers",
      state.snapshot.docker.available ? totals.container_count : "—",
      "",
      state.snapshot.docker.available
        ? `${totals.container_count - state.snapshot.unassigned_containers.length} mapped <span class="muted">·</span> ${state.snapshot.unassigned_containers.length} unassigned`
        : "Docker is currently unavailable",
      totals.container_count
        ? ((totals.container_count -
            state.snapshot.unassigned_containers.length) /
            totals.container_count) *
            100
        : 0,
    );
}
function renderAllocation() {
  const { projects, totals, codex } = state.snapshot;
  const visible = projects.filter((p) => p.memory_bytes > 0).slice(0, 6);
  if (codex.memory_bytes)
    visible.push({ name: "Codex processes", memory_bytes: codex.memory_bytes });
  const maximum = Math.max(1, ...visible.map((p) => p.memory_bytes));
  $("allocation").innerHTML = visible.length
    ? visible
        .map(
          (p) =>
            `<div class="allocation-row"><div class="allocation-label"><span class="allocation-color" style="background:${p.id ? projectColor(p.id) : "#91a0b2"}"></span><strong>${e(p.name)}</strong><span>${bytes(p.memory_bytes)}</span></div><div class="allocation-track"><span style="background:${p.id ? projectColor(p.id) : "#91a0b2"};width:${(p.memory_bytes / maximum) * 100}%"></span></div></div>`,
        )
        .join("")
    : '<p class="inline-note">No running project processes.</p>';
  $("allocation").title =
    `${bytes(totals.memory_bytes)} attributed to projects. Estimates may include shared memory more than once.`;
}
function matchingProjects() {
  const query = $("search").value.toLowerCase().trim();
  const running = $("running-only").checked;
  const projects = state.snapshot.projects
    .map((p) => ({
      ...p,
      worktrees: p.worktrees.filter(
        (w) =>
          (!running || w.running) &&
          (!query ||
            `${p.name} ${w.branch} ${w.path}`.toLowerCase().includes(query)),
      ),
    }))
    .filter((p) => p.worktrees.length);
  const sort = $("sort").value;
  return projects.sort((a, b) =>
    sort === "name"
      ? a.name.localeCompare(b.name)
      : (sort === "cpu"
          ? b.cpu_percent - a.cpu_percent
          : b.memory_bytes - a.memory_bytes) || a.name.localeCompare(b.name),
  );
}
function renderProjects() {
  const projects = matchingProjects();
  $("project-count").textContent = projects.length;
  $("projects").innerHTML = projects.length
    ? projects
        .map((p) => {
          const collapsed = state.collapsed.has(p.id);
          return `<article class="project-card"><div class="project-heading"><div class="project-identity"><div class="project-title">${e(p.name)}<span class="running-badge ${p.running_worktrees ? "" : "idle"}">${p.running_worktrees ? '<span class="status-dot"></span> Running' : "Idle"}</span></div><div class="project-meta">${p.worktrees.length} wt · ${p.process_count} proc · ${p.container_count} ctr</div></div><div class="project-totals numeric"><span class="project-memory" title="Project memory">${bytes(p.memory_bytes)}</span><span title="Project CPU">${percent(p.cpu_percent)}</span></div><button class="collapse-button" data-collapse="${e(p.id)}" aria-expanded="${!collapsed}" aria-controls="tree-${e(p.id)}" aria-label="${collapsed ? "Expand" : "Collapse"} ${e(p.name)}">${collapsed ? "+" : "−"}</button></div><div id="tree-${e(p.id)}" ${collapsed ? "hidden" : ""}><div class="tree-columns"><span>Worktree / branch</span><span class="services-heading">Proc / containers</span><span class="ports-heading">Ports</span><span class="align-right memory-heading">Memory</span><span class="align-right">CPU</span><span></span></div>${p.worktrees.map((w) => `<div class="worktree-row" data-worktree="${e(w.id)}" role="button" tabindex="0" aria-label="Inspect ${e(p.name)} ${e(w.branch)}"><div class="worktree-name"><span class="branch-icon" aria-hidden="true">⑂</span><div class="branch-label"><span class="branch-title" title="${e(w.path)}">${e(w.branch)}</span><span class="branch-info">${w.is_main ? "[main]" : ""}${w.codex_managed ? '<span class="codex-marker" title="Codex worktree">✳</span>' : ""}</span></div></div><span class="service-summary">${w.running ? `${w.processes.length} proc / ${w.containers.length} ctr` : "Idle"}</span><span class="row-ports" title="${e(w.ports.join(", "))}">${w.ports.length ? e(w.ports.join(", ")) : "—"}</span><span class="row-memory">${w.running ? bytes(w.memory_bytes) : "—"}</span><span class="row-cpu">${w.running ? percent(w.cpu_percent) : "—"}</span><span class="row-arrow" aria-hidden="true">↗</span></div>`).join("")}</div></article>`;
        })
        .join("")
    : `<div class="empty">${$("search").value ? "No projects match your search." : "No running projects."}<p>${$("running-only").checked ? "Turn off “Running only” to view idle worktrees." : "Add project directories in the local configuration."}</p></div>`;
}
function processTable(processes) {
  if (!processes.length)
    return '<div class="empty">No processes to show.</div>';
  return `<table class="host-table"><thead><tr><th scope="col">Process</th><th scope="col">Memory</th><th scope="col">CPU</th><th scope="col">PID</th></tr></thead><tbody>${processes.map((p) => `<tr><td>${e(p.name)}</td><td>${bytes(p.memory_bytes)}</td><td>${percent(p.cpu_percent)}</td><td>${p.pid}</td></tr>`).join("")}</tbody></table>`;
}
function renderHost() {
  $("host-processes").innerHTML = processTable(state.snapshot.other_processes);
}
function allContainers() {
  const result = [
    ...state.snapshot.unassigned_containers.map((c) => ({ ...c, owner: null })),
  ];
  for (const p of state.snapshot.projects)
    for (const w of p.worktrees)
      for (const c of w.containers)
        result.push({ ...c, owner: `${p.name} / ${w.branch}` });
  return result.sort((a, b) => (b.memory_bytes || 0) - (a.memory_bytes || 0));
}
function renderContainers() {
  const containers = allContainers();
  $("containers-section").innerHTML =
    `<div class="section-heading"><div><h2>Running containers <span class="count-pill">${containers.length}</span></h2><p>${e(state.snapshot.docker.status)}</p></div></div>` +
    (containers.length
      ? `<div class="container-grid">${containers.map((c) => `<article class="panel container-card"><h3>${e(c.name)}</h3><p class="image-name">${e(c.image)} <span class="muted">·</span> ${e(c.id.substring(0, 12))}</p><div class="container-stats"><div><strong>${bytes(c.memory_bytes)}</strong><small>Working set</small></div><div><strong>${percent(c.cpu_percent)}</strong><small>Machine CPU</small></div><div><strong>${c.pids ?? "—"}</strong><small>Tasks / threads</small></div></div><p class="inline-note">${c.ports.length ? `Published ports: ${c.ports.join(", ")}` : "No published ports"}${!c.stats_available ? " · Statistics unavailable" : ""}</p><div class="container-owner">${c.owner ? `<button class="text-button" data-worktree="${e(c.worktree_id)}">⑂ ${e(c.owner)}</button>` : "<span>Unassigned · ownership needs review</span>"}<span>${e(c.ownership)}</span></div></article>`).join("")}</div>`
      : `<div class="empty">${state.snapshot.docker.available ? "No containers are running." : "Docker is unavailable."}<p>${state.snapshot.docker.available ? "Containers will appear here when they start." : "The host monitor is still running. Check the configured Docker socket and permissions."}</p></div>`);
}
function renderCodex() {
  const c = state.snapshot.codex;
  $("codex-section").innerHTML =
    `<div class="codex-grid">${metric("Threads updated · 24h", c.available ? c.recent_threads : "—", "", `${c.thread_count} stored threads`)}${metric("Codex worktrees", c.managed_worktrees, "", "Existing Git checkouts")}${metric("Codex memory", (c.memory_bytes / 1024 ** 2).toFixed(0), "MiB", `${percent(c.cpu_percent)} CPU · ${plural(c.processes.length, "process")}`)}</div><article class="panel info-panel"><p>${e(c.status)} · refreshed every minute. Thread recency indicates metadata updates, not active agents. Conversation text and token costs are not collected.</p></article><div class="section-heading"><h2>Codex processes</h2></div><div class="panel">${processTable(c.processes)}</div>`;
}
function setView() {
  const requested = location.hash.substring(1);
  state.view = views[requested] ? requested : "overview";
  $("breadcrumb-current").textContent = views[state.view];
  document.title = `SM Manager · ${views[state.view]}`;
  $("dashboard").dataset.view = state.view;
  document.querySelectorAll("[data-view]").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === state.view);
    if (el.dataset.view === state.view) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
  $("overview-content").hidden = state.view !== "overview";
  $("project-section").hidden = !["overview", "projects"].includes(state.view);
  $("containers-section").hidden = state.view !== "containers";
  $("codex-section").hidden = state.view !== "codex";
  $("host-section").hidden = state.view !== "overview";
  $("metrics").hidden = state.view === "codex";
}
function historySources() {
  const projects = [...(state.snapshot?.projects || [])].sort((a, b) =>
    a.name.localeCompare(b.name),
  );
  if (state.entity === "all") {
    return [
      {
        entity: "projects",
        label: "All projects",
        color: "#dce3ed",
        total: true,
      },
      ...projects.map((p) => ({
        entity: `project:${p.id}`,
        label: p.name,
        color: projectColor(p.id),
      })),
    ];
  }
  if (state.entity === "host")
    return [{ entity: "host", label: "Whole machine", color: "#58a6ff" }];
  if (state.entity === "projects")
    return [
      {
        entity: "projects",
        label: "All projects",
        color: "#dce3ed",
        total: true,
      },
    ];
  for (const project of projects) {
    if (state.entity === `project:${project.id}`)
      return [
        {
          entity: state.entity,
          label: project.name,
          color: projectColor(project.id),
        },
      ];
    const worktree = project.worktrees.find(
      (w) => state.entity === `worktree:${w.id}`,
    );
    if (worktree)
      return [
        {
          entity: state.entity,
          label: `${project.name} / ${worktree.branch}`,
          color: projectColor(project.id),
        },
      ];
  }
  return [];
}
function renderChart() {
  const data = state.history;
  const current =
    data && data.entity === state.entity && data.hours === state.hours;
  const isMemory = state.metric === "memory_bytes";
  $("history-subtitle").textContent =
    `${state.hours}h · ${isMemory ? "memory" : "machine CPU %"}`;
  if (!current) {
    $("chart").innerHTML =
      `<div class="chart-placeholder">${state.historyError ? "History unavailable. Retrying…" : "Loading history…"}</div>`;
    $("chart-legend").innerHTML = "";
    $("chart").setAttribute("aria-label", "Resource history loading");
    $("history-note").textContent = state.historyError
      ? "History request failed"
      : "Loading samples…";
    return;
  }
  const visible = data.series.filter(
    (series) => !state.hiddenSeries.has(series.entity) && !series.error,
  );
  const points = visible.flatMap((series) => series.history.points);
  const format = (value) => (isMemory ? bytes(value) : percent(value));
  const legend = data.series
    .map((series) => {
      const enabled = !state.hiddenSeries.has(series.entity);
      const latest = series.history?.points.at(-1)?.[state.metric];
      const value = series.error
        ? "unavailable"
        : latest === undefined
          ? "no samples"
          : format(latest);
      return `<button class="legend-item" data-series-toggle="${e(series.entity)}" aria-pressed="${enabled}" ${series.error ? "disabled" : ""} title="${e(series.label)} · latest bucket average · click to ${enabled ? "hide" : "show"}"><span class="legend-swatch ${series.total ? "total" : ""}" style="--series-color:${series.color}"></span><span class="legend-name">${e(series.label)}</span><span class="legend-value">${value}</span></button>`;
    })
    .join("");
  const focusedSeries = $("chart-legend").contains(document.activeElement)
    ? document.activeElement.dataset.seriesToggle
    : null;
  if ($("chart-legend").innerHTML !== legend) {
    $("chart-legend").innerHTML = legend;
    if (focusedSeries)
      [...$("chart-legend").querySelectorAll("[data-series-toggle]")]
        .find((button) => button.dataset.seriesToggle === focusedSeries)
        ?.focus({ preventScroll: true });
  }
  const errors = data.series.filter((series) => series.error).length;
  const starts = data.series
    .map((series) => series.history?.started_at)
    .filter(Number.isFinite);
  $("history-note").textContent = errors
    ? `${errors} series unavailable · available series shown`
    : state.historyLoading
      ? "Updating samples…"
      : starts.length
        ? `Since ${new Date(Math.min(...starts) * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })} · bucket averages · gaps = no data`
        : "No samples in this range";
  if (!points.length) {
    const message = visible.length
      ? "No samples in this range."
      : errors === data.series.length
        ? "History unavailable. Retrying…"
        : "No series selected. Toggle a legend item to show it.";
    $("chart").innerHTML = `<div class="chart-placeholder">${message}</div>`;
    $("chart").setAttribute("aria-label", message);
    return;
  }
  const end = data.asOf;
  const start = end - data.hours * 3600;
  const width = Math.max(250, $("chart").clientWidth);
  const height = $("chart").clientHeight || 180;
  const left = 47,
    right = 10,
    top = 10,
    bottom = 25;
  const graphW = width - left - right,
    graphH = height - top - bottom;
  const max = Math.max(
    isMemory ? 64 * 1024 ** 2 : 1,
    ...points.map((point) => point[state.metric] * 1.12),
  );
  const step = isMemory
    ? max >= 1024 ** 3
      ? 256 * 1024 ** 2
      : 16 * 1024 ** 2
    : max < 5
      ? 0.5
      : 5;
  const ceiling = Math.ceil(max / step) * step;
  const x = (timestamp) =>
    left +
    Math.max(0, Math.min(1, (timestamp - start) / (end - start))) * graphW;
  const y = (value) => top + graphH - (value / ceiling) * graphH;
  const axisValue = (value) =>
    isMemory
      ? ceiling >= 1024 ** 3
        ? `${(value / 1024 ** 3).toFixed(1)}G`
        : `${(value / 1024 ** 2).toFixed(0)}M`
      : `${value.toFixed(ceiling < 5 ? 1 : 0)}%`;
  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map(
      (r) =>
        `<line class="chart-grid" x1="${left}" x2="${width - right}" y1="${y(ceiling * r)}" y2="${y(ceiling * r)}"/><text x="${left - 8}" y="${y(ceiling * r) + 3}" text-anchor="end">${axisValue(ceiling * r)}</text>`,
    )
    .join("");
  const ticks = width < 450 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1];
  const labels = ticks
    .map(
      (r) =>
        `<text x="${left + graphW * r}" y="${height - 4}" text-anchor="${r === 0 ? "start" : r === 1 ? "end" : "middle"}">${r === 1 ? "Now" : new Date((start + data.hours * 3600 * r) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}</text>`,
    )
    .join("");
  const lines = [...visible]
    .sort((a, b) => Number(!!a.total) - Number(!!b.total))
    .map((series) => {
      const segments = [];
      for (const point of series.history.points) {
        let segment = segments.at(-1);
        if (
          !segment ||
          point.timestamp - segment.at(-1).timestamp >
            series.history.bucket_seconds * 1.5
        ) {
          segment = [];
          segments.push(segment);
        }
        segment.push(point);
      }
      return `<g data-series="${e(series.entity)}"><title>${e(series.label)}</title>${segments
        .map((segment) => {
          const path = segment
            .map(
              (p, index) =>
                `${index ? "L" : "M"}${x(p.timestamp).toFixed(1)},${y(p[state.metric]).toFixed(1)}`,
            )
            .join(" ");
          return `<path d="${path}" fill="none" stroke="${series.color}" stroke-width="${series.total ? 2 : 1.6}" ${series.total ? 'stroke-dasharray="5 4"' : ""} stroke-linecap="round" stroke-linejoin="round"/>${segment.length === 1 ? `<circle cx="${x(segment[0].timestamp)}" cy="${y(segment[0][state.metric])}" r="2.5" fill="${series.color}"/>` : ""}`;
        })
        .join("")}</g>`;
    })
    .join("");
  $("chart").innerHTML =
    `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true">${grid}${lines}${labels}</svg>`;
  $("chart").setAttribute(
    "aria-label",
    `${isMemory ? "Memory" : "CPU"} history, last ${data.hours} hours. ${visible.length} visible series: ${visible.map((series) => series.label).join(", ")}.`,
  );
}
async function loadHistory() {
  if (!state.snapshot) return;
  const request = ++state.historyRequest;
  const hours = state.hours,
    entity = state.entity;
  const sources = historySources();
  state.historyLoading = true;
  state.historyError = false;
  renderChart();
  // Each series has its own failure state; one unavailable project must not hide the rest.
  const results = await Promise.allSettled(
    sources.map((source) =>
      api(
        `/api/history?hours=${hours}&entity=${encodeURIComponent(source.entity)}`,
      ),
    ),
  );
  if (request !== state.historyRequest) return;
  state.historyLoading = false;
  state.historyError =
    results.length > 0 &&
    results.every((result) => result.status === "rejected");
  state.history = {
    entity,
    hours,
    asOf: Date.now() / 1000,
    series: sources.map((source, index) =>
      results[index].status === "fulfilled"
        ? { ...source, history: results[index].value }
        : { ...source, error: true },
    ),
  };
  renderChart();
}
function findWorktree(id) {
  for (const p of state.snapshot.projects) {
    const w = p.worktrees.find((w) => w.id === id);
    if (w) return w;
  }
  return null;
}
function openDetail(id) {
  const w = findWorktree(id);
  if (!w) return;
  state.selected = id;
  $("detail-project").textContent = w.project_name;
  $("detail-title").textContent = w.branch;
  $("detail-content").innerHTML =
    `<p class="detail-path">${e(w.path)}</p><div class="detail-stats"><div><strong>${bytes(w.memory_bytes)}</strong><small>Resource memory estimate</small></div><div><strong>${percent(w.cpu_percent)}</strong><small>Whole-machine CPU</small></div><div><strong>${w.processes.length + w.containers.length}</strong><small>Processes + containers</small></div></div><div class="detail-links">${w.urls.map((link) => `<a class="button" href="${e(link.url)}" target="_blank" rel="noreferrer">Open app :${link.port} ↗</a>`).join("")}</div><p class="inline-note">${w.ports.length ? `Listening / published ports: ${w.ports.join(", ")}<br>` : ""}${w.recent_threads} Codex threads updated in this directory in 24h · ${e(ago(w.last_codex_activity))}<br>Snapshot taken ${new Date(state.snapshot.timestamp * 1000).toLocaleTimeString()} · reopen to refresh</p><div class="detail-section"><h3>Processes <span class="count-pill">${w.processes.length}</span></h3>${w.processes.length ? w.processes.map((p) => `<div class="detail-service"><div><strong>${e(p.name)}</strong><small>PID ${p.pid} · ${e(p.ownership)}${p.ports.length ? ` · :${p.ports.join(", :")}` : ""}</small><small>${e(p.cwd || "Directory unavailable")}</small></div><span>${bytes(p.memory_bytes)}<small>${percent(p.cpu_percent)} CPU</small></span></div>`).join("") : '<p class="inline-note">No host processes in this worktree.</p>'}</div><div class="detail-section"><h3>Containers <span class="count-pill">${w.containers.length}</span></h3>${w.containers.length ? w.containers.map((c) => `<div class="detail-service"><div><strong>${e(c.name)}</strong><small>${e(c.image)} · ${e(c.id.substring(0, 12))}</small><small>Matched by ${e(c.ownership)}</small></div><span>${bytes(c.memory_bytes)}<small>${percent(c.cpu_percent)} CPU</small></span></div>`).join("") : '<p class="inline-note">No containers mapped to this worktree.</p>'}</div><div class="cleanup-panel"><h3>Cleanup brief</h3><p>Copy a cleanup brief with this worktree's resource IDs. Your agent can review ownership and stop the app using its own lifecycle tools.</p><button class="button primary" id="copy-cleanup">Copy cleanup brief <span aria-hidden="true">↗</span></button><p class="inline-note">This button only copies text. Nothing is stopped or deleted.</p></div>`;
  if (!$("detail-dialog").open) $("detail-dialog").showModal();
  $("copy-cleanup").addEventListener("click", async () => {
    try {
      const plan = await api(`/api/cleanup-plan/${encodeURIComponent(id)}`);
      await copy(
        `${plan.prompt}\nMonitor: ${location.origin}\n\nResource inventory (revalidate before acting):\n${JSON.stringify(plan.resources, null, 2)}\n\nLifecycle launchers to review: ${plan.launchers_to_review.join(", ") || "Check the repository documentation"}`,
        "Cleanup brief copied. Ready for Codex.",
      );
    } catch (error) {
      toast(error.message);
    }
  });
}
function render() {
  const s = state.snapshot;
  $("loading").hidden = true;
  $("dashboard").hidden = false;
  $("machine-name").textContent = s.system.hostname;
  $("machine-platform").textContent = `Linux · ${s.system.cores} logical cores`;
  $("nav-projects").textContent = s.totals.running_projects;
  $("nav-containers").textContent = s.docker.available
    ? s.totals.container_count
    : "—";
  $("warnings").innerHTML = s.warnings
    .map((w) => `<div class="warning">${e(w)}</div>`)
    .join("");
  $("footer-meta").textContent =
    `Read only · every ${s.interval}s · ${s.collection_ms}ms to collect`;
  [...s.projects]
    .sort((a, b) => a.name.localeCompare(b.name))
    .forEach((project) => projectColor(project.id));
  renderMetrics();
  renderAllocation();
  renderProjects();
  renderHost();
  renderContainers();
  renderCodex();
  const options =
    '<option value="all">All — project comparison</option><option value="host">Whole machine</option><option value="projects">All projects · total</option>' +
    s.projects
      .map(
        (p) =>
          `<optgroup label="${e(p.name)}"><option value="project:${e(p.id)}">${e(p.name)} · all worktrees</option>${p.worktrees
            .filter((w) => w.running || state.entity === `worktree:${w.id}`)
            .map(
              (w) =>
                `<option value="worktree:${e(w.id)}">⑂ ${e(w.branch)}</option>`,
            )
            .join("")}</optgroup>`,
      )
      .join("");
  if ($("history-entity").innerHTML !== options) {
    $("history-entity").innerHTML = options;
    $("history-entity").value = state.entity;
    if (!$("history-entity").value) {
      state.entity = "all";
      $("history-entity").value = "all";
    }
  }
  updateConnection();
}
function updateConnection() {
  const s = state.snapshot;
  const age = s ? Date.now() / 1000 - s.timestamp : Infinity;
  const stale =
    !state.online || !s || s.stale || age > Math.max(30, s.interval * 3);
  $("connection").classList.toggle("stale", stale);
  $("connection").innerHTML =
    `<span class="status-dot"></span><span>${stale ? (s ? `Connection interrupted · last sample ${e(ago(s.timestamp))}` : "Connecting to monitor…") : `Live · updated ${e(ago(s.timestamp))}`}</span>`;
}
async function refresh() {
  try {
    state.snapshot = await api("/api/snapshot");
    state.online = true;
    render();
    await loadHistory();
  } catch (error) {
    state.online = false;
    updateConnection();
    if (!state.snapshot)
      $("loading").querySelector("p").textContent =
        `${error.message} Retrying shortly…`;
  } finally {
    setTimeout(refresh, (state.snapshot?.interval || 5) * 1000);
  }
}
$("search").addEventListener("input", () => state.snapshot && renderProjects());
$("running-only").addEventListener(
  "change",
  () => state.snapshot && renderProjects(),
);
$("sort").addEventListener("change", () => state.snapshot && renderProjects());
$("history-range").addEventListener("click", (event) => {
  const button = event.target.closest("[data-hours]");
  if (!button) return;
  state.hours = Number(button.dataset.hours);
  document.querySelectorAll("[data-hours]").forEach((el) => {
    el.classList.toggle("selected", el === button);
    el.setAttribute("aria-pressed", String(el === button));
  });
  loadHistory();
});
$("chart-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-metric]");
  if (!button) return;
  state.metric = button.dataset.metric;
  document.querySelectorAll("[data-metric]").forEach((el) => {
    el.classList.toggle("selected", el === button);
    el.setAttribute("aria-pressed", String(el === button));
  });
  renderChart();
});
$("history-entity").addEventListener("change", (event) => {
  state.entity = event.target.value;
  state.hiddenSeries.clear();
  loadHistory();
});
$("chart-legend").addEventListener("click", (event) => {
  const button = event.target.closest("[data-series-toggle]");
  if (!button) return;
  const entity = button.dataset.seriesToggle;
  state.hiddenSeries.has(entity)
    ? state.hiddenSeries.delete(entity)
    : state.hiddenSeries.add(entity);
  renderChart();
});
document.addEventListener("click", (event) => {
  const collapse = event.target.closest("[data-collapse]");
  if (collapse) {
    const id = collapse.dataset.collapse;
    state.collapsed.has(id)
      ? state.collapsed.delete(id)
      : state.collapsed.add(id);
    renderProjects();
  }
  const worktree = event.target.closest("[data-worktree]");
  if (worktree) openDetail(worktree.dataset.worktree);
});
document.addEventListener("keydown", (event) => {
  if (
    (event.key === "Enter" || event.key === " ") &&
    event.target.matches(".worktree-row")
  ) {
    event.preventDefault();
    openDetail(event.target.dataset.worktree);
  }
});
$("close-detail").addEventListener("click", () => $("detail-dialog").close());
$("detail-dialog").addEventListener("click", (event) => {
  if (event.target === $("detail-dialog")) {
    const r = event.target.getBoundingClientRect();
    if (
      event.clientX < r.left ||
      event.clientX > r.right ||
      event.clientY < r.top ||
      event.clientY > r.bottom
    )
      event.target.close();
  }
});
$("copy-report").addEventListener("click", async () => {
  if (!state.snapshot) {
    toast("Waiting for the first sample.");
    return;
  }
  const s = state.snapshot;
  const lines = [
    `SM Manager · ${s.system.hostname} · ${new Date(s.timestamp * 1000).toISOString()}`,
    `Monitor: ${location.origin}`,
    `CPU ${percent(s.system.cpu_percent)} | RAM ${bytes(s.system.memory_used)} / ${bytes(s.system.memory_total)}`,
    "",
  ];
  for (const p of s.projects)
    for (const w of p.worktrees)
      if (w.running)
        lines.push(
          `${p.name} / ${w.branch}: ${bytes(w.memory_bytes)}, CPU ${percent(w.cpu_percent)}, ${plural(w.processes.length, "process")}, ${plural(w.containers.length, "container")}\n  ${w.path}\n  Worktree ID: ${w.id}`,
        );
  if (s.unassigned_containers.length)
    lines.push(
      "\nUnassigned containers: " +
        s.unassigned_containers.map((c) => c.name).join(", "),
    );
  lines.push(
    `\nCodex: ${bytes(s.codex.memory_bytes)} RSS; ${s.codex.recent_threads} threads updated in 24h.`,
    "Memory totals are estimates (RSS + container working set); CPU is a share of the whole machine.",
    ...s.warnings,
  );
  await copy(lines.join("\n"), "System report copied.");
});
window.addEventListener("hashchange", setView);
new ResizeObserver(() => {
  if (!$("overview-content").hidden) renderChart();
}).observe($("chart"));
setInterval(updateConnection, 5000);
setView();
refresh();
