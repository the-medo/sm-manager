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
const colors = ["#7d9d64", "#a8bd8b", "#c5b69a", "#9eb5ad", "#c4cba5"];
const state = {
  snapshot: null,
  history: null,
  hours: 24,
  metric: "memory_bytes",
  entity: "host",
  view: "overview",
  collapsed: new Set(),
  selected: null,
  historyRequest: 0,
  online: true,
};
const views = {
  overview: [
    "Overview",
    "Your machine, at a glance.",
    "Every project, worktree, and container. One place to see what's running.",
  ],
  projects: [
    "Projects",
    "A place for every project.",
    "From the main checkout to that one experimental branch.",
  ],
  containers: [
    "Containers",
    "A little order for Docker.",
    "Running containers and the worktrees they belong to.",
  ],
  codex: [
    "Codex activity",
    "Behind the agent.",
    "Local Codex activity and the resources its processes use.",
  ],
};

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
function metric(label, value, unit, caption, icon, fill) {
  return `<article class="metric"><div class="metric-label">${label}<span class="metric-icon" aria-hidden="true">${icon}</span></div><div class="metric-value numeric">${value}<small>${unit}</small></div><div class="metric-caption">${caption}</div>${fill !== undefined ? `<div class="mini-meter"><span style="width:${Math.max(0, Math.min(100, fill))}%"></span></div>` : ""}</article>`;
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
      "▥",
      (system.memory_used / system.memory_total) * 100,
    ) +
    metric(
      "CPU usage",
      system.cpu_percent.toFixed(1),
      "%",
      `${system.cores} logical cores <span class="muted">·</span> whole machine`,
      "⌘",
      system.cpu_percent,
    ) +
    metric(
      "Running worktrees",
      totals.running_worktrees,
      `<span class="muted">/ ${totals.worktree_count}</span>`,
      `<strong>${plural(totals.running_projects, "project")} active</strong> across your workspace`,
      "⑂",
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
      "▤",
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
  const visible = projects.filter((p) => p.memory_bytes > 0).slice(0, 4);
  if (codex.memory_bytes)
    visible.push({ name: "Codex processes", memory_bytes: codex.memory_bytes });
  const maximum = Math.max(1, ...visible.map((p) => p.memory_bytes));
  $("allocation").innerHTML = visible.length
    ? visible
        .map(
          (p, i) =>
            `<div class="allocation-row"><div class="allocation-label"><span class="allocation-color" style="background:${colors[i % colors.length]}"></span><strong>${e(p.name)}</strong><span>${bytes(p.memory_bytes)}</span></div><div class="allocation-track"><span style="background:${colors[i % colors.length]};width:${(p.memory_bytes / maximum) * 100}%"></span></div></div>`,
        )
        .join("")
    : '<p class="allocation-empty">No project processes are running.<br>Your workspace has some breathing room.</p>';
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
          return `<article class="project-card"><div class="project-heading"><div class="project-monogram">${e(p.name.substring(0, 1).toUpperCase())}</div><div class="project-identity"><div class="project-title">${e(p.name)}<span class="running-badge ${p.running_worktrees ? "" : "idle"}">${p.running_worktrees ? '<span class="status-dot"></span> Running' : "Idle"}</span></div><div class="project-meta">${plural(p.worktrees.length, "worktree")} shown <span class="muted">·</span> ${plural(p.process_count, "process")} <span class="muted">·</span> ${plural(p.container_count, "container")}</div></div><div class="project-totals numeric"><span>${bytes(p.memory_bytes)}<small>Memory</small></span><span>${percent(p.cpu_percent)}<small>CPU</small></span></div><button class="collapse-button" data-collapse="${e(p.id)}" aria-expanded="${!collapsed}" aria-controls="tree-${e(p.id)}" aria-label="${collapsed ? "Expand" : "Collapse"} ${e(p.name)}">${collapsed ? "+" : "−"}</button></div><div id="tree-${e(p.id)}" ${collapsed ? "hidden" : ""}><div class="tree-columns"><span>Worktree / branch</span><span>Services</span><span>Memory</span><span>CPU</span><span></span></div>${p.worktrees.map((w) => `<div class="worktree-row" data-worktree="${e(w.id)}" role="button" tabindex="0" aria-label="Inspect ${e(p.name)} ${e(w.branch)}"><div class="worktree-name"><span class="branch-icon" aria-hidden="true">⑂</span><div style="min-width:0"><span class="branch-title" title="${e(w.path)}">${e(w.branch)}</span><span class="branch-info">${w.is_main ? "Main checkout" : "Worktree"}${w.codex_managed ? '<span>·</span><span class="codex-marker">✳ Codex</span>' : ""}${!w.running ? "<span>· Idle</span>" : ""}</span></div></div><span class="service-summary">${w.processes.length ? plural(w.processes.length, "process") : ""}${w.processes.length && w.containers.length ? " · " : ""}${w.containers.length ? plural(w.containers.length, "container") : ""}${!w.running ? "No running services" : ""}</span><span class="row-memory">${w.running ? bytes(w.memory_bytes) : "—"}</span><span class="row-cpu">${w.running ? percent(w.cpu_percent) : "—"}</span><span class="row-arrow" aria-hidden="true">↗</span></div>`).join("")}</div></article>`;
        })
        .join("")
    : `<div class="empty">${$("search").value ? "No projects match your search." : "Nothing running here right now."}<p>${$("running-only").checked ? "Turn off “Running only” to browse all discovered worktrees." : "Add project directories to your local configuration to discover more."}</p></div>`;
}
function processTable(processes) {
  if (!processes.length)
    return '<div class="empty">No processes to show.</div>';
  return `<table class="host-table"><thead><tr><th scope="col">Process</th><th scope="col">Memory</th><th scope="col">CPU</th><th scope="col">PID</th></tr></thead><tbody>${processes.map((p) => `<tr><td>${e(p.name)}</td><td>${bytes(p.memory_bytes)}</td><td>${percent(p.cpu_percent)}</td><td>${p.pid}</td></tr>`).join("")}</tbody></table>`;
}
function renderHost() {
  $("host-processes").innerHTML = processTable(
    state.snapshot.other_processes.slice(0, 5),
  );
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
    `<div class="codex-grid">${metric("Threads updated · 24h", c.available ? c.recent_threads : "—", "", c.available ? `${c.thread_count} threads in local metadata` : "Metadata unavailable", "✳")}${metric("Codex worktrees", c.managed_worktrees, "", "Existing Git checkouts in the Codex directory", "⑂")}${metric("Codex process memory", (c.memory_bytes / 1024 ** 2).toFixed(0), "MiB", `${percent(c.cpu_percent)} of machine CPU · ${plural(c.processes.length, "process")}`, "▥")}</div><article class="panel info-panel"><h2>A small window into local activity</h2><p>Thread counts use the last update time in Codex's local metadata. An updated thread doesn't necessarily mean an agent is running. Worktree ownership comes from Git and local paths.</p><p>This monitor reads thread directories, update times, and worktree attachments. Conversation text, credentials, and prompts are not collected. Token spend and billing aren't tracked.</p><p class="inline-note">${e(c.status)} · metadata refreshes once a minute</p></article><div class="section-heading"><div><h2>Codex processes</h2><p>Agent runtime overhead, shown separately from project services.</p></div></div><div class="panel">${processTable(c.processes)}</div>`;
}
function setView() {
  const requested = location.hash.substring(1);
  state.view = views[requested] ? requested : "overview";
  const [name, title, description] = views[state.view];
  $("breadcrumb-current").textContent = name;
  $("page-title").textContent = title;
  $("page-description").textContent = description;
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
function renderChart() {
  const data = state.history;
  if (!data) {
    $("chart").innerHTML =
      '<div class="chart-placeholder">Loading history…</div>';
    return;
  }
  const points = data.points;
  if (!points.length) {
    $("chart").innerHTML =
      '<div class="chart-placeholder">The first samples will appear here shortly.</div>';
    return;
  }
  const isMemory = state.metric === "memory_bytes";
  const end = Date.now() / 1000;
  const start = end - state.hours * 3600;
  const width = Math.max(240, $("chart").clientWidth - 34),
    height = 172,
    left = 47,
    right = 15,
    top = 12,
    bottom = 26;
  const graphW = width - left - right,
    graphH = height - top - bottom;
  const max = isMemory
    ? Math.max(64 * 1024 ** 2, ...points.map((p) => p.memory_bytes * 1.25))
    : Math.max(10, ...points.map((p) => p.cpu_percent * 1.25));
  const memoryStep = max >= 1024 ** 3 ? 512 * 1024 ** 2 : 16 * 1024 ** 2;
  const ceiling = isMemory
    ? Math.ceil(max / memoryStep) * memoryStep
    : Math.min(100, Math.ceil(max / 10) * 10);
  const x = (timestamp) =>
    left +
    Math.max(0, Math.min(1, (timestamp - start) / (end - start))) * graphW;
  const y = (value) => top + graphH - Math.min(1, value / ceiling) * graphH;
  const segments = [];
  for (const point of points) {
    let segment = segments[segments.length - 1];
    if (
      !segment ||
      point.timestamp - segment[segment.length - 1].timestamp >
        data.bucket_seconds * 2.5
    ) {
      segment = [];
      segments.push(segment);
    }
    segment.push(point);
  }
  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map(
      (r) =>
        `<line x1="${left}" x2="${width - right}" y1="${y(ceiling * r)}" y2="${y(ceiling * r)}" stroke="#edf1e6" stroke-dasharray="3 4"/><text x="${left - 9}" y="${y(ceiling * r) + 3}" text-anchor="end">${isMemory ? (ceiling >= 1024 ** 3 ? `${((ceiling * r) / 1024 ** 3).toFixed(1)}G` : `${((ceiling * r) / 1024 ** 2).toFixed(0)}M`) : `${(ceiling * r).toFixed(0)}%`}</text>`,
    )
    .join("");
  const labels = [0, 0.25, 0.5, 0.75, 1]
    .map(
      (r) =>
        `<text x="${left + graphW * r}" y="${height - 4}" text-anchor="${r === 0 ? "start" : r === 1 ? "end" : "middle"}">${r === 1 ? "Now" : new Date((start + state.hours * 3600 * r) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</text>`,
    )
    .join("");
  const color = isMemory ? "#8daa6f" : "#baa685";
  const lines = segments
    .map((segment) => {
      const path = segment
        .map(
          (p, i) =>
            `${i === 0 ? "M" : "L"}${x(p.timestamp).toFixed(1)},${y(p[state.metric]).toFixed(1)}`,
        )
        .join(" ");
      const first = segment[0],
        last = segment[segment.length - 1];
      return `<path d="${path} L${x(last.timestamp)},${y(0)} L${x(first.timestamp)},${y(0)} Z" fill="url(#chart-gradient)"/><path d="${path}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>${segment.length === 1 ? `<circle cx="${x(first.timestamp)}" cy="${y(first[state.metric])}" r="3" fill="${color}"/>` : ""}`;
    })
    .join("");
  $("chart").innerHTML =
    `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="chart-gradient" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="${color}" stop-opacity=".22"/><stop offset="100%" stop-color="${color}" stop-opacity=".02"/></linearGradient></defs>${grid}${lines}${labels}</svg>`;
  $("chart").setAttribute(
    "aria-label",
    `${isMemory ? "Memory" : "CPU"} history for the last ${state.hours} hours. ${points.length} collected points. Latest: ${isMemory ? bytes(points.at(-1).memory_bytes) : percent(points.at(-1).cpu_percent)}.`,
  );
  $("history-note").textContent =
    `Collecting since ${new Date(data.started_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} · gaps mean no samples`;
  $("history-subtitle").textContent =
    `${state.hours}-hour view · ${isMemory ? "memory in use" : "share of total CPU capacity"}`;
}
async function loadHistory() {
  const request = ++state.historyRequest;
  try {
    const history = await api(
      `/api/history?hours=${state.hours}&entity=${encodeURIComponent(state.entity)}`,
    );
    if (request !== state.historyRequest) return;
    state.history = history;
    renderChart();
  } catch {
    if (request === state.historyRequest) {
      $("history-note").textContent =
        "History unavailable · retrying on the next refresh";
      if (!state.history)
        $("chart").innerHTML =
          '<div class="chart-placeholder">History is temporarily unavailable.</div>';
    }
  }
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
    `<p class="detail-path">${e(w.path)}</p><div class="detail-stats"><div><strong>${bytes(w.memory_bytes)}</strong><small>Resource memory estimate</small></div><div><strong>${percent(w.cpu_percent)}</strong><small>Whole-machine CPU</small></div><div><strong>${w.processes.length + w.containers.length}</strong><small>Processes + containers</small></div></div><div class="detail-links">${w.urls.map((link) => `<a class="button" href="${e(link.url)}" target="_blank" rel="noreferrer">Open app :${link.port} ↗</a>`).join("")}</div><p class="inline-note">${w.ports.length ? `Listening / published ports: ${w.ports.join(", ")}<br>` : ""}${w.recent_threads} Codex threads updated in this directory in 24h · ${e(ago(w.last_codex_activity))}<br>Snapshot taken ${new Date(state.snapshot.timestamp * 1000).toLocaleTimeString()} · reopen to refresh</p><div class="detail-section"><h3>Processes <span class="count-pill">${w.processes.length}</span></h3>${w.processes.length ? w.processes.map((p) => `<div class="detail-service"><div><strong>${e(p.name)}</strong><small>PID ${p.pid} · ${e(p.ownership)}${p.ports.length ? ` · :${p.ports.join(", :")}` : ""}</small><small>${e(p.cwd || "Directory unavailable")}</small></div><span>${bytes(p.memory_bytes)}<small>${percent(p.cpu_percent)} CPU</small></span></div>`).join("") : '<p class="inline-note">No host processes in this worktree.</p>'}</div><div class="detail-section"><h3>Containers <span class="count-pill">${w.containers.length}</span></h3>${w.containers.length ? w.containers.map((c) => `<div class="detail-service"><div><strong>${e(c.name)}</strong><small>${e(c.image)} · ${e(c.id.substring(0, 12))}</small><small>Matched by ${e(c.ownership)}</small></div><span>${bytes(c.memory_bytes)}<small>${percent(c.cpu_percent)} CPU</small></span></div>`).join("") : '<p class="inline-note">No containers mapped to this worktree.</p>'}</div><div class="cleanup-panel"><h3>A tidy handoff to your agent</h3><p>Copy a cleanup brief with this worktree's resource IDs. Your agent can review ownership and stop the app using its own lifecycle tools.</p><button class="button primary" id="copy-cleanup">Copy cleanup brief <span aria-hidden="true">↗</span></button><p class="inline-note">This button only copies text. Nothing is stopped or deleted.</p></div>`;
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
  renderMetrics();
  renderAllocation();
  renderProjects();
  renderHost();
  renderContainers();
  renderCodex();
  const options =
    '<option value="host">Whole machine</option><option value="projects">All projects</option>' +
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
      state.entity = "host";
      $("history-entity").value = "host";
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
  loadHistory();
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
