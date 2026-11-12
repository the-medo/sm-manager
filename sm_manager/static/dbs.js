const dbIcon = '<img src="/db.svg" alt="" width="16" height="16">';

function dbMatches(path, ownerKey) {
  if (!path || !state.dbCatalog || state.dbCatalog.stale) return [];
  return state.dbCatalog.connections.filter((db) =>
    db.owner?.[ownerKey] === path && db.status === "available" && db.cloudbeaver_url
  );
}

function dbLink(db, showName = false) {
  if (state.dbCatalog?.stale || db.status !== "available" || !db.cloudbeaver_url) return "";
  const name = [db.owner?.worktree, db.database].filter(Boolean).join(" / ");
  const label = e(`Open in CloudBeaver — ${name}`);
  return `<a class="db-link ${showName ? "db-choice" : "checkout-icon"}" href="${e(db.cloudbeaver_url)}" target="_blank" rel="noopener noreferrer" title="${label}" aria-label="${label}">${dbIcon}${showName ? e(name) : ""}</a>`;
}

function dbAction(path, ownerKey) {
  const matches = dbMatches(path, ownerKey);
  if (!matches.length) return "";
  if (matches.length === 1) return dbLink(matches[0]);
  const label = e(`Open in CloudBeaver — choose from ${matches.length} databases`);
  return `<button type="button" class="checkout-icon" data-db-path="${e(path)}" data-owner-key="${ownerKey}" aria-haspopup="dialog" aria-controls="db-chooser" title="${label}" aria-label="${label}">${dbIcon}</button>`;
}

function renderDbChooser() {
  const chooser = $("db-chooser");
  const matches = dbMatches(chooser.dataset.path, chooser.dataset.ownerKey);
  if (!matches.length) {
    $("db-choices").innerHTML = "";
    renderDbChooser.html = "";
    if (chooser.open) chooser.close();
    return;
  }
  const html = matches.map((db) => dbLink(db, true)).join("");
  if (html !== renderDbChooser.html) {
    $("db-choices").innerHTML = html;
    renderDbChooser.html = html;
  }
  if (!chooser.open) chooser.showModal();
}

function groupedBy(items, key) {
  const groups = new Map();
  for (const item of items) {
    const value = key(item);
    if (!groups.has(value)) groups.set(value, []);
    groups.get(value).push(item);
  }
  return [...groups.values()];
}

function dbRows(dbs) {
  return `<ul class="db-rows">${dbs.map((db) => {
    const host = db.host?.includes(":") && !db.host.startsWith("[") ? `[${db.host}]` : db.host;
    const endpoint = [host, db.port].filter((part) => part != null).join(":");
    return `<li><strong>${e(db.database)}</strong><span>${e(db.engine)}</span><code>${e(endpoint || "Endpoint unavailable")}</code>
      <span class="db-status" data-status="${e(db.status)}">${e(db.status)}</span>${dbLink(db)}</li>`;
  }).join("")}</ul>`;
}

function dbGroups(connections) {
  const owned = connections.filter((db) => db.owner);
  const groups = groupedBy(owned, (db) => db.owner.group);
  const panels = groups.map((group) => {
    const repos = groupedBy(group, (db) => db.owner.primary_path);
    return `<article class="panel zed-family"><div class="panel-heading"><h2>${e(group[0].owner.group)}</h2></div>
      ${repos.map((repo) => `<section class="zed-repo"><h3>${e(repo[0].owner.repository)}</h3>
        ${groupedBy(repo, (db) => db.owner.worktree_path).map((tree) => {
          const owner = tree[0].owner;
          return `<section class="db-worktree"><h4 title="${e(owner.worktree_path)}">${e(owner.worktree)} <span class="zed-revision">${e(owner.branch || "Branch unavailable")}</span></h4>${dbRows(tree)}</section>`;
        }).join("")}</section>`).join("")}</article>`;
  });
  const unassigned = connections.filter((db) => !db.owner);
  if (unassigned.length) panels.push(`<article class="panel zed-family"><div class="panel-heading"><h2>Unassigned</h2></div><div class="zed-repo">${dbRows(unassigned)}</div></article>`);
  return panels.join("");
}

function renderDbsPage() {
  const catalog = state.dbCatalog;
  const status = $("dbs-status");
  status.className = catalog && !catalog.stale ? "section-note" : "warning";
  if (catalog) {
    const checked = new Date(catalog.checked_at).toLocaleString();
    status.textContent = `${catalog.stale ? "Stale catalog · " : ""}Last checked ${checked}${catalog.stale ? " · Waiting for a fresh catalog to enable CloudBeaver links." : ""}`;
  } else {
    status.textContent = `${state.dbError} Retrying in one minute.`;
  }
  let html = "";
  if (catalog) {
    html = dbGroups(catalog.connections);
    if (!html && !catalog.stale) html = '<div class="empty">No databases in the catalog.</div>';
    if (catalog.connections.some((db) => db.status === "configured"))
      html += '<p class="section-note">Configured databases are declared by a worktree. The last check could not verify them.</p>';
  }
  if (html !== renderDbsPage.html) {
    $("dbs-groups").innerHTML = html;
    renderDbsPage.html = html;
  }
  if ($("db-chooser").open) renderDbChooser();
}

function expireDbCatalog() {
  if (!state.dbCatalog || state.dbCatalog.stale || Date.now() < state.dbExpiresAt) return;
  state.dbCatalog.stale = true;
  renderDbsPage();
  renderCatalogActions();
}

async function refreshDbs() {
  const requestedAt = Date.now();
  try {
    const catalog = await api("/api/dbs");
    state.dbExpiresAt = requestedAt + catalog.fresh_for_seconds * 1000;
    state.dbCatalog = catalog;
    const remaining = state.dbExpiresAt - Date.now();
    catalog.stale ||= remaining <= 0;
    clearTimeout(refreshDbs.expiry);
    if (!catalog.stale) refreshDbs.expiry = setTimeout(expireDbCatalog, Math.ceil(remaining));
  } catch (error) {
    state.dbCatalog = null;
    state.dbError = error.message;
  }
  renderDbsPage();
}
