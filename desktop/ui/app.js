// lan-telegram 桌面壳前端：零框架 vanilla JS（Liquid Glass 版）。
// 与 Rust 桥的全部接口：engine_state / ensure_server / read_view / history_before /
// send_text / send_file / mark_read / contact_list / get_blob / save_blob / open_blob / read_preview
// 事件：tg-events（Rust 事件泵从 /events 长轮询转推）。webview 无任何网络能力。

const T = window.__TAURI__;
const { invoke } = T.core;
const { listen } = T.event;
// v2 的 withGlobalTauri 不打包插件 JS API，直接走 plugin IPC 命令
const dlgOpen = (options) => invoke("plugin:dialog|open", { options });
const dlgSave = (options) => invoke("plugin:dialog|save", { options });

const S = {
  me: { name: "?", id: "" },
  tls: false,
  convs: [],
  contacts: new Map(),
  active: null,
  msgs: [],
  oldest: 0,
  replyTo: null,
  pending: null,       // {path, name, kind, thumb_b64}
  pendingTarget: null,
  loadingOlder: false,
  thumbCache: new Map(), // msg.id -> dataURL
};

const $ = (id) => document.getElementById(id);

const PALETTE = ["#e17076", "#eda75a", "#7bc862", "#41b6f5", "#a77de3", "#6ec9cb", "#f29f72"];
function avatarColor(key) {
  let h = 0;
  for (const ch of key || "?") h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}
function fmtTime(ms) {
  if (!ms) return "";
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function fmtDay(ms) {
  const d = new Date(ms), now = new Date();
  if (d.toDateString() === now.toDateString()) return "今天";
  if (new Date(now - 864e5).toDateString() === d.toDateString()) return "昨天";
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}
function fmtSize(n) {
  if (!n) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

function peerOf(convId) {
  const dev = convId.startsWith("dm:") ? convId.slice(3) : "";
  const c = S.convs.find((x) => x.id === convId);
  const known = S.contacts.get(dev);
  return {
    id: dev,
    name: (c && c.title) || (known && known.name) || dev.slice(0, 8),
    online: known && known.last_seen && Date.now() - known.last_seen < 180000,
  };
}

function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  requestAnimationFrame(() => (t.className = "show" + (err ? " err" : "")));
  clearTimeout(toast._h);
  toast._h = setTimeout(() => (t.className = "", (t.hidden = true)), err ? 4200 : 2200);
}

async function api(cmd, args) {
  const r = await invoke(cmd, args || {});
  if (r && r.ok === false && r.error) throw new Error(r.error);
  return r;
}

/* ---------------- 会话列表 ---------------- */

function renderConvs() {
  const ul = $("convs");
  ul.textContent = "";
  if (!S.convs.length) {
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
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.onclick = () => openConv(c.id);
    li.onkeydown = (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openConv(c.id); }
    };

    const av = document.createElement("div");
    av.className = "avatar";
    av.style.background = `linear-gradient(150deg, ${avatarColor(p.id || p.name)}, ${avatarColor(p.name || p.id)})`;
    av.textContent = p.name.slice(0, 1).toUpperCase();
    if (p.online) {
      const dot = document.createElement("span");
      dot.className = "dot";
      av.appendChild(dot);
    }

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

/* ---------------- 消息区 ---------------- */

function tickHtml(state, dir) {
  if (dir !== "out") return "";
  if (state === "read") return `<span class="tick">✓✓</span>`;
  if (state === "delivered") return `<span class="tick">✓✓</span>`;
  if (state === "sent") return `<span class="tick">✓</span>`;
  if (state === "queued") return `<span class="tick" title="待补发">🕓</span>`;
  if (state === "failed") return `<span class="tick" style="color:#ff8080">✕</span>`;
  return "";
}

function fileTileSvg() {
  return `<svg viewBox="0 0 24 24"><path d="M14 3v5h5M7 3h8l5 5v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/></svg>`;
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
    const target = S.msgs.find((x) => x.id === m.reply_to);
    const t = target ? (target.body.text || "[附件]").slice(0, 40) : m.reply_to.slice(0, 8);
    ref.textContent = "↩ " + t;
    ref.onclick = () => {
      const el = $("msgs").querySelector(`[data-id="${m.reply_to}"]`);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    };
    bub.appendChild(ref);
  }

  if (m.kind === "image") {
    const holder = document.createElement("div");
    holder.className = "thumbwrap";
    holder.innerHTML = `<div class="thumb-pending">图片</div>`;
    bub.appendChild(holder);
    hydrateThumb(m.id, holder);
    const cap = document.createElement("span");
    cap.textContent = (m.body && m.body.name) || "";
    cap.style.cssText = "display:block;color:var(--dim);font-size:12px";
    bub.appendChild(cap);
  } else if (m.kind === "file") {
    const card = document.createElement("div");
    card.className = "filecard";
    const tile = document.createElement("div");
    tile.className = "ftile";
    tile.innerHTML = fileTileSvg();
    const meta = document.createElement("div");
    meta.className = "fmeta";
    const name = document.createElement("span"); name.className = "fname"; name.textContent = m.body.name || "文件";
    const size = document.createElement("span"); size.className = "fsize"; size.textContent = fmtSize(m.body.size);
    meta.append(name, size);
    const open = document.createElement("button");
    open.className = "faction"; open.textContent = "打开";
    open.onclick = async () => { try { await api("open_blob", { msg: m.id, name: m.body.name || "" }); } catch (e) { toast(e.message, true); } };
    const save = document.createElement("button");
    save.className = "faction"; save.textContent = "另存";
    save.onclick = async () => {
      const dest = await dlgSave({ defaultPath: m.body.name || "download.bin" });
      if (!dest) return;
      try { const r = await api("save_blob", { msg: m.id, dest, thumb: false }); toast(`已保存 ${r.size} 字节`); }
      catch (e) { toast(e.message, true); }
    };
    card.append(tile, meta, open, save);
    bub.appendChild(card);
  } else {
    const text = document.createElement("span");
    text.textContent = (m.body && m.body.text) || "";
    bub.appendChild(text);
    bub.ondblclick = () => {
      navigator.clipboard && navigator.clipboard.writeText((m.body && m.body.text) || "");
      toast("已复制");
    };
  }

  bub.oncontextmenu = (ev) => { ev.preventDefault(); setReply(m); };

  const meta = document.createElement("span");
  meta.className = "meta";
  meta.innerHTML = `${fmtTime(m.ts)}${tickHtml(m.state, m.direction)}`;
  bub.appendChild(meta);
  wrap.appendChild(bub);
  return wrap;
}

async function hydrateThumb(mid, holder) {
  if (S.thumbCache.has(mid)) {
    paintThumb(mid, S.thumbCache.get(mid), holder);
    return;
  }
  try {
    const r = await api("get_blob", { msg: mid, thumb: true });
    const url = `data:image/png;base64,${r.b64}`;
    S.thumbCache.set(mid, url);
    const el = $("msgs").querySelector(`[data-id="${mid}"] .thumbwrap`) || holder;
    paintThumb(mid, url, el);
  } catch (_) {
    // 无缩略图（CLI 发的）：点击直接开原件
    holder.innerHTML = `<div class="thumb-pending">图片（无缩略图）</div>`;
  }
}
function paintThumb(mid, url, holder) {
  holder.innerHTML = "";
  const img = document.createElement("img");
  img.className = "thumb";
  img.src = url;
  img.onclick = () => api("open_blob", { msg: mid, name: "image.png" }).catch((e) => toast(e.message, true));
  holder.appendChild(img);
}

function renderMsgs() {
  const box = $("msgs");
  box.textContent = "";
  let lastDay = "";
  if (S.msgs.length >= 50) {
    const older = document.createElement("button");
    older.className = "load-old";
    older.textContent = "查看更早";
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
  const m = S.msgs.find((x) => x.id === msgId);
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
    S.contacts = new Map((r.contacts || []).map((c) => [c.id, c]));
  } catch (_) {}
}

async function openConv(id) {
  S.active = id;
  S.replyTo = null; $("reply-bar").hidden = true;
  $("empty").hidden = true; $("offline").hidden = true; $("chat").hidden = false;
  const p = peerOf(id);
  $("peer-name").textContent = p.name;
  $("peer-sub").textContent = p.online ? "在线" : "离线";
  $("peer-sub").className = p.online ? "online" : "";
  $("peer-avatar").textContent = p.name.slice(0, 1).toUpperCase();
  $("peer-avatar").style.background = `linear-gradient(150deg, ${avatarColor(p.id)}, ${avatarColor(p.name)})`;
  renderConvs();
  try {
    const r = await api("read_view", { conv: id, limit: 50 });
    S.msgs = r.items || [];
    S.oldest = S.msgs.length ? S.msgs[0].ts : 0;
    renderMsgs();
    if (S.convs.some((c) => c.id === id && c.unread)) {
      await api("mark_read", { conv: id });
      const c = S.convs.find((x) => x.id === id);
      if (c) c.unread = 0;
      renderConvs();
    }
    $("input").focus();
  } catch (e) { toast(e.message, true); }
}

async function loadOlder() {
  if (!S.active || !S.oldest || S.loadingOlder) return;
  S.loadingOlder = true;
  try {
    const r = await api("history_before", { conv: S.active, before: S.oldest, limit: 50 });
    const olderItems = r.items || [];
    if (olderItems.length) {
      const box = $("msgs");
      const keepTop = box.scrollHeight - box.scrollTop;
      S.msgs = [...olderItems, ...S.msgs];
      S.oldest = olderItems[0].ts;
      renderMsgs();
      box.scrollTop = box.scrollHeight - keepTop;
    }
  } catch (e) { toast(e.message, true); }
  finally { S.loadingOlder = false; }
}

function setReply(m) {
  S.replyTo = { id: m.id, text: ((m.body && (m.body.text || m.body.name)) || "").slice(0, 40) };
  $("reply-text").textContent = S.replyTo.text || m.id.slice(0, 8);
  $("reply-bar").hidden = false;
  $("input").focus();
}

/* ---------------- 附件选择 ---------------- */

const IMG_EXT = ["png", "jpg", "jpeg", "gif", "webp", "bmp"];
const extOf = (p) => (p.split(".").pop() || "").toLowerCase();
const baseOf = (p) => p.split(/[\\/]/).pop();

async function pickFile() {
  const path = await dlgOpen({ multiple: false, title: "选择要发送的文件" });
  if (!path) return;
  const name = baseOf(path);
  const kind = IMG_EXT.includes(extOf(name)) ? "image" : "file";
  let thumb_b64 = null;
  if (kind === "image") {
    try {
      const r = await api("read_preview", { path });
      if (r.preview) thumb_b64 = await makeThumb(r.preview);
    } catch (_) {}
  }
  S.pending = { path, name, kind, thumb_b64 };
  const bar = $("pending-bar");
  bar.hidden = false;
  $("pending-name").textContent = name;
  $("pending-kind").textContent = kind === "image" ? "图片" : "附件";
  const pimg = $("pending-thumb");
  if (thumb_b64) { pimg.src = "data:image/png;base64," + thumb_b64; pimg.style.display = ""; }
  else { pimg.removeAttribute("src"); pimg.style.display = "none"; }
  $("input").focus();
}

// 前端 canvas 生成 320px 内 PNG 缩略图（不引入 Rust 图像依赖）
function makeThumb(dataUrl) {
  return new Promise((res) => {
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, 320 / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.max(1, Math.round(img.width * scale));
      c.height = Math.max(1, Math.round(img.height * scale));
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      res(c.toDataURL("image/png", 0.85).split(",")[1]);
    };
    img.onerror = () => res(null);
    img.src = dataUrl;
  });
}

function clearPending() {
  S.pending = null;
  $("pending-bar").hidden = true;
}

/* ---------------- 发送 ---------------- */

async function doSend() {
  const input = $("input");
  const text = input.value.trim();
  const p = S.pending;
  if (!p && !text) return;
  const to = S.active ? peerOf(S.active).id || peerOf(S.active).name : S.pendingTarget;
  if (!to) { toast("先点「新对话」指定对方", true); return; }
  input.value = ""; input.style.height = "auto";
  $("send").classList.add("busy");
  try {
    let r;
    if (p) {
      r = await api("send_file", { to, path: p.path, thumb_b64: p.thumb_b64, reply_to: S.replyTo ? S.replyTo.id : null });
      if (r.message && p.thumb_b64) S.thumbCache.set(r.message.id, "data:image/png;base64," + p.thumb_b64);
      clearPending();
    } else {
      r = await api("send_text", { to, text, reply_to: S.replyTo ? S.replyTo.id : null });
    }
    if (r.message) {
      if (r.message.conv !== S.active) await openConv(r.message.conv);
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

/* ---------------- 事件泵 ---------------- */

let convRefreshTimer = null;
function scheduleConvRefresh() {
  clearTimeout(convRefreshTimer);
  convRefreshTimer = setTimeout(refreshConvs, 350);
}

function onEvents(events) {
  let convDirty = false;
  let receipted = false;
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
      receipted = true;
      updateTicks(d.message_id, d.state === "read" ? "read" : "delivered");
      convDirty = d.state === "read";
    } else if (e.type === "state") {
      updateTicks(d.message_id, d.state);
    }
  }
  if (convDirty) scheduleConvRefresh();
  // 本会话的另一端也可能从别的入口（CLI/网页）收发：receipt 到达时重拉整段保证一致
  if (receipted && S.active) refetchActive();
}

let refetchBusy = false;
async function refetchActive() {
  if (!S.active || refetchBusy) return;
  refetchBusy = true;
  try {
    const box = $("msgs");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    const r = await api("read_view", { conv: S.active, limit: 50 });
    S.msgs = r.items || [];
    S.oldest = S.msgs.length ? S.msgs[0].ts : 0;
    renderMsgs();
    if (!atBottom) box.scrollTop = box.scrollHeight - box.clientHeight;
  } catch (_) {}
  finally { refetchBusy = false; }
}

/* ---------------- 初始化 ---------------- */

async function startEngineAndRetry() {
  $("start-server").textContent = "启动中…";
  try {
    const r = await api("ensure_server", {});
    if (r.running) { await boot(); return; }
    toast(r.error || "启动失败", true);
  } catch (e) { toast(e.message, true); }
  $("start-server").textContent = "启动服务";
}

async function boot() {
  let connected = false;
  try {
    const st = await api("engine_state", {});
    connected = !!st.running;
    if (connected) {
      $("offline").hidden = true;
      S.me.name = st.device_name || "?";
      S.me.id = st.device_id || "";
      S.tls = !!st.tls;
      $("me-name").textContent = S.me.name;
      $("me-state").textContent = st.tls ? "TLS 1.3 · 已加密" : "明文模式（建议 tls on）";
      $("me-state").className = st.tls ? "on" : "";
      $("foot-state").textContent = `v${(st.ping && st.ping.version) || "?"} · ${st.tls ? "TLS 1.3" : "明文"} · 端口 ${st.local_port ?? "?"}`;
    }
  } catch (e) { connected = false; }
  if (!connected) {
    if (!S.active) $("offline").hidden = false;   // 正在看会话就不遮挡
    return;
  }
  await refreshContacts();
  await refreshConvs();
  // 自动回到最近会话（幂等：首启、引擎后起、重试循环都会补触发）
  if (!S.active && !S.pendingTarget && S.convs.length && $("chat").hidden) {
    openConv(S.convs[0].id);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  window.addEventListener("error", (e) => {
    document.title = "JSERR " + String(e.message || "").slice(0, 90);
  });
  window.addEventListener("unhandledrejection", (e) => {
    document.title = "JSREJ " + String((e.reason && e.reason.message) || e.reason || "").slice(0, 90);
  });
  $("theme-toggle").onclick = () => {
    const cur = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = cur;
    localStorage.setItem("lt-theme", cur);
  };
  const saved = localStorage.getItem("lt-theme");
  if (saved) document.documentElement.dataset.theme = saved;

  $("send").onclick = doSend;
  $("attach").onclick = pickFile;
  $("pending-cancel").onclick = clearPending;
  $("start-server").onclick = startEngineAndRetry;
  $("new-chat").onclick = () => {
    const t = prompt("对方 IP[:端口] 或设备名：", S.pendingTarget || "");
    if (t && t.trim()) {
      S.pendingTarget = t.trim();
      S.active = null;
      $("chat").hidden = false; $("empty").hidden = true; $("offline").hidden = true;
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
    input.style.height = Math.min(input.scrollHeight, 130) + "px";
  });

  listen("tg-events", (ev) => onEvents(ev.payload));
  document.addEventListener("keydown", (ev) => {
    if (ev.altKey && !ev.ctrlKey && !ev.shiftKey && ev.code === "KeyS" && !$("offline").hidden) {
      ev.preventDefault();
      startEngineAndRetry();
    }
  });
  boot();
  // 引擎离线时每 4 秒自动重试；已连接但还没打开任何会话也补一次 auto-open
  setInterval(() => {
    const needBoot = !$("offline").hidden;
    const needOpen = $("chat").hidden && !S.pendingTarget;
    if (needBoot || needOpen) boot();
  }, 4000);
});
