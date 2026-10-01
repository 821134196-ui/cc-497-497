/* 共享脚本：会话、导航、接口封装、文案与徽章。 */
(function () {
  "use strict";

  const NAV = [
    ["/receive.html", "收货扫描"],
    ["/trace.html", "条包追溯"],
    ["/returns.html", "退货待审"],
    ["/quarantine.html", "异常隔离"],
    ["/stock.html", "库存差异"],
  ];

  const STATUS_TEXT = {
    IN_STOCK: "在库",
    SOLD: "已售",
    QUARANTINED: "异常隔离",
    RETURNING: "退货在途",
    RETURNED: "已出库",
    RECV_REJECTED: "收货拒收",
  };

  const ANOMALY_TEXT = {
    damaged: "破损",
    code_mismatch: "串码不符",
    duplicate: "重复扫描",
    foreign_store: "非本店入库",
    return_expired: "超过退货期限",
    return_verify: "退货核对中",
    return_rejected_keep: "退货驳回保留隔离",
    carrier_rejected: "配送方拒收退回",
  };

  const EVENT_TEXT = {
    receive: "正常收货入库",
    receive_quarantine: "收货异常隔离",
    receive_duplicate_blocked: "重复扫描拦截",
    receive_reject_unknown: "来源不明拒收",
    sale: "前台售出",
    return_request: "发起退货申请",
    return_approved: "店长复核通过",
    return_rejected: "店长复核驳回",
    carrier_receipt: "配送方回执",
    carrier_receipt_replay: "配送方回执重放",
    anomaly_release: "解除隔离回在库",
    anomaly_refuse: "异常处置拒收",
    stock_count: "实物盘点",
    return_expired_blocked: "超期退货拦截",
  };

  const REASON_TEXT = { damaged: "破损", mismatch: "串码不符", other: "其他" };

  const REQ_STATUS = {
    pending: "待店长复核",
    outbound: "已复核·待回执",
    closed: "已结案",
    rejected: "已驳回",
  };

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function badge(text, kind) {
    return `<span class="badge ${kind || ""}">${esc(text)}</span>`;
  }

  function statusBadge(status) {
    const map = {
      IN_STOCK: "ok", SOLD: "warn", QUARANTINED: "bad",
      RETURNING: "info", RETURNED: "", RECV_REJECTED: "bad",
    };
    return badge(STATUS_TEXT[status] || status, map[status]);
  }

  function anomalyBadge(anomaly) {
    if (!anomaly) return "";
    const kind = ["damaged", "code_mismatch", "duplicate", "foreign_store",
      "return_expired", "carrier_rejected"].includes(anomaly) ? "bad" : "warn";
    return badge("异常：" + (ANOMALY_TEXT[anomaly] || anomaly), kind);
  }

  async function api(method, path, body) {
    const opt = { method, headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(path, opt);
    } catch (e) {
      throw { message: "网络错误，服务未启动？" };
    }
    let data = null;
    try { data = await resp.json(); } catch (e) { /* ignore */ }
    if (resp.status === 401) {
      location.href = "/login.html?next=" + encodeURIComponent(location.pathname);
      throw { status: 401, message: "未登录" };
    }
    if (!data) throw { status: resp.status, message: "响应解析失败" };
    if (!data.ok) throw { status: resp.status, message: data.error || "操作失败", code: data.code };
    return data.data;
  }

  function toast(title, body, kind) {
    let host = document.querySelector(".toast-host");
    if (!host) {
      host = document.createElement("div");
      host.className = "toast-host";
      document.body.appendChild(host);
    }
    const el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.innerHTML =
      `<div class="t-title">${esc(title)}</div>` +
      (body ? `<div class="t-body">${esc(body)}</div>` : "");
    host.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .4s"; }, 4200);
    setTimeout(() => el.remove(), 4800);
  }

  function verdictHost(el, kind, title, body) {
    el.innerHTML =
      `<div class="verdict ${kind}">
         <div class="v-title">${esc(title)}</div>
         ${body ? `<div class="v-body">${body}</div>` : ""}
       </div>`;
  }

  function renderChrome(active, me) {
    const navHtml = NAV.map(([href, name]) =>
      `<a href="${href}" class="${href === active ? "active" : ""}">${name}</a>`).join("");
    const role = me.role === "manager" ? "店长" : "店员";
    document.body.insertAdjacentHTML("afterbegin",
      `<header class="topbar">
         <span class="brand">卷烟条包追溯（演示）</span>
         <nav>${navHtml}</nav>
         <span class="spacer"></span>
         <span class="userbox">
           <span class="store-tag">门店 ${esc(me.store_id)}</span>
           <span class="badge ${me.role === "manager" ? "role-manager" : "role-clerk"}">${esc(me.display_name)}</span>
           <button id="logout-btn" class="small">退出登录</button>
         </span>
       </header>`);
    document.getElementById("logout-btn").addEventListener("click", async () => {
      await api("POST", "/api/logout", {});
      location.href = "/login.html";
    });
  }

  async function initPage(active, fn) {
    let me;
    try {
      me = await api("GET", "/api/me");
    } catch (e) {
      return; // 已跳转登录页
    }
    if (!me) {
      location.href = "/login.html?next=" + encodeURIComponent(active);
      return;
    }
    renderChrome(active, me);
    await fn(me);
  }

  window.TRACE = {
    esc, badge, statusBadge, anomalyBadge,
    api, toast, verdictHost, initPage,
    STATUS_TEXT, ANOMALY_TEXT, EVENT_TEXT, REASON_TEXT, REQ_STATUS,
  };
})();
