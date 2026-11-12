// Run the buildless UI helpers with a synthetic catalog and dialog.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const script = readFileSync(join(__dirname, "../sm_manager/static/dbs.js"), "utf8");

function db(database, path, primary = "/repo", status = "available") {
  return {
    database, engine: "PostgreSQL", host: "localhost", port: 5432, status,
    cloudbeaver_url: status === "available" ? "https://db.the-medo.com/cloudbeaver/open?id=" + database : null,
    owner: path ? { worktree_path: path, primary_path: primary, worktree: "feature", branch: "shared",
      repository: "repo", group: "family" } : null,
  };
}
function catalog(connections) {
  const state = { dbCatalog: { connections, stale: false, checked_at: new Date().toISOString() } };
  const elements = {
    "dbs-status": {}, "dbs-groups": {}, "db-choices": {},
    "db-chooser": { dataset: {}, showModal() { this.open = true; }, close() { this.open = false; } },
  };
  const ui = { state, e: String, $: (id) => elements[id], renderCatalogActions() {} };
  vm.runInNewContext(script, ui);
  return ui;
}

test("exact paths separate same-named branches, aggregate project databases, and exclude offline and unowned entries", () => {
  const ui = catalog([
    db("one", "/trees/a"), db("two", "/trees/a"), db("three", "/trees/a-more"),
    db("offline", "/trees/a", "/repo", "stopped"), db("other", "/trees/b", "/another/repo"), db("unowned", null),
  ]);
  assert.equal(ui.dbMatches("/trees/a", "worktree_path").length, 2);
  assert.equal(ui.dbMatches("/repo", "primary_path").length, 3);
  assert.equal(ui.dbAction("/trees", "worktree_path"), "");
  assert.equal(ui.dbAction("/trees/a/", "worktree_path"), "");
  assert.equal(ui.dbAction("/repo", "worktree_path"), "");
  assert.equal(ui.dbAction(null, "primary_path"), "");
  assert.match(ui.dbAction("/trees/a-more", "worktree_path"), /href="[^"]+id=three" target="_blank"/);
  assert.match(ui.dbAction("/trees/a", "worktree_path"), /<button[^>]+aria-haspopup="dialog"/);
  const chooser = ui.$("db-chooser");
  chooser.dataset = { path: "/repo", ownerKey: "primary_path" };
  ui.renderDbChooser();
  assert.ok(chooser.open);
  const choices = ui.$("db-choices").innerHTML;
  for (const name of ["one", "two", "three"]) assert.ok(choices.includes("feature / " + name));
  assert.doesNotMatch(choices, /offline|other|unowned/);
});

test("DBs page groups shared paths, separates Unassigned and explains configured entries", () => {
  const ui = catalog([db("one", "/tree"), db("two", "/tree"), db("declared", "/tree", "/repo", "configured"), db("solo", null)]);
  ui.renderDbsPage();
  const groups = ui.$("dbs-groups").innerHTML;
  assert.equal(groups.match(/class="db-worktree"/g).length, 1);
  assert.match(groups, /family[\s\S]*repo[\s\S]*feature[\s\S]*shared[\s\S]*one[\s\S]*two/);
  assert.match(groups, /Unassigned[\s\S]*solo/);
  assert.match(groups, /Configured databases.*could not verify/);
  assert.equal(groups.match(/target="_blank"/g).length, 3);
});

test("expiry revokes page, worktree and chooser launches while preserving the last check and statuses", () => {
  const ui = catalog([db("one", "/a"), db("two", "/a")]);
  const chooser = ui.$("db-chooser");
  chooser.dataset = { path: "/a", ownerKey: "worktree_path" };
  ui.renderDbChooser();
  ui.state.dbExpiresAt = Date.now() - 1;
  ui.expireDbCatalog();
  assert.equal(ui.dbAction("/a", "worktree_path"), "");
  assert.equal(chooser.open, false);
  assert.match(ui.$("dbs-status").textContent, /Stale catalog.*Last checked/);
  assert.match(ui.$("dbs-groups").innerHTML, /available/);
  assert.doesNotMatch(ui.$("dbs-groups").innerHTML, /href=/);
});

test("external environments have independent named profiles and never match a checkout", () => {
  const rows = ["dev", "prod"].map((env) => ({
    ...db("shared-db", null), group: env.toUpperCase(), name: env + "-example",
  }));
  const ui = catalog([...rows, db("local", null)]);
  ui.renderDbsPage();
  const cards = ui.$("dbs-groups").innerHTML.split("</article>");
  assert.match(cards[0], /<h2>DEV<\/h2>[\s\S]*dev-example[\s\S]*shared-db/);
  assert.match(cards[1], /<h2>PROD<\/h2>[\s\S]*prod-example[\s\S]*shared-db/);
  assert.match(cards[2], /Unassigned[\s\S]*local/);
  assert.equal(ui.dbAction("/repo", "primary_path"), "");
});
