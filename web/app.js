/* 门店卷烟条包流转追溯 —— 前端逻辑（原生 JS，无依赖） */
"use strict";

const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({ ok: false, error: { message: "响应解析失败" } }));
  if (!res.ok || data.ok === false) {
    const err = new Error(data.error ? data.error.message
      : (data.message || "请求失败"));
    err.code = data.error ? data.error.code : (data.result || "error");
    err.data = data;  // 业务异常（200 + ok:false）的对比明细
    throw err;
  }
  return data;
};

const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

const STATE_LABEL = {
  in_stock: "在库", sold: "已售", return_pending: "退货待审",
  return_approved: "复核通过待回执", outbound: "已出库", quarantined: "异常隔离",
};
const RESULT_LABEL = {
  ok: "正常入库", duplicate: "重复扫描", damaged: "破损",
  code_mismatch: "串码不符", foreign_store: "非本店入库",
  delivery_refused: "配送方拒收回执", overdue: "超过退货期限",
};

let ME = null;

// --------------------------------------------------------------- 登录
async function boot() {
  try {
    const d = await api("/api/me");
    enterApp(d.user);
  } catch {
    showLogin();
  }
}
function showLogin() {
  document.getElementById("login").style.display = "";
  document.getElementById("app").style.display = "none";
}
function enterApp(user) {
  ME = user;
  document.getElementById("login").style.display = "none";
  document.getElementById("app").style.display = "";
  document.getElementById("who-name").textContent = user.name;
  document.getElementById("who-role").textContent = user.role_label;
  document.getElementById("who-store").textContent = user.store_id;
  document.getElementById("ret-request-panel").style.display =
    user.role === "clerk" ? "" : "none";
  document.getElementById("ret-action-th").style.display =
    user.role === "manager" ? "" : "none";
  switchPage("receive");
  loadNotes();
  loadRecent();
}
document.getElementById("lg-btn").onclick = async () => {
  try {
    const d = await api("/api/login", { method: "POST", body: {
      username: document.getElementById("lg-user").value.trim(),
      password: document.getElementById("lg-pass").value,
    }});
    enterApp(d.user);
  } catch (e) { alert("登录失败：" + e.message); }
};
document.getElementById("logout-btn").onclick = async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  location.reload();
};

// --------------------------------------------------------------- 页签
function switchPage(name) {
  document.querySelectorAll("nav.tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.page === name));
  document.querySelectorAll(".page").forEach(p =>
    p.classList.toggle("active", p.id === "page-" + name));
  if (name === "trace") loadTraceHint();
  if (name === "returns") loadReturns();
  if (name === "quarantine") loadQuarantine();
  if (name === "diff") { loadInventory(); loadDiff(); }
}
document.getElementById("tabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-page]");
  if (b) switchPage(b.dataset.page);
});

function banner(el, kind, html) {
  el.className = "banner show " + kind;
  el.innerHTML = html;
}
function hideBanner(el) { el.className = "banner"; }

// --------------------------------------------------------------- 收货扫描
async function loadNotes() {
  const d = await api("/api/notes");
  document.getElementById("rc-note").innerHTML = d.notes
    .map(n => `<option value="${esc(n.id)}">${esc(n.id)}（${esc(n.carrier)}，${esc(n.delivered_at)}）</option>`)
    .join("");
}

// 输入串码时，固定栏显示完整串码 + 最近一次流转位置
let codebarTimer = null;
document.getElementById("rc-code").addEventListener("input", () => {
  clearTimeout(codebarTimer);
  const code = document.getElementById("rc-code").value.trim().toUpperCase();
  const bar = document.getElementById("receive-codebar");
  if (!code) { bar.classList.remove("show"); return; }
  document.getElementById("cb-code").textContent = code;
  codebarTimer = setTimeout(() => refreshCodebar(code), 250);
});
async function refreshCodebar(code) {
  const bar = document.getElementById("receive-codebar");
  try {
    const d = await api("/api/trace?code=" + encodeURIComponent(code));
    const p = d.data.package, f = d.data.flows[d.data.flows.length - 1];
    bar.classList.add("show");
    document.getElementById("cb-state").textContent = p.state_label;
    document.getElementById("cb-location").textContent =
      f ? (f.location || "—") : "—";
    document.getElementById("cb-last").textContent = f
      ? `${f.ts} · ${flowText(f)}` : "无流转记录";
  } catch {
    bar.classList.add("show");
    document.getElementById("cb-state").textContent = "系统中尚无此串码（新扫将尝试入库）";
    document.getElementById("cb-location").textContent = "—";
    document.getElementById("cb-last").textContent = "—";
  }
}
function flowText(f) {
  const map = { receive: "收货入库", receive_quarantine: "收货即隔离", sell: "销售",
    return_request: "发起退货", return_approve: "店长复核通过",
    return_reject: "店长驳回", carrier_accept: "配送接收出库",
    carrier_refuse: "配送拒收隔离", return_overdue_blocked: "超期退货被拦截" };
  return map[f.action] || f.action;
}

document.getElementById("rc-submit").onclick = async () => {
  const code = document.getElementById("rc-code").value.trim().toUpperCase();
  if (!code) return alert("请输入条包完整串码");
  hideBanner(document.getElementById("rc-ok"));
  hideBanner(document.getElementById("rc-err"));
  try {
    const d = await api("/api/receive", { method: "POST", body: {
      note_id: document.getElementById("rc-note").value,
      code, barcode: document.getElementById("rc-barcode").value.trim(),
      location: document.getElementById("rc-location").value.trim(),
      damaged: document.getElementById("rc-damaged").checked,
    }});
    banner(document.getElementById("rc-ok"), "ok",
      `<span class="tag">结论：正常入库</span>${esc(d.message)}`);
    document.getElementById("rc-code").value = "";
    document.getElementById("receive-codebar").classList.remove("show");
  } catch (e) {
    showReceiveError(e);
  }
  loadRecent();
};
function showReceiveError(e) {
  const el = document.getElementById("rc-err");
  banner(el, "err",
    `<span class="tag">结论：${esc(RESULT_LABEL[e.code] || "未入库")}</span>${esc(e.message)}`);
  // 异常对比左右并排：预期 / 实际
  const cmp = document.getElementById("rc-compare");
  const detail = e.data && e.data.anomaly_detail;
  if (detail) {
    const pairs = [["expected", "actual"], ["barcode_expected", "barcode_actual"]];
    let eHtml = "", aHtml = "";
    pairs.forEach(([ek, ak]) => {
      if (detail[ek] != null || detail[ak] != null) {
        eHtml += `<dt>比对项</dt><dd>${esc(detail[ek] || "—")}</dd>`;
        aHtml += `<dt>比对项</dt><dd>${esc(detail[ak] || "—")}</dd>`;
      }
    });
    document.getElementById("rc-cmp-expected").innerHTML =
      eHtml || "<dt>系统预期</dt><dd>该串码应在本店本配送单的收货计划内</dd>";
    document.getElementById("rc-cmp-actual").innerHTML =
      aHtml || `<dt>实际扫描</dt><dd>${esc(JSON.stringify(detail))}</dd>`;
    cmp.style.display = "grid";
  } else {
    cmp.style.display = "none";
  }
}
function detailLabel(k) {
  return { expected: "系统预期", actual: "实际扫描", plan: "配送计划",
    barcode_expected: "预期条码", barcode_actual: "实际条码" }[k] || k;
}

async function loadRecent() {
  const d = await api("/api/recent-scans");
  document.getElementById("recent-body").innerHTML = d.scans.map(s => `
    <tr>
      <td class="small">${esc(s.ts)}</td>
      <td class="mono">${esc(s.code)}</td>
      <td>${esc({ receive: "收货", sell: "销售", return: "退货" }[s.context] || s.context)}</td>
      <td>${resultBadge(s.result)}</td>
      <td class="small">${esc(s.reason || "")}</td>
    </tr>`).join("") || `<tr><td colspan="5" class="muted">暂无记录</td></tr>`;
}
function resultBadge(r) {
  const bad = ["duplicate", "damaged", "code_mismatch", "foreign_store",
    "delivery_refused", "overdue", "wrong_state", "no_inbound_chain",
    "not_found", "active_return_exists"];
  const cls = r === "ok" ? "ok" : bad.includes(r) ? "err" : "warn";
  const text = r === "ok" ? "正常" : (RESULT_LABEL[r] || r);
  return `<span class="badge ${cls}">${esc(text)}</span>`;
}

// --------------------------------------------------------------- 条包追溯
function loadTraceHint() { /* 进入页面时聚焦 */ document.getElementById("tr-code").focus(); }
document.getElementById("tr-btn").onclick = doTrace;
document.getElementById("tr-code").addEventListener("keydown", e => { if (e.key === "Enter") doTrace(); });

async function doTrace() {
  const code = document.getElementById("tr-code").value.trim().toUpperCase();
  hideBanner(document.getElementById("tr-err"));
  document.getElementById("tr-result").style.display = "none";
  if (!code) return;
  try {
    const d = await api("/api/trace?code=" + encodeURIComponent(code));
    renderTrace(d.data);
  } catch (e) {
    banner(document.getElementById("tr-err"), "err",
      `<span class="tag">结论：查不到 / 不可见</span>${esc(e.message)}`);
  }
}
function renderTrace(data) {
  const p = data.package, sp = data.spec;
  const stateBadge = p.state === "quarantined"
    ? `<span class="badge err">${esc(p.state_label)}</span>`
    : p.state === "outbound" ? `<span class="badge">${esc(p.state_label)}</span>`
    : `<span class="badge ok">${esc(p.state_label)}</span>`;
  document.getElementById("tr-kv1").innerHTML = `
    <dt>完整串码</dt><dd class="mono">${esc(p.code)}</dd>
    <dt>当前状态</dt><dd>${stateBadge}</dd>
    <dt>品牌规格</dt><dd>${sp ? esc(sp.brand + " " + sp.spec_name) : "（未知）"}</dd>
    <dt>条码</dt><dd class="mono">${sp ? esc(sp.barcode) : "—"}</dd>`;
  document.getElementById("tr-kv2").innerHTML = `
    <dt>批次</dt><dd class="mono">${esc(p.batch || "—")}</dd>
    <dt>库位</dt><dd>${esc(p.location || "—")}</dd>
    <dt>入库时间</dt><dd>${esc(p.received_at || "—")}</dd>
    <dt>异常标注</dt><dd>${p.anomaly_type ? `<span class="badge err">${esc(p.anomaly_label || p.anomaly_type)}</span>` : "无"}</dd>`;

  document.getElementById("tr-flows").innerHTML = data.flows.map(f => {
    const bad = /quarantine|refuse|reject|overdue/.test(f.action);
    return `<li class="${bad ? "bad" : ""}">
      <div class="t-time">${esc(f.ts)} · ${esc(f.actor_name)} · 位置：${esc(f.location || "—")}</div>
      <div class="t-state">${esc(f.from_state_label)} → ${esc(f.to_state_label)}</div>
      <div class="t-note">${esc(flowText(f))}${f.note ? "：" + esc(f.note) : ""}
        ${f.photo_path ? `<br><img class="photo-thumb" src="/photos/${esc(basename(f.photo_path))}" alt="流转照片">` : ""}
      </div></li>`;
  }).join("");

  const panel = document.getElementById("tr-returns-panel");
  if (data.returns.length) {
    panel.style.display = "";
    document.getElementById("tr-returns").innerHTML = data.returns.map(r => {
      const rc = data.receipts.find(x => x.return_id === r.id);
      return `<tr>
        <td>${r.id}</td>
        <td>${esc(RET_STATUS[r.status] || r.status)}</td>
        <td>${esc(r.clerk_name || "")}</td>
        <td>${r.days_held == null ? "—" : r.days_held + " 天"}</td>
        <td class="small">${esc(r.reason || "")}<br><span class="muted">${esc(r.review_note || "")}</span></td>
        <td class="small">${rc ? `<span class="badge ${rc.accepted ? "ok" : "err"}">${rc.accepted ? "接收" : "拒收"}</span> ${esc(rc.receipt_no)}<br><span class="muted">${esc(rc.reason || "")}</span>` : "—"}</td>
      </tr>`;
    }).join("");
  } else panel.style.display = "none";
  document.getElementById("tr-result").style.display = "";
}
function basename(p) { return p.split(/[\\/]/).pop(); }
const RET_STATUS = { pending: "待店长复核", approved: "待配送回执", rejected: "已驳回/拒绝",
  outbound: "配送接收已出库", refused: "配送拒收隔离" };

// --------------------------------------------------------------- 退货
async function loadReturns() {
  const d = await api("/api/returns");
  const isMgr = ME && ME.role === "manager";
  document.getElementById("ret-body").innerHTML = d.returns.map(r => {
    const statusCls = r.status === "pending" ? "warn"
      : r.status === "approved" ? "warn"
      : r.status === "outbound" ? "ok"
      : r.status === "refused" ? "err" : "";
    let actions = "";
    if (isMgr) {
      if (r.status === "pending")
        actions = `<button class="btn small" data-act="approve" data-id="${r.id}">复核通过</button>
                   <button class="btn small danger" data-act="reject" data-id="${r.id}">驳回</button>`;
      else if (r.status === "approved")
        actions = `<button class="btn small" data-act="receipt" data-id="${r.id}">请求配送回执</button>`;
      else if (r.receipt)
        actions = `<button class="btn small secondary" data-act="receipt" data-id="${r.id}">重放回执</button>`;
    }
    return `<tr>
      <td>${r.id}</td>
      <td class="mono">${esc(r.code)}<br><span class="muted small">${esc(r.spec_text)}</span></td>
      <td><span class="badge ${statusCls}">${esc(r.status_label)}</span>
          ${r.receipt ? `<span class="badge ${r.receipt.accepted ? "ok" : "err"}">回执：${r.receipt.accepted ? "接收" : "拒收"}</span>` : ""}</td>
      <td>${esc(r.clerk_name || "")}</td>
      <td>${r.days_held == null ? "—" : r.days_held + " 天"}</td>
      <td class="small">${esc(r.reason || "")}<br><span class="muted">${esc(r.review_note || "")}${r.manager_name ? "（" + esc(r.manager_name) + "）" : ""}</span></td>
      <td style="white-space:nowrap;">${actions}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">暂无退货单</td></tr>`;
}
document.getElementById("ret-body").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const id = b.dataset.id, act = b.dataset.act;
  try {
    if (act === "approve") {
      const note = prompt("复核意见（可空）：", "包装完好，同意退货") || "";
      await api(`/api/returns/${id}/review`, { method: "POST", body: { approved: true, note }});
    } else if (act === "reject") {
      const note = prompt("驳回原因：", "不满足退货条件") || "不满足退货条件";
      await api(`/api/returns/${id}/review`, { method: "POST", body: { approved: false, note }});
    } else if (act === "receipt") {
      const d = await api(`/api/returns/${id}/receipt`, { method: "POST", body: {} });
      alert((d.replayed ? "【回执重放】" : "【配送回执】") + d.message);
    }
  } catch (err) { alert("操作失败：" + err.message); }
  loadReturns();
});
document.getElementById("ret-btn").onclick = async () => {
  const code = document.getElementById("ret-code").value.trim().toUpperCase();
  const reason = document.getElementById("ret-reason").value.trim();
  if (!code || !reason) return alert("请填写串码和退货原因");
  const file = document.getElementById("ret-photo").files[0];
  const photo_data = file ? await fileToDataUrl(file) : null;
  hideBanner(document.getElementById("ret-ok"));
  hideBanner(document.getElementById("ret-err"));
  try {
    const d = await api("/api/returns", { method: "POST", body: { code, reason, photo_data }});
    banner(document.getElementById("ret-ok"), "ok",
      `<span class="tag">结论：已受理</span>${esc(d.message)}`);
    document.getElementById("ret-code").value = "";
    document.getElementById("ret-reason").value = "";
    document.getElementById("ret-photo").value = "";
  } catch (e) {
    const tag = { overdue: "超过退货期限", no_inbound_chain: "入库链路核对失败",
      wrong_state: "状态不符", active_return_exists: "重复申请",
      foreign_store: "非本店条包" }[e.code] || "退货被拦截";
    banner(document.getElementById("ret-err"), "err",
      `<span class="tag">结论：${esc(tag)}</span>${esc(e.message)}`);
  }
  loadReturns();
};
function fileToDataUrl(file) {
  return new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(fr.result);
    fr.onerror = rej;
    fr.readAsDataURL(file);
  });
}

// --------------------------------------------------------------- 异常隔离
async function loadQuarantine() {
  const d = await api("/api/quarantine");
  document.getElementById("q-body").innerHTML = d.packages.map(p => {
    let detail = "";
    try {
      const obj = JSON.parse(p.anomaly_detail || "{}");
      detail = Object.entries(obj).map(([k, v]) =>
        `<div><span class="muted">${esc(detailLabel(k))}：</span>${esc(v)}</div>`).join("");
    } catch {}
    return `<tr>
      <td class="mono">${esc(p.code)}</td>
      <td>${esc(p.spec_text)}</td>
      <td class="mono">${esc(p.batch || "—")}</td>
      <td><span class="badge err">${esc(p.anomaly_label)}</span><div class="small muted">（文字结论，不依赖颜色）</div></td>
      <td class="small">${detail || "—"}</td>
      <td>${esc(p.location || "—")}</td>
      <td>${p.photo_path ? `<img class="photo-thumb" src="/photos/${esc(basename(p.photo_path))}" alt="异常照片">` : "—"}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">暂无隔离条包</td></tr>`;
}

// --------------------------------------------------------------- 库存
async function loadInventory() {
  const d = await api("/api/inventory");
  document.getElementById("inv-body").innerHTML = d.groups.map((g, i) => {
    const c = g.counts;
    const active = c.sold + c.return_pending + c.return_approved;
    const sum = c.in_stock + active + c.outbound + c.quarantined;
    const rows = g.packages.map(p => `
      <tr class="inv-sub" data-group="${i}" style="display:none;">
        <td></td>
        <td class="mono subrows" colspan="5">${esc(p.code)} · 批次 ${esc(p.batch || "—")}
          · 库位 ${esc(p.location || "—")} · ${esc(p.state_label)}
          ${p.anomaly_type ? `· <span class="badge err">${esc(RESULT_LABEL[p.anomaly_type] || p.anomaly_type)}</span>` : ""}
        </td>
      </tr>`).join("");
    return `<tr class="group-head" data-group="${i}">
        <td><span class="caret">▶</span></td>
        <td>${esc(g.spec_text)}</td>
        <td>${c.in_stock}</td>
        <td>${active}</td>
        <td>${c.outbound}</td>
        <td>${c.quarantined ? `<span class="badge err">${c.quarantined}</span>` : 0}</td>
        <td>${sum}</td>
      </tr>${rows}`;
  }).join("") || `<tr><td colspan="7" class="muted">暂无库存</td></tr>`;

  document.getElementById("inv-body").querySelectorAll(".group-head").forEach(h => {
    h.onclick = () => {
      const i = h.dataset.group;
      const open = h.querySelector(".caret").textContent === "▼";
      h.querySelector(".caret").textContent = open ? "▶" : "▼";
      document.querySelectorAll(`tr.inv-sub[data-group="${i}"]`)
        .forEach(r => r.style.display = open ? "none" : "");
    };
  });
}

async function loadDiff() {
  const d = await api("/api/diff");
  document.getElementById("diff-notes").innerHTML = d.data.notes.map(n => {
    const diffCls = n.missing || n.quarantined || n.extra_count ? "err" : "ok";
    const head = `<div class="banner show ${diffCls}" style="margin:6px 0 10px;">
      <span class="tag">结论：${diffCls === "ok" ? "账实相符" : "存在差异"}</span>
      配送单 <b>${esc(n.note_id)}</b>（${esc(n.carrier)}）应到 ${n.expected}，
      实扫 ${n.arrived}，应到未扫 <b>${n.missing}</b>，
      在库 ${n.in_stock}，已售/退货中 ${n.sold}，已出库 ${n.outbound}，
      异常隔离 <b>${n.quarantined}</b>${n.extra_count ? `，计划外入库 <b>${n.extra_count}</b>` : ""}
    </div>`;
    const rows = n.items.map(it => {
      const problem = ["missing", "quarantined"].includes(it.status);
      return `<tr>
        <td class="mono">${esc(it.expected_code)}</td>
        <td>${esc(it.spec_text)}</td>
        <td>${it.anomaly_label ? `<span class="badge err">${esc(it.status_label)}（${esc(it.anomaly_label)}）</span>`
          : `<span class="badge ${problem ? "err" : it.status === "in_stock" ? "ok" : ""}">${esc(it.status_label)}</span>`}</td>
      </tr>`;
    }).join("");
    return `<div style="margin-bottom:18px;">${head}
      <table><thead><tr><th>计划串码</th><th>规格</th><th>实查状态（文字）</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }).join("");
}

boot();
