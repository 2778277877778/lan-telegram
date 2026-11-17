// lan-telegram 桌面壳前端（Telegram Web 风格）
// 与 Rust 桥的接口：engine_state / ensure_server / read_view / history_before /
// send_text / send_file / mark_read / contact_list / get_blob / save_blob /
// open_blob / read_preview / search_messages
// 事件：tg-events（Rust 事件泵从 /events 长轮询转推）。webview 无任何网络能力。

const T = window.__TAURI__;
const { invoke } = T.core;
const { listen } = T.event;
const dlgOpen = (options) => invoke("plugin:dialog|open", { options });
const dlgSave = (options) => invoke("plugin:dialog|save", { options });

const S = {
  me: { name: "?", id: "", addr: "" },
  tls: false,
  port: null,
  convs: [],
  contacts: new Map(),
  active: null,
  msgs: [],
  oldest: 0,
  replyTo: null,
  pending: null,
  pendingTarget: null,
  loadingOlder: false,
  thumbCache: new Map(),
  uploadRef: null,
};

const $ = (id) => document.getElementById(id);
const AVATAR_CLASSES = ["av-blue", "av-purple", "av-green", "av-orange", "av-pink", "av-cyan", "av-red", "av-gray"];

function avatarClass(key) {
  let h = 0;
  for (const ch of String(key || "?")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_CLASSES[h % AVATAR_CLASSES.length];
}
function applyAvatar(el, key, text) {
  if (!el) return;
  el.className = el.className.replace(/\bav-\w+/g, "").trim();
  el.classList.add(avatarClass(key));
  el.textContent = (text || "?").slice(0, 1).toUpperCase();
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
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function peerOf(convId) {
  const dev = convId.startsWith("dm:") ? convId.slice(3) : "";
  const c = S.convs.find((x) => x.id === convId);
  const known = S.contacts.get(dev);
  const ip = (known && known.ip) || "";
  const port = (known && known.port) || "";
  const name = (c && c.title) || (known && known.name) || dev.slice(0, 8) || "?";
  const fresh = known && known.last_seen && Date.now() - known.last_seen < 400000;
  return { id: dev, name, ip, port, addr: ip ? `${ip}:${port}` : "", online: !!fresh };
}

function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  requestAnimationFrame(() => (t.className = "show" + (err ? " err" : "")));
  clearTimeout(toast._h);
  toast._h = setTimeout(() => { t.className = ""; t.hidden = true; }, err ? 4200 : 2200);
}

async function api(cmd, args) {
  const r = await invoke(cmd, args || {});
  if (r && r.ok === false && r.error) throw new Error(r.error);
  return r;
}

/* ------------------------------- 会话列表 ------------------------------- */

function renderConvs() {
  const ul = $("convs");
  ul.textContent = "";
  if (!S.convs.length) {
    const li = document.createElement("li");
    li.className = "ci-empty";
    li.textContent = "还没有对话\n点「新对话」发第一条";
    li.style.whiteSpace = "pre-line";
    ul.appendChild(li);
    return;
  }
  for (const c of S.convs) {
    const p = peerOf(c.id);
    const li = document.createElement("li");
    li.className = "chat-item" + (c.id === S.active ? " active" : "");
    li.onclick = () => openConv(c.id);

    const av = document.createElement("div");
    av.className = "avatar";
    applyAvatar(av, p.id || p.name, p.name);
    if (p.online) {
      const dot = document.createElement("span");
      dot.className = "dot";
      av.appendChild(dot);
    }

    const main = document.createElement("div");
    main.className = "ci-main";
    const r1 = document.createElement("div");
    r1.className = "ci-row";
    const nm = document.createElement("span");
    nm.className = "ci-name";
    nm.textContent = p.name;
    const tm = document.createElement("span");
    tm.className = "ci-time";
    tm.textContent = fmtTime(c.last_msg_at);
    r1.append(nm, tm);

    const r2 = document.createElement("div");
    r2.className = "ci-row2";
    const pv = document.createElement("span");
    pv.className = "ci-preview";
    pv.textContent = c.preview || (p.addr || "—");
    r2.append(pv);
    if (c.unread) {
      const b = document.createElement("span");
      b.className = "ci-badge";
      b.textContent = c.unread;
      r2.append(b);
    }
    main.append(r1, r2);
    li.append(av, main);
    ul.appendChild(li);
  }
}

/* ------------------------------- 消息气泡 ------------------------------- */

function tickSvg(state) {
  if (state === "read") {
    return `<span class="tick"><svg viewBox="0 0 24 24"><path d="M1.5 12.5l4.5 4.5L15 8"/><path d="M9 15.5l2 2L22 8"/></svg></span>`;
  }
  if (state === "delivered") {
    return `<span class="tick"><svg viewBox="0 0 24 24"><path d="M1.5 12.5l4.5 4.5L15 8"/><path d="M9 15.5l2 2L22 8"/></svg></span>`;
  }
  if (state === "sent") {
    return `<span class="tick"><svg viewBox="0 0 24 24"><path d="M4 12.5l4.5 4.5L19 7"/></svg></span>`;
  }
  if (state === "queued") {
    return `<span class="tick"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg></span>`;
  }
  if (state === "failed") {
    return `<span class="tick" style="color:#e5484d"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></span>`;
  }
  return "";
}

function makeBubble(m) {
  const row = document.createElement("div");
  row.className = "msg " + (m.direction === "out" ? "out" : "in");
  row.dataset.id = m.id;

  const bub = document.createElement("div");
  bub.className = "bubble";

  if (m.reply_to) {
    const ref = document.createElement("div");
    ref.className = "reply-ref";
    const target = S.msgs.find((x) => x.id === m.reply_to);
    ref.textContent = "↩ " + (target ? ((target.body && (target.body.text || target.body.name)) || "附件") : m.reply_to.slice(0, 8));
    ref.onclick = () => {
      const el = $("msgs").querySelector(`[data-id="${m.reply_to}"]`);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    };
    bub.appendChild(ref);
  }

  if (m.kind === "image") {
    bub.classList.add("media");
    const holder = document.createElement("div");
    holder.className = "thumbwrap";
    holder.innerHTML = `<div class="thumb-pending">图片</div>`;
    bub.appendChild(holder);
    hydrateThumb(m.id, holder);
  } else if (m.kind === "file") {
    const card = document.createElement("div");
    card.className = "filecard";
    const tile = document.createElement("div");
    tile.className = "ftile";
    tile.innerHTML = `<svg viewBox="0 0 24 24"><path d="M14 3v5h5M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/></svg>`;
    const meta = document.createElement("div");
    meta.className = "fmeta";
    const name = document.createElement("span");
    name.className = "fname";
    name.textContent = (m.body && m.body.name) || "文件";
    const size = document.createElement("span");
    size.className = "fsize";
    size.textContent = fmtSize(m.body && m.body.size);
    const act = document.createElement("div");
    act.className = "factions";
    const open = document.createElement("button");
    open.className = "faction";
    open.textContent = "打开";
    open.onclick = () => api("open_blob", { msg: m.id, name: (m.body && m.body.name) || "" }).catch((e) => toast(e.message, true));
    const save = document.createElement("button");
    save.className = "faction";
    save.textContent = "另存";
    save.onclick = async () => {
      const dest = await dlgSave({ defaultPath: (m.body && m.body.name) || "download.bin" });
      if (!dest) return;
      try { const r = await api("save_blob", { msg: m.id, dest, thumb: false }); toast(`已保存 ${fmtSize(r.size)}`); }
      catch (e) { toast(e.message, true); }
    };
    act.append(open, save);
    meta.append(name, size, act);
    card.append(tile, meta);
    bub.appendChild(card);
  } else {
    const body = document.createElement("div");
    body.className = "body";
    body.textContent = (m.body && m.body.text) || "";
    bub.appendChild(body);
    bub.ondblclick = () => {
      navigator.clipboard && navigator.clipboard.writeText((m.body && m.body.text) || "");
      toast("已复制");
    };
  }

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.innerHTML = `<span>${fmtTime(m.ts)}</span>${m.direction === "out" ? tickSvg(m.state) : ""}`;
  bub.appendChild(meta);

  bub.oncontextmenu = (ev) => { ev.preventDefault(); setReply(m); };
  row.appendChild(bub);
  return row;
}

async function hydrateThumb(mid, holder) {
  if (S.thumbCache.has(mid)) { paintThumb(mid, S.thumbCache.get(mid), holder); return; }
  try {
    const r = await api("get_blob", { msg: mid, thumb: true });
    const url = `data:image/png;base64,${r.b64}`;
    S.thumbCache.set(mid, url);
    const el = $("msgs").querySelector(`[data-id="${mid}"] .thumbwrap`) || holder;
    paintThumb(mid, url, el);
  } catch (_) {
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
  const inner = document.createElement("div");
  inner.className = "msgs-inner";
  box.appendChild(inner);

  if (S.msgs.length >= 50) {
    const older = document.createElement("button");
    older.className = "load-old";
    older.textContent = "查看更早消息";
    older.onclick = loadOlder;
    inner.appendChild(older);
  }
  let lastDay = "";
  for (const m of S.msgs) {
    const day = fmtDay(m.ts);
    if (day !== lastDay) {
      lastDay = day;
      const sep = document.createElement("div");
      sep.className = "day-sep";
      sep.textContent = day;
      inner.appendChild(sep);
    }
    inner.appendChild(makeBubble(m));
  }
  box.scrollTop = box.scrollHeight;
}

function updateTicks(msgId, state) {
  const m = S.msgs.find((x) => x.id === msgId);
  if (m && m.direction === "out") {
    m.state = state;
    const el = $("msgs").querySelector(`[data-id="${msgId}"] .meta`);
    if (el) el.innerHTML = `<span>${fmtTime(m.ts)}</span>${tickSvg(m.state)}`;
  }
}

/* ------------------------------- 信息栏 ------------------------------- */

function updateInfoPanel(convId) {
  const p = peerOf(convId);
  applyAvatar($("info-avatar"), p.id || p.name, p.name);
  $("info-name").textContent = p.name;
  $("info-ip").textContent = p.ip || "—";
  $("info-port").textContent = p.port || "—";
  $("info-devid").textContent = p.id ? p.id.slice(0, 16) + "…" : "—";
  $("info-status").textContent = p.online ? "在线" : "离线 / 最近联系过";
  $("info-status").className = "info-sub" + (p.online ? " online" : "");
  $("info-tls").textContent = S.tls ? "TLS 1.3 已启用" : "明文（建议 tls on）";
}
function resetInfoPanel() {
  applyAvatar($("info-avatar"), "?", "?");
  $("info-name").textContent = "—";
  $("info-ip").textContent = "—";
  $("info-port").textContent = "—";
  $("info-devid").textContent = "—";
  $("info-status").textContent = "未选择会话";
  $("info-tls").textContent = S.tls ? "TLS 1.3 已启用" : "明文";
}

function setMyAddress(addr) {
  S.me.addr = addr || "";
  $("me-ip").textContent = S.me.addr || "未获取到 IP";
  $("empty-ip").textContent = S.me.addr || "—";
  $("info-local").textContent = S.me.addr || "—";
}

async function copyMyAddress() {
  const a = S.me.addr || "";
  if (!a) { toast("本机地址还没取到", true); return; }
  try {
    await navigator.clipboard.writeText(a);
    toast(`已复制 ${a}`);
  } catch (_) { toast(`本机地址：${a}`); }
}

/* ------------------------------- 数据动作 ------------------------------- */

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
  S.replyTo = null;
  $("reply-bar").hidden = true;
  $("empty").hidden = true;
  $("offline").hidden = true;
  $("chat").hidden = false;

  const p = peerOf(id);
  applyAvatar($("peer-avatar"), p.id || p.name, p.name);
  $("peer-name").textContent = p.name;
  $("peer-sub").textContent = (p.addr ? p.addr + " · " : "") + (p.online ? "在线" : "离线");
  $("peer-sub").className = "peer-sub" + (p.online ? " online" : "");
  updateInfoPanel(id);
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
    const older = r.items || [];
    if (older.length) {
      const box = $("msgs");
      const keep = box.scrollHeight - box.scrollTop;
      S.msgs = [...older, ...S.msgs];
      S.oldest = older[0].ts;
      renderMsgs();
      box.scrollTop = box.scrollHeight - keep;
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

/* ------------------------------- 搜索 ------------------------------- */

function highlight(text, q) {
  const safe = esc(text);
  if (!q) return safe;
  const needle = esc(q);
  const lower = safe.toLowerCase();
  const n = needle.toLowerCase();
  let out = "", i = 0;
  while (true) {
    const at = lower.indexOf(n, i);
    if (at < 0) { out += safe.slice(i); break; }
    out += safe.slice(i, at) + "<b>" + safe.slice(at, at + n.length) + "</b>";
    i = at + n.length;
  }
  return out;
}

function convLabel(it) {
  const conv = it.conv || "";
  if (conv.startsWith("dm:")) return { who: it.dev_name || conv.slice(3, 11), tag: "" };
  return { who: it.conv_title || conv, tag: "" };
}

function renderResults(items, q) {
  const ul = $("results");
  ul.textContent = "";
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "res-empty";
    li.textContent = `没有找到包含「${q}」的消息`;
    ul.appendChild(li);
    return;
  }
  for (const it of items) {
    const lbl = convLabel(it);
    const li = document.createElement("li");
    li.className = "res";
    const head = document.createElement("div");
    head.className = "res-head";
    const who = document.createElement("span");
    who.className = "res-who";
    who.textContent = lbl.who;
    const tm = document.createElement("span");
    tm.className = "res-time";
    tm.textContent = fmtTime(it.ts);
    head.append(who, tm);
    const body = document.createElement("div");
    body.className = "res-text";
    const b = it.body || {};
    const raw = it.kind === "file" ? `[文件] ${b.name || ""}`
      : it.kind === "image" ? `[图片] ${b.name || ""}` : (b.text || "");
    body.innerHTML = highlight(raw, q);
    li.append(head, body);
    li.onclick = () => { clearSearch(); openConv(it.conv); };
    ul.appendChild(li);
  }
}

function clearSearch() {
  $("search").value = "";
  $("search-clear").hidden = true;
  $("results").hidden = true;
  $("convs").hidden = false;
}

let searchTimer = null;
async function doSearch(q) {
  q = (q || "").trim();
  if (!q) { clearSearch(); return; }
  $("search-clear").hidden = false;
  $("convs").hidden = true;
  $("results").hidden = false;
  try {
    const r = await api("search_messages", { q, limit: 50 });
    renderResults(r.items || [], q);
  } catch (e) {
    renderResults([], q);
    toast(e.message, true);
  }
}

/* ------------------------------- 附件与发送 ------------------------------- */

const IMG_EXT = ["png", "jpg", "jpeg", "gif", "webp", "bmp"];
const extOf = (p) => (p.split(".").pop() || "").toLowerCase();
const baseOf = (p) => p.split(/[\\/]/).pop();

function clearPending() {
  S.pending = null;
  $("pending-bar").hidden = true;
  $("pending-progress").hidden = true;
}

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
  $("pending-bar").hidden = false;
  $("pending-name").textContent = name;
  $("pending-kind").textContent = kind === "image" ? "图片" : "附件";
  const img = $("pending-thumb");
  if (thumb_b64) { img.src = "data:image/png;base64," + thumb_b64; img.style.display = ""; }
  else { img.removeAttribute("src"); img.style.display = "none"; }
  $("input").focus();
}

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

async function doSend() {
  const input = $("input");
  const text = input.value.trim();
  const p = S.pending;
  if (!p && !text) return;
  const to = S.active ? (peerOf(S.active).id || peerOf(S.active).name) : S.pendingTarget;
  if (!to) { toast("先点「新对话」指定对方", true); return; }
  input.value = "";
  input.style.height = "auto";
  $("send").classList.add("busy");
  try {
    let r;
    if (p) {
      const ref = "u" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
      S.uploadRef = ref;
      $("pending-progress").hidden = false;
      $("pending-fill").style.width = "0%";
      $("pending-pct").textContent = "上传中";
      r = await api("send_file", {
        to, path: p.path, thumb_b64: p.thumb_b64,
        reply_to: S.replyTo ? S.replyTo.id : null, client_ref: ref,
      });
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
    S.replyTo = null;
    $("reply-bar").hidden = true;
    await refreshConvs();
  } catch (e) {
    toast(e.message, true);
  } finally {
    S.uploadRef = null;
    $("pending-progress").hidden = true;
    $("send").classList.remove("busy");
  }
}

/* ------------------------------- 事件泵 ------------------------------- */

let convRefreshTimer = null;
function scheduleConvRefresh() {
  clearTimeout(convRefreshTimer);
  convRefreshTimer = setTimeout(refreshConvs, 350);
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

function onEvents(events) {
  let convDirty = false, receipted = false;
  for (const e of events) {
    const d = e.data || {};
    if (e.type === "message") {
      convDirty = true;
      const convId = "dm:" + (d.from || "");
      if (convId === S.active && d.from !== S.me.id) refetchActive().then(() => api("mark_read", { conv: convId }).catch(() => {}));
    } else if (e.type === "receipt") {
      receipted = true;
      updateTicks(d.message_id, d.state === "read" ? "read" : "delivered");
      convDirty = d.state === "read";
    } else if (e.type === "state") {
      updateTicks(d.message_id, d.state);
    } else if (e.type === "progress") {
      if (S.uploadRef && d.client_ref === S.uploadRef) {
        const pct = Math.max(0, Math.min(100, d.percent || 0));
        $("pending-fill").style.width = pct + "%";
        $("pending-pct").textContent = pct + "%";
        $("pending-progress").hidden = false;
      }
    }
  }
  if (convDirty) scheduleConvRefresh();
  if (receipted && S.active) refetchActive();
}

/* ------------------------------- 新对话弹层 ------------------------------- */

function openModal() {
  $("modal").hidden = false;
  $("peer-input").value = "";
  renderKnownPeers();
  setTimeout(() => $("peer-input").focus(), 30);
}
function closeModal() { $("modal").hidden = true; }

function renderKnownPeers() {
  const ul = $("known-peers");
  ul.textContent = "";
  const list = [...S.contacts.values()].filter((c) => c.id !== S.me.id && c.ip);
  if (!list.length) {
    const li = document.createElement("li");
    li.textContent = "还没有联系过的设备";
    li.style.cursor = "default";
    li.style.color = "var(--text-3)";
    ul.appendChild(li);
    return;
  }
  for (const c of list.slice(0, 8)) {
    const li = document.createElement("li");
    const nm = document.createElement("span");
    nm.className = "kp-name";
    nm.textContent = c.name || c.id.slice(0, 8);
    const ip = document.createElement("span");
    ip.className = "kp-ip";
    ip.textContent = `${c.ip}:${c.port || S.port || 32200}`;
    li.append(nm, ip);
    li.onclick = () => {
      closeModal();
      startChatWith(`${c.ip}:${c.port || S.port || 32200}`);
    };
    ul.appendChild(li);
  }
}

function startChatWith(target) {
  S.pendingTarget = target;
  S.active = null;
  $("chat").hidden = false;
  $("empty").hidden = true;
  $("offline").hidden = true;
  $("peer-name").textContent = target;
  $("peer-sub").textContent = "输入消息并发送，即建立对话";
  $("peer-sub").className = "peer-sub";
  applyAvatar($("peer-avatar"), target, target);
  resetInfoPanel();
  $("info-name").textContent = target;
  $("info-ip").textContent = target.split(":")[0];
  $("info-port").textContent = target.split(":")[1] || String(S.port || 32200);
  $("info-status").textContent = "待发送首条消息";
  $("msgs").textContent = "";
  $("input").focus();
}

/* ------------------------------- 引擎状态 ------------------------------- */

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
  let st = null;
  try {
    st = await api("engine_state", {});
  } catch (e) {
    st = null;
  }
  if (!st || !st.ping) {
    if (!S.active) $("offline").hidden = false;
    return;
  }
  $("offline").hidden = true;
  const ping = st.ping || {};
  S.me.name = ping.device || st.device_name || "?";
  S.me.id = ping.device_id || st.device_id || "";
  S.tls = !!st.tls;
  S.port = ping.port || null;
  $("me-name").textContent = S.me.name;
  applyAvatar($("me-avatar"), S.me.id || S.me.name, S.me.name);
  const ips = ping.ips || [];
  setMyAddress(ips.length && S.port ? `${ips[0]}:${S.port}` : "");
  $("foot-state").textContent = `v${ping.version || "?"} · ${S.tls ? "TLS 1.3" : "明文"} · 端口 ${S.port || "?"}`;
  $("info-tls").textContent = S.tls ? "TLS 1.3 已启用" : "明文（建议 tls on）";

  await refreshContacts();
  await refreshConvs();
  if (!S.active && !S.pendingTarget && S.convs.length && $("chat").hidden) {
    openConv(S.convs[0].id);
  } else if (S.active) {
    updateInfoPanel(S.active);
  }
}

/* ------------------------------- 初始化 ------------------------------- */

document.addEventListener("DOMContentLoaded", () => {
  window.addEventListener("error", (e) => { document.title = "JSERR " + String(e.message || "").slice(0, 80); });

  $("theme-toggle").onclick = () => {
    const cur = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = cur;
    localStorage.setItem("lt-theme", cur);
  };
  const saved = localStorage.getItem("lt-theme");
  if (saved) document.documentElement.dataset.theme = saved;

  $("me-addr").onclick = copyMyAddress;
  $("info-local-row").onclick = copyMyAddress;
  $("info-close").onclick = () => { $("infopanel").classList.add("collapsed"); };
  $("chat-head").onclick = () => { $("infopanel").classList.remove("collapsed"); };

  $("new-chat").onclick = openModal;
  $("modal-close").onclick = closeModal;
  $("peer-go").onclick = () => {
    const v = $("peer-input").value.trim();
    if (!v) { toast("请填写对方 IP", true); return; }
    closeModal();
    startChatWith(v);
  };
  $("peer-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("peer-go").click();
    if (e.key === "Escape") closeModal();
  });
  $("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });

  $("send").onclick = doSend;
  $("attach").onclick = pickFile;
  $("pending-cancel").onclick = clearPending;
  $("start-server").onclick = startEngineAndRetry;
  $("reply-cancel").onclick = () => { S.replyTo = null; $("reply-bar").hidden = true; };

  const search = $("search");
  search.addEventListener("input", () => {
    const q = search.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => doSearch(q), 260);
  });
  search.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { clearTimeout(searchTimer); clearSearch(); search.blur(); }
    if (ev.key === "Enter") { clearTimeout(searchTimer); doSearch(search.value); }
  });
  $("search-clear").onclick = () => { clearTimeout(searchTimer); clearSearch(); search.focus(); };

  const input = $("input");
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); doSend(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 130) + "px";
  });

  listen("tg-events", (ev) => onEvents(ev.payload));
  boot();
  setInterval(() => {
    const needBoot = !$("offline").hidden;
    const needOpen = $("chat").hidden && !S.pendingTarget;
    if (needBoot || needOpen) boot();
  }, 4000);
});
