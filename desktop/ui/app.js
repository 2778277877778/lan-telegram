// lan-telegram 桌面壳前端：零框架 vanilla JS。
// 与 Rust 桥接层的全部接口：engine_state / read_view / history_before / send_text / mark_read / contact_list
// 事件：tg-events（Rust 事件泵从 /events 长轮询转推）。

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const S = {
  me: { name: "?", id: "" },
  tls: false,
  convs: [],
  contacts: new Map(),     // device_id -> {name, ip, last_seen}
  active: null,            // 当前会话 id
  msgs: [],                // 当前会话已渲染消息（时间升序）
  oldest: 0,               // 最旧一条 ts，用于向前翻页
  replyTo: null,           // {id, text}
  pendingTarget: null,     // 新对话目标（发送首条后转正式会话）
  loadingOlder: false,
};

const $ = (id) => document.getElementById(id);

function avatarColor(key) {
  const palette = ["#e17076", "#eda75a", "#7bc862", "#65aadd", "#a77de3", "#6ec9cb", "#f29f72"];
  let h = 0;
  for (const ch of key || "?") h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return palette[h % palette.length];
}

function fmtTime(ms) {
  if (!ms) return "";
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function fmtDay(ms) {
  const d = new Date(ms), now = new Date();
  if (d.toDateString() === now.toDateString()) return "今天";
  const y = new Date(now - 864e5);
  if (d.toDateString() === y.toDateString()) return "昨天";
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

function peerOf(convId) {
  const dev = convId.startsWith("dm:") ? convId.slice(3) : "";
  const c = S.convs.find(x => x.id === convId);
  const known = S.contacts.get(dev);
  return {
    id: dev,
    name: (c && c.title) || (known && known.name) || dev.slice(0, 8),
    ip: known && known.ip,
    online: known && known.last_seen && Date.now() - known.last_seen < 180000,
  };
}

function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  t.className = err ? "err" : "";
  clearTimeout(toast._h);
  toast._h = setTimeout(() => (t.hidden = true), err ? 4200 : 2400);
}

async function api(cmd, args) {
  const r = await invoke(cmd, args || {});
  if (r && r.ok === false && r.error) throw new Error(r.error);
  return r;
}

/* ---------------- 渲染：会话列表 ---------------- */

function renderConvs() {
  const ul = $("convs");
  ul.textContent = "";
  if (!S.convs.length && !S.pendingTarget) {
    const li = document.createElement("li");
    li.className = "c-empty";
    li.textContent = "还没有会话\n点「新对话」发第一条";
    li.style.whiteSpace = "pre-line";
    ul.appendChild(li);
    return;
  }
  for (const c of S.convs) {
    const p = peerOf(c.id);
    const li = document.createElement("li");
    li.className = "conv" + (c.id === S.active ? " active" : "");
    li.onclick = () => openConv(c.id);

    const av = document.createElement("div");
    av.className = "avatar";
    av.style.background = avatarColor(p.id || p.name);
    av.textContent = p.name.slice(0, 1).toUpperCase();

    const main = document.createElement("div");
    main.className = "c-main";
    const r1 = document.createElement("div"); r1.className = "c-row";
    const nm = document.createElement("span"); nm.className = "c-name"; nm.textContent = p.name;
    const tm = document.createElement("span"); tm.className = "c-time"; tm.textContent = fmtTime(c.last_msg_at);
    r1.append(nm, tm);
    const r2 = document.createElement("div"); r2.className = "c-row2";
    const pv = document.createElement("span"); pv.className = "c-preview";
    pv.textContent = c.preview || "（空会话）";
    r2.append(pv);
    if (c.unread) {
      const b = document.createElement("span"); b.className = "c-badge"; b.textContent = c.unread;
      r2.append(b);
    }
    main.append(r1, r2);
    li.append(av, main);
    ul.appendChild(li);
  }
}

/* ---------------- 渲染：消息区 ---------------- */

function tickHtml(state, dir) {
  if (dir !== "out") return "";
  if (state === "read") return `<span class="tick">✓✓</span>`;
  if (state === "delivered") return `<span class="tick">✓✓</span>`;
  if (state === "sent") return `<span class="tick">✓</span>`;
  if (state === "queued") return `<span class="tick" title="待补发">🕓</span>`;
  return "";
}

function makeBubble(m) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + (m.direction === "out" ? "out" : "in");
  wrap.dataset.id = m.id;
  const bub = document.createElement("div");
  bub.className = "bubble";
  if (m.reply_to) {
    const ref = document.createElement("div");
    ref.className = "reply-ref";
    const target = S.msgs.find(x => x.id === m.reply_to);
    const t = target ? ((target.body && target.body.text) || "").slice(0, 40) : m.reply_to.slice(0, 8);
    ref.textContent = "↩ " + (t || "引用");
    ref.onclick = () => {
      const el = wrap.parentElement.querySelector(`[data-id="${m.reply_to}"]`);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    };
    bub.appendChild(ref);
  }
  const text = document.createElement("span");
  text.textContent = (m.body && m.body.text) || "";
  bub.appendChild(text);
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.innerHTML = `${fmtTime(m.ts)}${tickHtml(m.state, m.direction)}`;
  meta.append(document.createTextNode(""));
  // 勾选部分用 span 包裹已安全（固定字形），时间为文本节点
  bub.appendChild(meta);
  bub.ondblclick = () => {
    navigator.clipboard && navigator.clipboard.writeText((m.body && m.body.text) || "");
    toast("已复制");
  };
  bub.oncontextmenu = (e) => { e.preventDefault(); setReply(m); };
  wrap.appendChild(bub);
  return wrap;
}

function renderMsgs() {
  const box = $("msgs");
  box.textContent = "";
  let lastDay = "";
  if (S.msgs.length > 0) {
    const older = document.createElement("button");
    older.className = "load-old";
    older.textContent = S.msgs.length >= 50 ? "查看更早" : "· 到顶了 ·";
    older.onclick = loadOlder;
    box.appendChild(older);
  }
  for (const m of S.msgs) {
    const day = fmtDay(m.ts);
    if (day !== lastDay) {
      lastDay = day;
      const sep = document.createElement("div");
      sep.className = "day-sep";
      sep.textContent = day;
      box.appendChild(sep);
    }
    box.appendChild(makeBubble(m));
  }
  box.scrollTop = box.scrollHeight;
}

function updateTicks(msgId, state) {
  const m = S.msgs.find(x => x.id === msgId);
  if (m && m.direction === "out") {
    m.state = state;
    const el = $("msgs").querySelector(`[data-id="${msgId}"] .meta`);
    if (el) el.innerHTML = `${fmtTime(m.ts)}${tickHtml(m.state, m.direction)}`;
  }
}

/* ---------------- 数据动作 ---------------- */

async function refreshConvs() {
  try {
    const r = await api("read_view", { conv: null, limit: null });
    S.convs = r.conversations || [];
    renderConvs();
  } catch (e) { toast(e.message, true); }
}

async function refreshContacts() {
  try {
    const r = await api("contact_list", {});
    S.contacts = new Map((r.contacts || []).map(c => [c.id, c]));
  } catch (_) { /* 非关键路径 */ }
}

async function openConv(id) {
  S.active = id;
  S.replyTo = null;
  $("reply-bar").hidden = true;
  $("empty").hidden = true;
  $("chat").hidden = false;
  const p = peerOf(id);
  $("peer-name").textContent = p.name;
  $("peer-sub").textContent = p.online ? "在线" : "离线";
  $("peer-sub").className = p.online ? "online" : "";
  $("peer-avatar").textContent = p.name.slice(0, 1).toUpperCase();
  $("peer-avatar").style.background = avatarColor(p.id || p.name);
  renderConvs();
  try {
    const r = await api("read_view", { conv: id, limit: 50 });
    S.msgs = r.items || [];
    S.oldest = S.msgs.length ? S.msgs[0].ts : 0;
    renderMsgs();
    if (S.convs.some(c => c.id === id && c.unread)) {
      await api("mark_read", { conv: id });   // 查看即已读（服务端发回执）
      const c = S.convs.find(x => x.id === id);
      if (c) c.unread = 0;
      renderConvs();
    }
  } catch (e) { toast(e.message, true); }
}

async function loadOlder() {
  if (!S.active || !S.oldest || S.loadingOlder) return;
  S.loadingOlder = true;
  try {
    const r = await api("history_before", { conv: S.active, before: S.oldest, limit: 50 });
    const olderItems = r.items || [];
    if (!olderItems.length) { S.msgs = S.msgs; renderMsgs(); return; }
    const box = $("msgs");
    const keepTop = box.scrollHeight - box.scrollTop;
    S.msgs = [...olderItems, ...S.msgs];
    S.oldest = olderItems[0].ts;
    renderMsgs();
    box.scrollTop = box.scrollHeight - keepTop;
  } catch (e) { toast(e.message, true); }
  finally { S.loadingOlder = false; }
}

function setReply(m) {
  S.replyTo = { id: m.id, text: ((m.body && m.body.text) || "").slice(0, 40) };
  $("reply-text").textContent = S.replyTo.text || m.id.slice(0, 8);
  $("reply-bar").hidden = false;
  $("input").focus();
}

async function doSend() {
  const input = $("input");
  const text = input.value.trim();
  if (!text) return;
  // 目标优先用完整设备 ID（find_device 按 id 精确匹配），显示名可能是截断 id
  const to = S.active ? (peerOf(S.active).id || peerOf(S.active).name) : S.pendingTarget;
  if (!to) { toast("先点「新对话」指定对方", true); return; }
  input.value = "";
  input.style.height = "auto";
  $("send").classList.add("busy");
  try {
    const r = await api("send_text", {
      to, text, reply_to: S.replyTo ? S.replyTo.id : null,
    });
    if (r.message) {
      if (r.message.conv !== S.active) { await openConv(r.message.conv); }
      else { S.msgs.push(r.message); renderMsgs(); }
    }
    if (r.queued) toast(`对方暂不可达，已入队自动补发（${r.error || ""}）`);
    else if (r.state === "delivered") toast("已送达");
    S.pendingTarget = null;
    S.replyTo = null; $("reply-bar").hidden = true;
    await refreshConvs();
  } catch (e) {
    toast(e.message, true);
  } finally { $("send").classList.remove("busy"); }
}

/* ---------------- 事件泵（Rust → 前端） ---------------- */

function onEvents(events) {
  let convDirty = false;
  for (const e of events) {
    const d = e.data || {};
    if (e.type === "message") {
      convDirty = true;
      const convId = "dm:" + (d.from || "");
      if (convId === S.active && d.from !== S.me.id) {
        (async () => {
          try {
            const r = await api("read_view", { conv: convId, limit: 50 });
            S.msgs = r.items || [];
            S.oldest = S.msgs.length ? S.msgs[0].ts : 0;
            renderMsgs();
            await api("mark_read", { conv: convId });
          } catch (_) {}
        })();
      }
    } else if (e.type === "receipt") {
      updateTicks(d.message_id, d.state === "read" ? "read" : "delivered");
      convDirty = d.state === "read";
    }
  }
  if (convDirty) refreshConvs().catch(() => {});
}

/* ---------------- 初始化 ---------------- */

async function boot() {
  $("theme-toggle").onclick = () => {
    const cur = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = cur;
    localStorage.setItem("lt-theme", cur);
  };
  const saved = localStorage.getItem("lt-theme");
  if (saved) document.documentElement.dataset.theme = saved;

  $("send").onclick = doSend;
  $("new-chat").onclick = () => {
    const t = window.prompt("对方 IP[:端口] 或设备名：", S.pendingTarget || "");
    if (t && t.trim()) {
      S.pendingTarget = t.trim();
      S.active = null;
      $("chat").hidden = false;
      $("empty").hidden = true;
      $("peer-name").textContent = S.pendingTarget + "（新对话）";
      $("peer-sub").textContent = "发首条消息建立会话";
      $("input").focus();
      renderConvs();
    }
  };
  $("reply-cancel").onclick = () => { S.replyTo = null; $("reply-bar").hidden = true; };
  const input = $("input");
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); doSend(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  });

  listen("tg-events", (ev) => onEvents(ev.payload));

  try {
    const st = await api("engine_state", {});
    S.me.name = st.device_name || "?";
    S.me.id = (st.device_id || "").slice(0, 32);
    S.tls = !!st.tls;
    $("me-name").textContent = S.me.name;
    $("me-state").textContent = st.tls ? "TLS 1.3 · 已加密" : "明文模式（建议 tls on）";
    $("me-state").className = st.tls ? "online" : "";
  } catch (e) {
    $("me-state").textContent = "引擎离线";
    toast(e.message || "连不上引擎：先运行 python lantelegram.py serve --background", true);
  }
  await refreshContacts();
  await refreshConvs();
}

document.addEventListener("DOMContentLoaded", boot);
