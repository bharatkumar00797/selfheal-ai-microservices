/* SelfHeal dashboard: textContent only; avoid HTML sinks. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    config: null,
    runs: [],
    currentId: null,
    cursor: 0,
    events: [],
    services: [],
    pending: [],
    summary: null,
    detail: null,
    pollTimer: null,
    tab: "topology",
  };

  function apiKey() {
    return sessionStorage.getItem("selfheal_api_key") || "";
  }

  function setApiKey(value) {
    if (value) sessionStorage.setItem("selfheal_api_key", value);
    else sessionStorage.removeItem("selfheal_api_key");
  }

  async function api(method, path, body) {
    const headers = { Accept: "application/json" };
    const key = apiKey();
    if (key) headers["X-API-Key"] = key;
    let payload;
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    const res = await fetch(path, { method, headers, body: payload });
    const ctype = res.headers.get("content-type") || "";
    const data = ctype.includes("json") ? await res.json() : await res.text();
    if (!res.ok) {
      const detail = data && data.detail ? data.detail : res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return { status: res.status, data };
  }

  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  function text(el, value) {
    el.textContent = value == null ? "" : String(value);
  }

  function pillClass(stateName) {
    if (stateName === "finished" || stateName === "resolved") return "pill ok";
    if (stateName === "error" || stateName === "escalated") return "pill bad";
    if (stateName === "running" || stateName === "awaiting_approval" || stateName === "verifying")
      return "pill warn";
    return "pill";
  }

  function healthOf(svc) {
    const m = svc.metrics;
    if (!m) return "healthy";
    if (m.error_rate >= 0.2 || m.healthy_replicas === 0) return "unhealthy";
    if (m.error_rate >= 0.05 || m.memory_pct >= 85 || m.p95_latency_ms > 400) return "degraded";
    return "healthy";
  }

  function sparkline(svg, series, key) {
    clear(svg);
    svg.setAttribute("viewBox", "0 0 100 36");
    svg.setAttribute("preserveAspectRatio", "none");
    if (!series.length) return;
    const values = series.map((s) => Number(s[key]) || 0);
    const max = Math.max(...values, 0.001);
    const step = values.length > 1 ? 100 / (values.length - 1) : 100;
    const pts = values
      .map((v, i) => `${(i * step).toFixed(1)},${(36 - (v / max) * 32 - 2).toFixed(1)}`)
      .join(" ");
    const poly = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    poly.setAttribute("fill", "none");
    poly.setAttribute("stroke", "#3d9cf0");
    poly.setAttribute("stroke-width", "1.5");
    poly.setAttribute("points", pts);
    svg.appendChild(poly);
  }

  function parseHash() {
    const hash = location.hash.replace(/^#/, "");
    const params = new URLSearchParams(hash);
    return { run: params.get("run"), tab: params.get("tab") };
  }

  function writeHash() {
    const parts = [];
    if (state.currentId) parts.push("run=" + encodeURIComponent(state.currentId));
    if (state.tab && state.tab !== "topology") parts.push("tab=" + encodeURIComponent(state.tab));
    const next = parts.length ? "#" + parts.join("&") : "";
    if (location.hash !== next) history.replaceState(null, "", location.pathname + next);
  }

  function setTab(name) {
    state.tab = name;
    document.querySelectorAll(".tabs [role=tab]").forEach((btn) => {
      btn.setAttribute("aria-selected", btn.dataset.tab === name ? "true" : "false");
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.dataset.panel === name);
    });
    writeHash();
  }

  async function loadConfig() {
    const { data } = await api("GET", "/api/config");
    state.config = data;
    text($("version"), "v" + data.version);
    const badge = $("mode-badge");
    text(badge, data.access_mode);
    badge.className = "pill " + (data.access_mode === "public-demo" ? "warn" : "ok");
    $("key-row").hidden = !data.auth_required;
    if (apiKey()) $("api-key").value = apiKey();

    const scen = $("scenario");
    clear(scen);
    const { data: list } = await api("GET", "/api/scenarios");
    list.scenarios.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = s.title;
      opt.dataset.desc = s.description;
      scen.appendChild(opt);
    });
    text($("scenario-desc"), list.scenarios[0] ? list.scenarios[0].description : "");

    const prov = $("provider");
    clear(prov);
    data.providers.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      if (p === data.default_provider) opt.selected = true;
      prov.appendChild(opt);
    });
  }

  async function refreshRuns() {
    const { data } = await api("GET", "/api/runs?limit=25");
    state.runs = data.runs;
    const list = $("run-list");
    clear(list);
    $("list-empty").hidden = data.runs.length > 0;
    data.runs.forEach((r) => {
      const li = document.createElement("li");
      if (r.id === state.currentId) li.classList.add("active");
      const title = document.createElement("div");
      title.className = "title";
      title.textContent = r.title;
      const meta = document.createElement("div");
      meta.className = "muted small";
      meta.textContent =
        r.state +
        (r.pending_approvals ? " · " + r.pending_approvals + " pending" : "") +
        " · " +
        r.provider;
      li.appendChild(title);
      li.appendChild(meta);
      li.addEventListener("click", () => selectRun(r.id));
      list.appendChild(li);
    });
  }

  function stopPoll() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function selectRun(id) {
    stopPoll();
    state.currentId = id;
    state.cursor = 0;
    state.events = [];
    state.services = [];
    state.pending = [];
    writeHash();
    $("empty-state").hidden = true;
    $("run-view").hidden = false;
    await refreshRuns();
    await pollOnce();
  }

  async function pollOnce() {
    if (!state.currentId) return;
    try {
      const { data: detail } = await api("GET", "/api/runs/" + state.currentId);
      state.detail = detail;
      renderHead(detail);
      const { data: ev } = await api(
        "GET",
        "/api/runs/" + state.currentId + "/events?since=" + state.cursor
      );
      if (ev.events.length) {
        state.events = state.events.concat(ev.events);
        state.cursor = ev.next_cursor;
      }
      state.services = ev.services || state.services;
      state.pending = ev.pending_approvals || [];
      state.summary = ev.summary;
      renderTimeline();
      renderTopology();
      renderIncidents(detail.incidents || []);
      renderApprovals();
      $("spinner").hidden = ev.done;
      if (!ev.done) state.pollTimer = setTimeout(pollOnce, 700);
      else await refreshRuns();
    } catch (err) {
      text($("run-error"), err.message || String(err));
    }
  }

  function renderHead(detail) {
    text($("run-title"), detail.title);
    text($("run-id"), detail.id);
    text($("run-ticks"), detail.ticks == null ? "—" : detail.ticks);
    const st = $("run-state");
    text(st, detail.state);
    st.className = pillClass(detail.state);
    text($("run-provider"), detail.provider);
    const s = detail.summary || {};
    text(
      $("run-summary"),
      "resolved " +
        (s.resolved || 0) +
        " · escalated " +
        (s.escalated || 0) +
        " · open " +
        (s.open || 0) +
        (detail.passed == null ? "" : detail.passed ? " · PASS" : " · check outcome")
    );
    text($("run-error"), detail.error || "");
  }

  function renderTimeline() {
    const ol = $("timeline");
    clear(ol);
    state.events.forEach((e) => {
      const li = document.createElement("li");
      const tick = document.createElement("span");
      tick.className = "tick";
      tick.textContent = "t+" + e.tick + "m";
      const svc = document.createElement("span");
      svc.className = "svc-name";
      svc.textContent = e.service;
      const msg = document.createElement("span");
      msg.textContent = "[" + e.type + "] " + e.message;
      li.appendChild(tick);
      li.appendChild(svc);
      li.appendChild(msg);
      ol.appendChild(li);
    });
  }

  function renderTopology() {
    const root = $("topology");
    clear(root);
    (state.services || []).forEach((svc) => {
      const card = document.createElement("div");
      card.className = "svc " + healthOf(svc);
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = svc.name;
      const meta = document.createElement("div");
      meta.className = "meta";
      const bits = [
        "v" + svc.version,
        svc.replicas + " replicas",
        svc.protected ? "protected" : null,
        svc.external ? "external" : null,
      ].filter(Boolean);
      if (svc.metrics) {
        bits.push("err " + (svc.metrics.error_rate * 100).toFixed(1) + "%");
        bits.push("p95 " + Math.round(svc.metrics.p95_latency_ms) + "ms");
      }
      meta.textContent = bits.join(" · ");
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.classList.add("spark");
      sparkline(svg, svc.sparkline || [], "error_rate");
      card.appendChild(name);
      card.appendChild(meta);
      card.appendChild(svg);
      root.appendChild(card);
    });
  }

  function renderIncidents(incidents) {
    const root = $("incidents");
    clear(root);
    if (!incidents.length) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "No incidents yet.";
      root.appendChild(p);
      return;
    }
    incidents.forEach((inc) => {
      const card = document.createElement("div");
      card.className = "inc";
      const row = document.createElement("div");
      row.className = "row";
      const title = document.createElement("strong");
      title.textContent = inc.service;
      const status = document.createElement("span");
      status.className = pillClass(inc.status);
      status.textContent = inc.status;
      row.appendChild(title);
      row.appendChild(status);
      const body = document.createElement("p");
      body.className = "muted small";
      body.textContent =
        (inc.root_cause ? "cause " + inc.root_cause + " · " : "") +
        "attempts " +
        inc.attempts +
        (inc.mttr_minutes != null ? " · MTTR " + inc.mttr_minutes + "m" : "");
      card.appendChild(row);
      card.appendChild(body);
      if (inc.actions && inc.actions.length) {
        const ul = document.createElement("ul");
        inc.actions.forEach((a) => {
          const li = document.createElement("li");
          li.textContent =
            a.action +
            " → " +
            a.result +
            (a.approved_by ? " (approved by " + a.approved_by + ")" : "");
          ul.appendChild(li);
        });
        card.appendChild(ul);
      }
      root.appendChild(card);
    });
  }

  function renderApprovals() {
    const root = $("approvals");
    clear(root);
    const count = $("pending-count");
    if (state.pending.length) {
      count.hidden = false;
      text(count, String(state.pending.length));
    } else {
      count.hidden = true;
    }
    if (!state.pending.length) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "No pending approvals.";
      root.appendChild(p);
      return;
    }
    state.pending.forEach((p) => {
      const card = document.createElement("div");
      card.className = "appr";
      const row = document.createElement("div");
      row.className = "row";
      const title = document.createElement("strong");
      title.textContent = p.action + " on " + p.service;
      row.appendChild(title);
      const why = document.createElement("p");
      why.className = "muted small";
      why.textContent =
        (p.hypothesis ? "hypothesis: " + p.hypothesis + ". " : "") +
        (p.evidence || []).join("; ");
      const actions = document.createElement("div");
      actions.className = "actions";
      const approve = document.createElement("button");
      approve.type = "button";
      approve.className = "ok";
      approve.textContent = "Approve & run (simulated)";
      approve.addEventListener("click", () => decide(p.incident_id, true));
      const reject = document.createElement("button");
      reject.type = "button";
      reject.className = "bad";
      reject.textContent = "Reject";
      reject.addEventListener("click", () => decide(p.incident_id, false));
      actions.appendChild(approve);
      actions.appendChild(reject);
      card.appendChild(row);
      card.appendChild(why);
      card.appendChild(actions);
      root.appendChild(card);
    });
  }

  async function decide(incidentId, approved) {
    text($("decision-error"), "");
    const name = $("approver").value.trim();
    if (!name) {
      text($("decision-error"), "Enter an approver name first.");
      return;
    }
    const path =
      "/api/runs/" +
      state.currentId +
      "/approvals/" +
      incidentId +
      "/" +
      (approved ? "approve" : "reject");
    try {
      await api("POST", path, { approver: name, reason: approved ? "" : "declined in dashboard" });
      state.cursor = 0;
      state.events = [];
      await pollOnce();
      await refreshRuns();
    } catch (err) {
      text($("decision-error"), err.message || String(err));
    }
  }

  $("scenario").addEventListener("change", () => {
    const opt = $("scenario").selectedOptions[0];
    text($("scenario-desc"), opt ? opt.dataset.desc || "" : "");
  });

  $("start-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    text($("start-error"), "");
    if ($("api-key").value) setApiKey($("api-key").value.trim());
    try {
      const body = {
        scenario: $("scenario").value,
        provider: $("provider").value || undefined,
      };
      const { status, data } = await api("POST", "/api/runs", body);
      await refreshRuns();
      await selectRun(data.id);
      if (status === 200 && data.events) {
        state.events = data.events.slice();
        state.cursor = data.events.length;
        state.services = data.services || [];
        state.pending = data.pending || [];
        renderTimeline();
        renderTopology();
        renderIncidents(data.incidents || []);
        renderApprovals();
      }
    } catch (err) {
      text($("start-error"), err.message || String(err));
    }
  });

  $("refresh-btn").addEventListener("click", () => refreshRuns().catch(() => {}));
  document.querySelectorAll(".tabs [role=tab]").forEach((btn) => {
    btn.addEventListener("click", () => setTab(btn.dataset.tab));
  });

  async function boot() {
    // Capture deep-link params before setTab/writeHash can rewrite location.hash
    // (writeHash omits run= until state.currentId is set).
    const initial = parseHash();
    setTab(initial.tab || "topology");
    await loadConfig();
    await refreshRuns();
    if (initial.run) await selectRun(initial.run);
  }

  boot().catch((err) => {
    text($("start-error"), err.message || String(err));
  });
})();
