#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lan-telegram —— 局域网聊天（M3：桌面 GUI 就绪的服务端）

设计文档见 docs/lan-telegram-design.md。
M1：本地存储 + 点对点协议 + 命令行；M2：TLS 1.3 + 指纹固定。
M3 新增：桌面/网页 GUI 所需端点（/api/v2/send、/conversations、/contacts、/read），
投递/已读逻辑抽为 server_send / server_mark_read，与 CLI 完全同源（消除行为分叉）。
明文 HTTP 下行为与 M1/M2 一致。

用法：
    python lantelegram.py info
    python lantelegram.py serve --background
    python lantelegram.py send --target 10.10.156.243 --text "在吗"
    python lantelegram.py history --with 10.10.156.243
    python lantelegram.py cert                      # 本机证书指纹（给对方核对）
    python lantelegram.py trust --target <IP:端口> --expect <指纹前缀>   # 固定对端
    python lantelegram.py tls on                    # 两端配对完成后启用 TLS 1.3
"""

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import secrets
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.3.0"
SERVICE = "lan-telegram"
DEFAULT_PORT = 32200
CREATE_NO_WINDOW = 0x08000000
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
UPLOAD_MAX = 500 * 1024 * 1024      # 单文件上限 500 MB
THUMB_MAX = 2 * 1024 * 1024         # 缩略图上限 2 MB

# ---------------------------------------------------------------- 路径与配置


def home_dir() -> Path:
    env = os.environ.get("LAN_TG_HOME")
    return Path(env) if env else Path.home() / ".lan-telegram"


def paths() -> dict:
    h = home_dir()
    return {
        "root": h,
        "config": h / "config.json",
        "db": h / "messages.db",
        "state": h / "state.json",
        "log": h / "server.log",
        "certs": h / "certs",
        "self_cert": h / "certs" / "cert.pem",
        "self_key": h / "certs" / "key.pem",
        "pins": h / "certs" / "pins",
        "pins_meta": h / "certs" / "pins.json",
        "attachments": h / "attachments",
    }


def attach_dir() -> Path:
    d = paths()["attachments"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_config() -> dict:
    p = paths()
    p["root"].mkdir(parents=True, exist_ok=True)
    cfg = {
        "device_id": "",
        "device_name": socket.gethostname(),
        "port": DEFAULT_PORT,
        "key": "",
        "tls": False,
    }
    if p["config"].exists():
        try:
            cfg.update(json.loads(p["config"].read_text(encoding="utf-8-sig")))
        except Exception:
            pass
    if not cfg.get("device_id"):
        cfg["device_id"] = uuid.uuid4().hex
    if not cfg.get("key"):
        cfg["key"] = secrets.token_hex(8)
    save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    p = paths()
    p["root"].mkdir(parents=True, exist_ok=True)
    p["config"].write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def read_state() -> dict:
    p = paths()["state"]
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
    return {}


def write_state(st: dict) -> None:
    try:
        paths()["state"].write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_ts(ms: int) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M:%S")
    except Exception:
        return "?"


def die(msg: str, code: int = 1):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- TLS 证书与指纹

# 自签证书：Ed25519 + BasicConstraints CA:TRUE（自锚信任根）+ EKU serverAuth/clientAuth，
# CN 内嵌本机 device_id（lan-telegram/<device_id>）。对端验证走「指纹固定」，不走公网 CA。


def cert_fingerprint(pem) -> str:
    """证书 DER 的 SHA-256，小写 hex。"""
    from cryptography import x509 as _x
    from cryptography.hazmat.primitives.serialization import Encoding
    if isinstance(pem, str):
        pem = pem.encode("utf-8")
    der = _x.load_pem_x509_certificate(pem).public_bytes(Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def fp_groups(hex_fp: str, groups: int = 8) -> str:
    """指纹转人工核对格式：8 组 4 位大写十六进制。"""
    return "-".join(hex_fp[i:i + 4].upper() for i in range(0, groups * 4, 4))


def normalize_expect(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch in "0123456789abcdef")


def cn_device_from_pem(pem) -> str:
    from cryptography import x509 as _x
    if isinstance(pem, str):
        pem = pem.encode("utf-8")
    cert = _x.load_pem_x509_certificate(pem)
    for at in cert.subject.get_attributes_for_oid(_x.oid.NameOID.COMMON_NAME):
        cn = str(at.value)
        if cn.startswith(SERVICE + "/"):
            return cn.split("/", 1)[1]
    return ""


def ensure_self_cert(cfg):
    """生成或读取本机自签证书，返回 (指纹hex, PEM bytes)。"""
    p = paths()
    if p["self_cert"].exists() and p["self_key"].exists():
        pem = p["self_cert"].read_bytes()
        return cert_fingerprint(pem), pem
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError:
        raise RuntimeError("TLS 功能需要 cryptography 库：pip install cryptography")
    key = Ed25519PrivateKey.generate()
    subj = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"{SERVICE}/{cfg['device_id']}"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, SERVICE),
    ])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subj).issuer_name(subj)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=True, crl_sign=True,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                                  ExtendedKeyUsageOID.CLIENT_AUTH]),
                           critical=False)
            .sign(key, None))
    p["certs"].mkdir(parents=True, exist_ok=True)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    p["self_cert"].write_bytes(cert_pem)
    p["self_key"].write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return cert_fingerprint(cert_pem), cert_pem


def load_pins() -> dict:
    p = paths()["pins_meta"]
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
    return {}


def add_pin(fp: str, pem: bytes, source: str = "") -> None:
    p = paths()
    p["pins"].mkdir(parents=True, exist_ok=True)
    (p["pins"] / f"{fp}.pem").write_bytes(pem)
    meta = load_pins()
    dev = cn_device_from_pem(pem)
    meta[fp] = {"device": dev, "source": source, "added": now_ms()}
    p["pins_meta"].write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                              encoding="utf-8")


def remove_pin(prefix: str) -> bool:
    prefix = normalize_expect(prefix)
    meta = load_pins()
    for fp in list(meta):
        if fp.startswith(prefix):
            meta.pop(fp, None)
            (paths()["pins"] / f"{fp}.pem").unlink(missing_ok=True)
            paths()["pins_meta"].write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
            return True
    return False


def pinned_cadata() -> str:
    out = []
    pins = paths()["pins"]
    if pins.exists():
        for f in sorted(pins.glob("*.pem")):
            out.append(f.read_text(encoding="utf-8", errors="ignore"))
    return "".join(out)


def build_tls_context(cfg, server: bool) -> ssl.SSLContext:
    """仅 TLS 1.3；信任库=已固定对端证书；双向要求客户端证书。"""
    ensure_self_cert(cfg)
    cadata = pinned_cadata()
    if not cadata:
        raise RuntimeError("还没有固定任何对端证书：先用 trust 配对")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(str(paths()["self_cert"]), str(paths()["self_key"]))
    ctx.load_verify_locations(cadata=cadata)
    ctx.verify_mode = ssl.CERT_REQUIRED
    if not server:
        ctx.check_hostname = False   # 身份靠指纹固定，不靠主机名
    return ctx


def tls_client_ctx(cfg):
    return build_tls_context(cfg, server=False) if cfg.get("tls") else None


# ---------------------------------------------------------------- 存储

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
  id TEXT PRIMARY KEY,
  name TEXT,
  ip TEXT,
  port INTEGER,
  last_seen INTEGER,
  trusted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conversations(
  id TEXT PRIMARY KEY,
  peer_device TEXT,
  title TEXT,
  created_at INTEGER,
  last_msg_at INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY,
  conv TEXT NOT NULL,
  sender TEXT,
  ts INTEGER,
  local_ts INTEGER,
  kind TEXT,
  body TEXT,
  state TEXT,
  reply_to TEXT,
  direction TEXT,
  read INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv, ts);
CREATE TABLE IF NOT EXISTS receipts(
  message_id TEXT, device TEXT, state TEXT, ts INTEGER,
  PRIMARY KEY(message_id, device)
);
CREATE TABLE IF NOT EXISTS outbox(
  message_id TEXT PRIMARY KEY,
  attempts INTEGER DEFAULT 0,
  next_try INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT,
  payload TEXT,
  ts INTEGER
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

OUTBOX_BACKOFF_MS = (2_000, 5_000, 15_000, 30_000, 60_000)


class Store:
    """SQLite 封装。服务端多线程访问，用一把锁串行化。"""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    # ---- 设备 / 会话
    def upsert_device(self, did, name="", ip="", port=DEFAULT_PORT):
        with self.lock:
            self.conn.execute(
                "INSERT INTO devices(id,name,ip,port,last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE devices.name END,"
                "ip=CASE WHEN excluded.ip<>'' THEN excluded.ip ELSE devices.ip END,"
                "port=excluded.port, last_seen=excluded.last_seen",
                (did, name or "", ip, int(port), now_ms()))
            self.conn.commit()

    def get_device(self, did):
        with self.lock:
            row = self.conn.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()
            return dict(row) if row else None

    def find_device(self, text):
        """按 IP / 设备名 / 设备 ID 找设备。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM devices WHERE id=? OR ip=? OR name=? LIMIT 1",
                (text, text, text)).fetchone()
            return dict(row) if row else None

    def ensure_conv(self, conv_id, peer_device, title=""):
        with self.lock:
            self.conn.execute(
                "INSERT INTO conversations(id,peer_device,title,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=CASE WHEN excluded.title<>'' "
                "THEN excluded.title ELSE conversations.title END",
                (conv_id, peer_device, title or "", now_ms()))
            self.conn.commit()

    def get_conv(self, conv_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            return dict(row) if row else None

    def list_convs(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT c.*, d.name AS dev_name, d.ip AS dev_ip, "
                " (SELECT body FROM messages m WHERE m.conv=c.id ORDER BY ts DESC LIMIT 1) AS last_body,"
                " (SELECT COUNT(*) FROM messages m WHERE m.conv=c.id AND m.direction='in' AND m.read=0) AS unread"
                " FROM conversations c LEFT JOIN devices d ON d.id=c.peer_device"
                " ORDER BY c.last_msg_at DESC").fetchall()
            return [dict(r) for r in rows]

    # ---- 消息
    def add_message(self, env, direction, state):
        """写入消息；返回 True 表示是新消息（幂等去重）。"""
        body = json.dumps(env.get("body") or {}, ensure_ascii=False)
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO messages"
                "(id,conv,sender,ts,local_ts,kind,body,state,reply_to,direction)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (env["id"], env.get("conv"), env.get("from"), int(env.get("ts") or now_ms()),
                 now_ms(), env.get("kind", "text"), body, state,
                 env.get("reply_to") or "", direction))
            self.conn.execute(
                "UPDATE conversations SET last_msg_at=? WHERE id=?",
                (int(env.get("ts") or now_ms()), env.get("conv")))
            self.conn.commit()
            return cur.rowcount > 0

    def get_message(self, mid):
        with self.lock:
            row = self.conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            return dict(row) if row else None

    def set_state(self, mid, state):
        with self.lock:
            self.conn.execute("UPDATE messages SET state=? WHERE id=?", (state, mid))
            self.conn.commit()

    def messages(self, conv, limit=50, before_ts=None):
        with self.lock:
            if before_ts:
                rows = self.conn.execute(
                    "SELECT * FROM messages WHERE conv=? AND ts<? ORDER BY ts DESC LIMIT ?",
                    (conv, int(before_ts), limit)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM messages WHERE conv=? ORDER BY ts DESC LIMIT ?",
                    (conv, limit)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def unread_inbound(self, conv=None):
        with self.lock:
            if conv:
                rows = self.conn.execute(
                    "SELECT * FROM messages WHERE direction='in' AND read=0 AND conv=?",
                    (conv,)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM messages WHERE direction='in' AND read=0").fetchall()
            return [dict(r) for r in rows]

    def mark_read(self, conv=None):
        with self.lock:
            if conv:
                self.conn.execute("UPDATE messages SET read=1 WHERE direction='in' AND conv=?", (conv,))
            else:
                self.conn.execute("UPDATE messages SET read=1 WHERE direction='in'")
            self.conn.commit()

    def add_receipt(self, mid, device, state):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO receipts(message_id,device,state,ts) VALUES(?,?,?,?)",
                (mid, device, state, now_ms()))
            self.conn.commit()

    # ---- 事件流（长轮询用）
    def add_event(self, etype, payload):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO events(type,payload,ts) VALUES(?,?,?)",
                (etype, json.dumps(payload, ensure_ascii=False), now_ms()))
            self.conn.commit()
            return cur.lastrowid

    def events_since(self, seq):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 200", (int(seq),)).fetchall()
            out = []
            for r in rows:
                try:
                    data = json.loads(r["payload"])
                except Exception:
                    data = {}
                out.append({"seq": r["seq"], "type": r["type"], "data": data})
            return out

    # ---- 待发队列
    def enqueue(self, mid):
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO outbox(message_id,attempts,next_try) VALUES(?,0,0)", (mid,))
            self.conn.commit()

    def dequeue(self, mid):
        with self.lock:
            self.conn.execute("DELETE FROM outbox WHERE message_id=?", (mid,))
            self.conn.commit()

    def due_outbox(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM outbox WHERE next_try<=? ORDER BY next_try LIMIT 20",
                (now_ms(),)).fetchall()
            return [dict(r) for r in rows]

    def bump_outbox(self, mid, attempts):
        # 与设计文档一致：2s / 5s / 15s / 30s / 60s 封顶
        delay = OUTBOX_BACKOFF_MS[min(attempts, len(OUTBOX_BACKOFF_MS)) - 1]
        with self.lock:
            self.conn.execute("UPDATE outbox SET attempts=?, next_try=? WHERE message_id=?",
                              (attempts, now_ms() + delay, mid))
            self.conn.commit()

    # ---- meta
    def meta_get(self, k, default=""):
        with self.lock:
            row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            return row["v"] if row else default

    def meta_set(self, k, v):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))
            self.conn.commit()

    def devices_public(self):
        """设备表（无敏感字段，devices 不含密钥）。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id,name,ip,port,last_seen FROM devices ORDER BY last_seen DESC").fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- GUI 共享动作

def env_row(store, mid):
    """取一条消息的前端展示形状。"""
    msg = store.get_message(mid)
    if not msg:
        return None
    try:
        body = json.loads(msg["body"] or "{}")
    except Exception:
        body = {}
    return {"id": msg["id"], "conv": msg["conv"], "sender": msg["sender"],
            "ts": msg["ts"], "kind": msg["kind"], "body": body,
            "state": msg["state"], "direction": msg["direction"],
            "reply_to": msg["reply_to"] or None, "read": msg["read"]}


def server_send(store, cfg, ref, text=None, reply_to=None, path=None, thumb_b64=None):
    """服务端发送：文本或文件/图片（hello→建档→投递，失败入队补发）。

    ref 可以是设备名 / 设备ID / IP / IP:端口；返回结构化结果供 CLI 与 GUI 共用。
    path 给出时走附件链路：先拷贝进本机 attachments（补发可重传），kind 按扩展名。
    """
    # ref 可以是设备名 / 设备ID / IP / IP:端口：先拆可能的端口，再按整体与主机名查设备表
    host, _, pstr = ref.rpartition(":")
    ip_hint, port_hint = (host, int(pstr)) if host and pstr.isdigit() else (ref, None)
    dev = store.find_device(ref) or store.find_device(ip_hint)
    ip = (dev or {}).get("ip") or ip_hint
    port = port_hint or int((dev or {}).get("port") or DEFAULT_PORT)
    if not ip:
        return {"ok": False, "error": f"未知目标 {ref}"}
    tctx = tls_client_ctx(cfg)

    # ---- 组装消息（文本 / 附件）
    if path:
        fp = Path(str(path).strip()).expanduser()
        if not fp.is_file():
            return {"ok": False, "error": f"文件不存在: {fp}"}
        size = fp.stat().st_size
        if size <= 0:
            return {"ok": False, "error": "空文件不发送"}
        if size > UPLOAD_MAX:
            return {"ok": False, "error": f"文件超过 {UPLOAD_MAX // 1048576} MB 上限"}
        ext = fp.suffix.lower().lstrip(".")
        kind = "image" if ext in IMAGE_EXT else "file"
        body = {"name": fp.name, "size": size}
    elif text:
        text = text.strip()
        kind, body = "text", {"text": text}
        if not text:
            return {"ok": False, "error": "消息为空"}
    else:
        return {"ok": False, "error": "缺少 text 或 path"}

    mid = uuid.uuid4().hex
    env = {"id": mid, "from": cfg["device_id"], "from_name": cfg.get("device_name"),
           "from_port": cfg.get("port"), "ts": now_ms(), "kind": kind, "body": body,
           "reply_to": reply_to}

    def queue_offline(reason, conv=None, peer_name=None):
        """对方不可达：聊过/已知目标就入队，否则报错（与 M1 CLI 语义一致）。"""
        cid = conv or (f"dm:{dev['id']}" if dev and dev.get("id") != cfg["device_id"] else "")
        nm = peer_name or ((dev.get("name") or dev["id"][:8]) if dev else "?")
        if cid:
            e = dict(env, conv=cid)
            store.ensure_conv(cid, cid[3:], dev.get("name") if dev else "")
            store.add_message(e, direction="out", state="queued")
            if path:
                _store_attach(mid, Path(str(path)), thumb_b64)
            store.enqueue(mid)
            return {"ok": True, "queued": True, "device": nm,
                    "message": env_row(store, mid), "error": str(reason)}
        return {"ok": False, "error": f"连不上 {ip}:{port} —— {reason}"}

    try:
        info = hello(ip, port, cfg["key"], cfg["device_id"], cfg.get("device_name"),
                     cfg.get("port", DEFAULT_PORT), tls_ctx=tctx)
    except RuntimeError as e:      # 密钥不符（401）
        return {"ok": False, "auth": True, "error": str(e),
                "key": cfg["key"]}
    except Exception as e:
        if isinstance(e, ssl.SSLError) and "certificate verify failed" in str(e):
            return {"ok": False, "tls": True,
                    "error": "对端证书与已固定指纹不符（TLS 握手拒绝）。"
                             "若对方确已更换证书：trust --remove <旧指纹前缀> 后重新 trust 配对"}
        return queue_offline(e)
    if not info:
        return queue_offline("对方无响应")
    peer_id = info["device_id"]
    if peer_id == cfg["device_id"]:
        return {"ok": False, "error": "目标就是本机，换一个地址"}

    conv = f"dm:{peer_id}"
    name = info.get("device") or ""
    store.upsert_device(peer_id, name, ip, port)
    store.ensure_conv(conv, peer_id, name)
    e = dict(env, conv=conv)
    store.add_message(e, direction="out", state="queued")
    if path:
        _store_attach(mid, Path(str(path)), thumb_b64)
    try:
        if path:
            st, obj = upload_file(ip, port, cfg["key"],
                                  dict(env, name=body["name"]),
                                  Path(str(path)),
                                  attach_dir() / (mid + ".thumb"), tls_ctx=tctx)
        else:
            st, obj = post_message(ip, port, cfg["key"],
                                   {k: v for k, v in e.items() if k != "conv"},
                                   tls_ctx=tctx)
    except Exception as ex:
        store.enqueue(mid)
        return {"ok": True, "queued": True, "device": name or peer_id[:8],
                "message": env_row(store, mid), "error": str(ex)}
    if st == 200 and obj.get("ok"):
        state = "delivered" if obj.get("delivered") else "sent"
        store.set_state(mid, state)
        store.dequeue(mid)
        return {"ok": True, "device": name or peer_id[:8], "peer_id": peer_id,
                "ip": ip, "port": port, "state": state,
                "message": env_row(store, mid)}
    store.enqueue(mid)
    return {"ok": True, "queued": True, "device": name or peer_id[:8],
            "message": env_row(store, mid), "error": f"HTTP {st}"}


def _store_attach(mid, src, thumb_b64=None):
    """把待发附件与缩略图拷进本机 attachments，供离线补发重传。"""
    d = attach_dir()
    dst = d / mid
    try:
        if not dst.exists():
            import shutil as _sh
            _sh.copyfile(str(src), str(dst))
    except Exception:
        pass
    if thumb_b64 and not (d / (mid + ".thumb")).exists():
        try:
            tb = base64.b64decode(thumb_b64)
            if 0 < len(tb) <= THUMB_MAX:
                (d / (mid + ".thumb")).write_bytes(tb)
        except Exception:
            pass


def server_mark_read(store, cfg, conv=None):
    """看完即已读：回执给各发件人 + 本地标记（cmd_history 与 GUI 共用）。"""
    tctx = tls_client_ctx(cfg)
    acked = 0
    for m in store.unread_inbound(conv):
        peer = store.get_device(m["sender"])
        if peer and peer.get("ip"):
            send_ack(peer["ip"], int(peer.get("port") or DEFAULT_PORT),
                     cfg["key"], m["id"], "read", tls_ctx=tctx)
            acked += 1
    store.mark_read(conv)
    return {"acked": acked}


# ---------------------------------------------------------------- 客户端


def http_json(ip, port, method, path, key, payload=None, timeout=8, tls_ctx=None):
    """发一个带密钥的 JSON 请求，返回 (status, obj)。失败抛异常。"""
    if tls_ctx is not None:
        conn = http.client.HTTPSConnection(ip, int(port), timeout=timeout, context=tls_ctx)
    else:
        conn = http.client.HTTPConnection(ip, int(port), timeout=timeout)
    body = None
    headers = {"X-Sync-Key": key}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "ignore")
        try:
            obj = json.loads(raw or "{}")
        except Exception:
            obj = {"raw": raw}
        return resp.status, obj
    finally:
        conn.close()


def hello(ip, port, key, device_id, name, my_port=DEFAULT_PORT, tls_ctx=None):
    st, obj = http_json(ip, port, "POST", "/api/v2/hello", key,
                        {"device_id": device_id, "name": name, "port": my_port},
                        tls_ctx=tls_ctx)
    if st == 200 and obj.get("service") == SERVICE:
        return obj
    if st == 401:
        raise RuntimeError("密钥不匹配（401）")
    return None


def post_message(ip, port, key, env, tls_ctx=None):
    st, obj = http_json(ip, port, "POST", "/api/v2/messages", key, env, timeout=15,
                        tls_ctx=tls_ctx)
    return st, obj


def send_ack(ip, port, key, mid, state="read", tls_ctx=None):
    try:
        http_json(ip, port, "POST", "/api/v2/ack", key, {"message_id": mid, "state": state},
                  timeout=5, tls_ctx=tls_ctx)
    except Exception:
        pass


def upload_file(ip, port, key, meta, file_path, thumb_path=None, tls_ctx=None):
    """把本地文件作为消息体上传给对端（M4：文件/图片）。

    meta 含 id/kind/ts/from/from_name/from_port/name；缩略图（若有）走 X-Thumb 头。
    返回 (status, obj)。失败抛异常。
    """
    conn_cls = http.client.HTTPSConnection if tls_ctx is not None else http.client.HTTPConnection
    if tls_ctx is not None:
        conn = conn_cls(ip, int(port), timeout=60, context=tls_ctx)
    else:
        conn = conn_cls(ip, int(port), timeout=60)
    size = os.path.getsize(str(file_path))
    headers = {
        "X-Sync-Key": key,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
        "X-Msg-Id": meta["id"],
        "X-Kind": meta["kind"],
        "X-Ts": str(meta["ts"]),
        "X-From": meta["from"],
        "X-From-Name": urllib.parse.quote(str(meta.get("from_name") or "")),
        "X-From-Port": str(meta.get("from_port") or DEFAULT_PORT),
        "X-Name": urllib.parse.quote(meta["name"]),
        "X-Size": str(size),
    }
    if thumb_path and os.path.exists(str(thumb_path)):
        headers["X-Thumb"] = base64.urlsafe_b64encode(
            Path(str(thumb_path)).read_bytes()).decode("ascii")
    try:
        with open(str(file_path), "rb") as f:
            conn.putrequest("POST", "/api/v2/upload", skip_host=True, skip_accept_encoding=True)
            headers["Host"] = f"{ip}:{port}"
            for k, v in headers.items():
                conn.putheader(k, v)
            conn.endheaders()
            left = size
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                conn.send(chunk)
                left -= len(chunk)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "ignore")
        try:
            obj = json.loads(raw or "{}")
        except Exception:
            obj = {"raw": raw}
        return resp.status, obj
    finally:
        conn.close()


# ---------------------------------------------------------------- 服务端


class Handler(BaseHTTPRequestHandler):
    server_version = f"LanTelegram/{VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, fmt, *args):
        try:
            with open(paths()["log"], "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {self.address_string()} {fmt % args}\n")
        except Exception:
            pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if code >= 400:
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _blob(self, code, data, ctype, fname=""):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if fname:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(fname))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _authed(self, query):
        key = self.headers.get("X-Sync-Key") or query.get("key", [""])[0]
        return bool(key) and hmac.compare_digest(key.encode(), self.server.sync_key.encode())

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    # ---- TLS 对端证书绑定
    def peer_cert_pem(self) -> bytes:
        """TLS 连接下取握手时已按固定指纹验证过的对端证书；明文连接返回空。"""
        try:
            der = self.connection.getpeercert(True)
        except Exception:
            return b""
        if not der:
            return b""
        from cryptography import x509 as _x
        from cryptography.hazmat.primitives.serialization import Encoding
        if isinstance(der, (bytes, bytearray)):
            return _x.load_der_x509_certificate(der).public_bytes(Encoding.PEM)
        return ssl.DER_cert_to_PEM_cert(der).encode("utf-8")

    def cn_device(self) -> str:
        """TLS 模式下取对端证书 CN 里内嵌的 device_id；明文返回空。"""
        pem = self.peer_cert_pem()
        return cn_device_from_pem(pem) if pem else ""

    # ---- GET
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        cfg = self.server.cfg

        if route == "/api/v2/ping":
            out = {
                "ok": True, "service": SERVICE, "version": VERSION,
                "device": cfg.get("device_name"), "device_id": cfg.get("device_id"),
                "auth_required": True,
            }
            if cfg.get("tls") and paths()["self_cert"].exists():
                try:
                    out["tls"] = True
                    out["fingerprint"] = cert_fingerprint(
                        paths()["self_cert"].read_bytes())
                except Exception:
                    pass
            return self._json(200, out)

        if route == "/api/v2/cert":
            # 证书是公开信息，必须在密钥校验之前、且**明文阶段即可访问**：
            # 配对顺序是「双方先 trust 固定指纹，再各自 tls on」，否则互相拿不到证书。
            try:
                fp, pem_bytes = ensure_self_cert(cfg)
            except RuntimeError as e:
                return self._json(500, {"ok": False, "error": str(e)})
            return self._json(200, {"ok": True, "service": SERVICE,
                                    "device_id": cfg.get("device_id"),
                                    "fingerprint": fp,
                                    "cert_pem": pem_bytes.decode("utf-8")})

        if not self._authed(query):
            return self._json(401, {"ok": False, "error": "密钥不匹配"})

        if route == "/api/v2/events":
            since = int(query.get("since", ["0"])[0] or 0)
            try:
                timeout = min(float(query.get("timeout", ["25"])[0]), 30.0)
            except Exception:
                timeout = 25.0
            deadline = time.time() + timeout
            while True:
                events = self.server.store.events_since(since)
                if events:
                    return self._json(200, {"ok": True, "events": events,
                                            "cursor": events[-1]["seq"]})
                if time.time() >= deadline:
                    return self._json(200, {"ok": True, "events": [], "cursor": since})
                time.sleep(0.25)

        if route == "/api/v2/history":
            conv = query.get("conv", [""])[0]
            limit = int(query.get("limit", ["50"])[0] or 50)
            before = query.get("before", [""])[0]
            items = self.server.store.messages(conv, limit=limit,
                                               before_ts=int(before) if before else None)
            for it in items:
                try:
                    it["body"] = json.loads(it.get("body") or "{}")
                except Exception:
                    it["body"] = {}
            return self._json(200, {"ok": True, "items": items})

        if route == "/api/v2/file":
            # GUI 取缩略图/原件（本机与对端两个入口都可用，过密钥校验）。
            # 只有消息表里认识这条 id 才放行，防任意路径读取。
            mid = (query.get("msg", [""])[0]).strip()
            thumb_q = (query.get("thumb", ["0"])[0]) == "1"
            msg = self.server.store.get_message(mid) if mid else None
            if not msg or msg.get("kind") not in ("file", "image"):
                return self._json(404, {"ok": False, "error": "未知文件消息"})
            base = attach_dir() / mid
            target = Path(str(base) + ".thumb") if thumb_q else base
            if not target.exists():
                return self._json(404, {"ok": False, "error": "附件不存在"})
            try:
                data = target.read_bytes()
            except Exception as e:
                return self._json(500, {"ok": False, "error": f"读取失败 {e}"})
            try:
                body = json.loads(msg.get("body") or "{}")
            except Exception:
                body = {}
            if thumb_q:
                return self._blob(200, data, "image/png")
            ctype = mimetypes.guess_type(body.get("name") or "")[0] or "application/octet-stream"
            return self._blob(200, data, ctype, body.get("name") or mid)

        if route == "/api/v2/conversations":
            out = []
            for c in self.server.store.list_convs():
                item = dict(c)
                try:
                    item["preview"] = (json.loads(c.get("last_body") or "{}").get("text") or "")[:60]
                except Exception:
                    item["preview"] = ""
                item.pop("last_body", None)
                out.append(item)
            return self._json(200, {"ok": True, "conversations": out})

        if route == "/api/v2/contacts":
            return self._json(200, {"ok": True, "contacts": self.server.store.devices_public()})

        return self._json(404, {"ok": False, "error": "not found"})

    # ---- POST
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if getattr(self.server, "local", False) and route in (
                "/api/v2/hello", "/api/v2/messages", "/api/v2/ack", "/api/v2/upload"):
            # 对等协议端点只属于面向局域网的主监听口
            return self._json(404, {"ok": False, "error": "not found"})
        if not self._authed(query):
            return self._json(401, {"ok": False, "error": "密钥不匹配"})
        # /upload 正文是原始字节，自行从 rfile 流式读取；其余 POST 一律 JSON
        obj = None
        if route != "/api/v2/upload":
            obj = self._body()
            if obj is None:
                return self._json(400, {"ok": False, "error": "bad json"})
        store = self.server.store
        cfg = self.server.cfg

        if route == "/api/v2/hello":
            peer_id = (obj.get("device_id") or "").strip()
            if not peer_id:
                return self._json(400, {"ok": False, "error": "缺少 device_id"})
            # TLS 模式：声明的设备 ID 必须与已验证证书的 CN 一致（防伪造 from）
            if self.server.tls:
                bound = self.cn_device()
                if bound and peer_id != bound:
                    return self._json(403, {"ok": False, "error": "证书与声明设备不符"})
            store.upsert_device(peer_id, obj.get("name") or "", self.client_address[0],
                                int(obj.get("port") or DEFAULT_PORT))
            return self._json(200, {
                "ok": True, "service": SERVICE, "version": VERSION,
                "device": cfg.get("device_name"), "device_id": cfg.get("device_id"),
                "server_time": now_ms(),
            })

        if route == "/api/v2/messages":
            mid = (obj.get("id") or "").strip()
            sender = (obj.get("from") or "").strip()
            if not mid or not sender:
                return self._json(400, {"ok": False, "error": "缺少 id / from"})
            if self.server.tls:
                bound = self.cn_device()
                if not bound:
                    return self._json(403, {"ok": False, "error": "TLS 模式缺少已验证对端证书"})
                if sender != bound:
                    return self._json(403, {"ok": False, "error": "证书与声明设备不符"})
            conv = f"dm:{sender}"
            obj["conv"] = conv
            store.upsert_device(sender, obj.get("from_name") or "", self.client_address[0],
                                int(obj.get("from_port") or DEFAULT_PORT))
            store.ensure_conv(conv, sender, obj.get("from_name") or "")
            is_new = store.add_message(obj, direction="in", state="delivered")
            if is_new:
                store.add_event("message", obj)
            return self._json(200, {"ok": True, "id": mid, "duplicate": not is_new,
                                    "delivered": True})

        if route == "/api/v2/upload":
            mid = (self.headers.get("X-Msg-Id") or "").strip()
            sender = (self.headers.get("X-From") or "").strip()
            kind = (self.headers.get("X-Kind") or "file").strip()
            try:
                size = int(self.headers.get("X-Size") or "0")
                ts = int(self.headers.get("X-Ts") or str(now_ms()))
            except ValueError:
                return self._json(400, {"ok": False, "error": "坏头部"})
            if not mid or not sender or len(mid) > 64 or kind not in ("file", "image") or size <= 0:
                return self._json(400, {"ok": False, "error": "缺少/非法上传头部"})
            if size > UPLOAD_MAX:
                return self._json(413, {"ok": False, "error": f"文件超过 {UPLOAD_MAX // 1048576} MB 上限"})
            if self.server.tls:
                bound = self.cn_device()
                if not bound:
                    return self._json(403, {"ok": False, "error": "TLS 模式缺少已验证对端证书"})
                if sender != bound:
                    return self._json(403, {"ok": False, "error": "证书与声明设备不符"})
            name = urllib.parse.unquote(self.headers.get("X-Name") or "file.bin")
            fname = urllib.parse.unquote(self.headers.get("X-From-Name") or "")
            if self.server.store.get_message(mid):
                # 重复投递也要读完正文，保持 keep-alive 连接干净
                remaining = int(self.headers.get("Content-Length") or "0")
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                return self._json(200, {"ok": True, "id": mid, "duplicate": True,
                                        "delivered": True})
            # 流式落盘（不整读进内存）：先写 .part 再原子改名
            d = attach_dir()
            part = d / (mid + ".part")
            try:
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0 or length > UPLOAD_MAX:
                    return self._json(411, {"ok": False, "error": "需要准确 Content-Length（≤上限）"})
                remaining, wrote = length, 0
                with open(part, "wb") as f:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                        wrote += len(chunk)
                if remaining or wrote != size:
                    part.unlink(missing_ok=True)
                    return self._json(400, {"ok": False, "error": "正文不完整"})
                part.replace(d / mid)
                thumb = self.headers.get("X-Thumb")
                if thumb:
                    try:
                        tb = base64.urlsafe_b64decode(thumb.encode("ascii", "ignore"))
                        if 0 < len(tb) <= THUMB_MAX:
                            (d / (mid + ".thumb")).write_bytes(tb)
                    except Exception:
                        pass
            except Exception as e:
                part.unlink(missing_ok=True)
                return self._json(500, {"ok": False, "error": f"落盘失败 {e}"})
            conv = f"dm:{sender}"
            env = {"id": mid, "conv": conv, "from": sender, "from_name": fname,
                   "ts": ts, "kind": kind, "body": {"name": name, "size": size},
                   "reply_to": None}
            store.upsert_device(sender, fname, self.client_address[0],
                                int(self.headers.get("X-From-Port") or DEFAULT_PORT))
            store.ensure_conv(conv, sender, fname)
            is_new = store.add_message(env, direction="in", state="delivered")
            if is_new:
                store.add_event("message", env)
            return self._json(200, {"ok": True, "id": mid, "duplicate": not is_new,
                                    "delivered": True})

        if route == "/api/v2/ack":
            mid = (obj.get("message_id") or "").strip()
            state = (obj.get("state") or "read").strip()
            if not mid:
                return self._json(400, {"ok": False, "error": "缺少 message_id"})
            store.set_state(mid, state)
            store.add_receipt(mid, (obj.get("from") or ""), state)
            store.add_event("receipt", {"message_id": mid, "state": state})
            return self._json(200, {"ok": True})

        if route == "/api/v2/send":
            ref = (obj.get("to") or "").strip()
            text = (obj.get("text") or "").strip() or None
            path = (obj.get("path") or "").strip() or None
            if not ref or (not text and not path):
                return self._json(400, {"ok": False, "error": "缺少 to 与 text/path"})
            res = server_send(store, cfg, ref, text, obj.get("reply_to"),
                              path, obj.get("thumb_b64"))
            return self._json(200 if res.get("ok") else 400, res)

        if route == "/api/v2/read":
            res = server_mark_read(store, cfg, (obj.get("conv") or "").strip() or None)
            return self._json(200, {"ok": True, **res})

        return self._json(404, {"ok": False, "error": "not found"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    tls = False
    local = False   # 本机 GUI/网页接入口：只受理界面端点，拒绝对等协议

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


# ---------------------------------------------------------------- 投递与重试


def deliver_now(store, cfg, mid, ip, port):
    """立即投递一条待发消息，成功返回 True。附件消息走 upload 重传。"""
    msg = store.get_message(mid)
    if not msg:
        return True
    kind = msg["kind"]
    if kind in ("file", "image"):
        d = attach_dir()
        src = d / mid
        if not src.exists():
            store.set_state(mid, "failed")
            store.dequeue(mid)
            store.add_event("state", {"message_id": mid, "state": "failed",
                                      "reason": "本机附件已不存在"})
            return True
        try:
            body = json.loads(msg["body"] or "{}")
        except Exception:
            body = {}
        env = {"id": mid, "from": cfg["device_id"], "from_name": cfg.get("device_name"),
               "from_port": cfg.get("port"), "ts": msg["ts"], "kind": kind,
               "name": body.get("name") or mid}
        try:
            st, obj = upload_file(ip, port, cfg["key"], env, src,
                                  d / (mid + ".thumb"), tls_ctx=tls_client_ctx(cfg))
        except Exception:
            return False
        if st == 200 and obj.get("ok"):
            store.set_state(mid, "delivered" if obj.get("delivered") else "sent")
            store.dequeue(mid)
            return True
        return False
    env = {
        "id": msg["id"], "from": cfg["device_id"], "from_name": cfg.get("device_name"),
        "from_port": cfg.get("port"), "ts": msg["ts"], "kind": msg["kind"],
        "body": json.loads(msg["body"] or "{}"), "reply_to": msg["reply_to"] or None,
    }
    try:
        st, obj = post_message(ip, port, cfg["key"], env,
                               tls_ctx=tls_client_ctx(cfg))
    except Exception:
        return False
    if st == 200 and obj.get("ok"):
        store.set_state(mid, "delivered" if obj.get("delivered") else "sent")
        store.dequeue(mid)
        return True
    return False


def outbox_worker(store, cfg, stop_event):
    """后台补发：对方离线时进队列，恢复后自动补。"""
    while not stop_event.is_set():
        try:
            for row in store.due_outbox():
                mid = row["message_id"]
                msg = store.get_message(mid)
                if not msg:
                    store.dequeue(mid)
                    continue
                conv = msg["conv"] or ""
                peer_id = conv[3:] if conv.startswith("dm:") else ""
                dev = store.get_device(peer_id) if peer_id else None
                if not dev or not dev.get("ip"):
                    store.bump_outbox(mid, row["attempts"] + 1)
                    continue
                if deliver_now(store, cfg, mid, dev["ip"], int(dev.get("port") or DEFAULT_PORT)):
                    print(f"[补发成功] {mid[:8]}")
                else:
                    store.bump_outbox(mid, row["attempts"] + 1)
        except Exception:
            pass
        stop_event.wait(5.0)


# ---------------------------------------------------------------- 命令


def cmd_info(args):
    cfg = load_config()
    st = read_state()
    store = Store(paths()["db"])
    convs = store.list_convs()
    running = bool(st.get("pid") and is_running(st["pid"]))
    print("=== lan-telegram 本机信息 ===")
    print(f"  设备名  : {cfg['device_name']}")
    print(f"  设备 ID : {cfg['device_id']}")
    print(f"  端口    : {cfg['port']}")
    print(f"  密钥    : {cfg['key']}")
    if cfg.get("tls") and paths()["self_cert"].exists():
        try:
            print(f"  传输    : TLS 1.3 · 指纹 {fp_groups(cert_fingerprint(paths()['self_cert'].read_bytes()))}"
                  f" · 已固定对端 {len(load_pins())} 个")
        except Exception:
            print("  传输    : TLS 1.3（证书读取失败）")
    else:
        print("  传输    : 明文 HTTP（tls on 可切换）")
    print(f"  服务    : {'运行中 PID ' + str(st.get('pid')) if running else '未运行'}")
    print(f"  会话数  : {len(convs)}")
    print(f"  数据目录: {paths()['root']}")


def cmd_serve(args):
    cfg = load_config()
    if args.port:
        cfg["port"] = int(args.port)
        save_config(cfg)
    port = int(cfg.get("port", DEFAULT_PORT))

    prev = read_state()
    if prev.get("pid") and prev["pid"] != os.getpid() and is_running(prev["pid"]):
        die(f"服务已在运行（PID {prev['pid']}，端口 {prev.get('port')}）")

    store = Store(paths()["db"])

    tls = bool(cfg.get("tls"))
    tls_ctx = None
    if tls:
        try:
            tls_ctx = build_tls_context(cfg, server=True)
        except RuntimeError as e:
            die(f"启用 TLS 失败：{e}")

    for _ in range(20):
        try:
            srv = Server(("0.0.0.0", port), Handler)
            break
        except OSError:
            port += 1
    else:
        die("找不到可用端口")

    if tls_ctx is not None:
        try:
            srv.socket = tls_ctx.wrap_socket(srv.socket, server_side=True)
        except Exception as e:
            die(f"TLS 包裹监听套接字失败：{e}")

    srv.sync_key = cfg["key"]
    srv.store = store
    srv.cfg = cfg
    srv.tls = tls
    if port != cfg.get("port"):
        cfg["port"] = port
        save_config(cfg)

    # 本机 GUI/网页接入口：固定明文回环 127.0.0.1（默认主端口+1）。
    # TLS 模式下主端口只认双向固定证书，桌面壳与浏览器一律走这个口；
    # 对等协议端点（hello/messages/ack）在此口上不可用，局域网侧不受影响。
    gui_port = port + 1
    for _ in range(6):
        try:
            gui = Server(("127.0.0.1", gui_port), Handler)
            break
        except OSError:
            gui_port += 1
    else:
        die("找不到可用的本机接入口（主端口+1 起被占用）")
    gui.sync_key, gui.store, gui.cfg = cfg["key"], store, cfg
    gui.tls, gui.local = False, True
    threading.Thread(target=gui.serve_forever, daemon=True).start()

    stop_event = threading.Event()
    threading.Thread(target=outbox_worker, args=(store, cfg, stop_event), daemon=True).start()
    write_state({"pid": os.getpid(), "port": port, "local_port": gui_port,
                 "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    print(f"[lan-telegram] 已启动  {cfg['device_name']}  (设备 {cfg['device_id'][:8]})")
    print(f"  端口: {port}   本机接口: 127.0.0.1:{gui_port}   密钥: {cfg['key']}")
    if tls:
        fp, _ = ensure_self_cert(cfg)
        print(f"  TLS:  1.3 强制 · 本机指纹 {fp_groups(fp)} · 已固定对端 {len(load_pins())} 个")
    print(f"  数据库: {paths()['db']}")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        write_state({})


def is_running(pid) -> bool:
    if not pid:
        return False
    try:
        pid = int(pid)
    except Exception:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return ctypes.get_last_error() == 5
            try:
                code = wintypes.DWORD()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == 259
            finally:
                k32.CloseHandle(h)
        except Exception:
            pass
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def start_background():
    cfg = load_config()
    st = read_state()
    if st.get("pid") and is_running(st["pid"]):
        return {"already": True, "port": st.get("port"), "local_port": st.get("local_port"),
                "pid": st["pid"]}
    if getattr(sys, "frozen", False):
        exe = sys.executable
        sib = Path(exe).with_name("lantelegram.exe")
        if sib.exists():
            exe = str(sib)
        cmd = [exe, "serve"]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "serve"]
    log = paths()["log"]
    paths()["root"].mkdir(parents=True, exist_ok=True)
    flags = 0x00000008 | 0x00000200 | CREATE_NO_WINDOW if os.name == "nt" else 0
    env = dict(os.environ)
    env.pop("_PYI_APPLICATION_HOME_DIR", None)
    env.pop("_PYI_PARENT_PROCESS_LEVEL", None)
    with open(log, "a", encoding="utf-8") as logf:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                                creationflags=flags, close_fds=True, env=env)
    for _ in range(40):
        time.sleep(0.15)
        st = read_state()
        if st.get("pid") and is_running(st["pid"]):
            return {"already": False, "port": st.get("port"), "local_port": st.get("local_port"),
                    "pid": st["pid"], "key": cfg["key"], "device_id": cfg["device_id"]}
    return {"already": False, "port": None, "pid": proc.pid, "warning": "启动超时，见日志",
            "log": str(log)}


def stop_service(quiet=False):
    st = read_state()
    pid = st.get("pid")
    if not pid or not is_running(pid):
        write_state({})
        if not quiet:
            print("[lan-telegram] 服务未在运行")
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                           creationflags=CREATE_NO_WINDOW, timeout=15)
        else:
            os.kill(int(pid), 15)
        if not quiet:
            print(f"[lan-telegram] 已停止（PID {pid}）")
    except Exception as e:
        if not quiet:
            print(f"[错误] 停止失败: {e}", file=sys.stderr)
    write_state({})
    return True


def parse_target(text):
    text = (text or "").strip()
    if not text:
        die("请提供 --target <IP[:端口]>")
    if ":" in text:
        host, _, port = text.rpartition(":")
        return host, int(port)
    return text, int(load_config().get("port", DEFAULT_PORT))


def cmd_send(args):
    cfg = load_config()
    text = (args.text or "").strip() or None
    path = (args.file or "").strip() or None
    if not text and not path:
        die("请提供 --text 或 --file")
    store = Store(paths()["db"])
    res = server_send(store, cfg, args.target, text, reply_to=None, path=path)
    if res.get("ok"):
        if res.get("queued"):
            print(f"对方暂不可达（{res.get('error', '')}），消息已入队；"
                  f"设备 {res.get('device', '?')} 上线后自动补发")
            return
        m = res["message"]
        print(f"已发送给 {res['device']} ({res['ip']}:{res['port']}) · {fmt_ts(m['ts'])}")
        return
    if res.get("auth"):
        die(f"{res['error']}\n  本机密钥: {res['key']}\n  让对方执行: pair --key {res['key']}")
    die(res.get("error") or "发送失败")


def cmd_history(args):
    store = Store(paths()["db"])
    cfg = load_config()
    if args.with_peer:
        dev = store.find_device(args.with_peer)
        if not dev:
            die(f"没有与 {args.with_peer} 的会话记录，先发一条消息试试")
        conv = f"dm:{dev['id']}"
        title = dev.get("name") or dev["id"][:8]
    else:
        convs = store.list_convs()
        if not convs:
            print("还没有任何会话。用 send --target <IP> --text ... 发第一条吧。")
            return
        print("=== 会话列表 ===")
        for c in convs:
            unread = c.get("unread") or 0
            mark = f"[{unread} 未读] " if unread else ""
            preview = ""
            try:
                b = json.loads(c.get("last_body") or "{}")
                preview = b.get("text") or (f"[附件] {b.get('name', '')}"[:28] if b.get("name") else "")
            except Exception:
                pass
            print(f"  {c['dev_name'] or c['peer_device'][:8]:<16} {mark}{preview}")
        print("\n用 history --with <设备名或IP> 查看具体对话")
        return

    items = store.messages(conv, limit=args.limit,
                           before_ts=args.before if args.before else None)
    if not items:
        print("这段对话还没有消息。")
        return
    print(f"=== 与 {title} 的对话（最近 {len(items)} 条）===")
    for m in items:
        who = "我" if m["direction"] == "out" else (store.get_device(m["sender"]) or {}).get("name") or "对方"
        try:
            body = json.loads(m["body"] or "{}")
        except Exception:
            body = {}
        if m["kind"] in ("file", "image"):
            kb = body.get("size", 0) / 1024
            size = f"{kb:.0f} KB" if kb < 1024 else f"{kb / 1024:.1f} MB"
            text = f"[{'图片' if m['kind'] == 'image' else '文件'} {body.get('name', '?')} · {size}]"
        else:
            text = body.get("text") or ""
        state = {"queued": "待发", "sent": "已发", "delivered": "已送达",
                 "read": "已读", "failed": "失败"}.get(m["state"], m["state"])
        print(f"[{fmt_ts(m['ts'])}] {who}: {text}" + (f"   ({state})" if m["direction"] == "out" else ""))

    # 看完即已读：本地标记 + 回执给对端（与 GUI 共用同一路径）
    server_mark_read(store, cfg, conv)


def cmd_contacts(args):
    store = Store(paths()["db"])
    with store.lock:
        rows = store.conn.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
    if not rows:
        print("还没有联系过任何设备。")
        return
    print("=== 设备 ===")
    for r in rows:
        seen = fmt_ts(r["last_seen"]) if r["last_seen"] else "?"
        print(f"  {r['name'] or r['id'][:8]:<16} {r['ip'] or '-':<16}:{r['port'] or '-':<6} 最近 {seen}")


def cmd_key(args):
    cfg = load_config()
    if args.new:
        cfg["key"] = secrets.token_hex(8)
        save_config(cfg)
        print(f"新密钥: {cfg['key']}")
        st = read_state()
        if st.get("pid") and is_running(st["pid"]):
            stop_service(quiet=True)
            time.sleep(0.5)
            start_background()
            print("服务已重启，新密钥生效")
        print(f"让对方执行: pair --key {cfg['key']}")
        return
    print(cfg["key"])


def cmd_pair(args):
    if not args.key:
        die("请提供密钥：pair --key <字符串>")
    cfg = load_config()
    cfg["key"] = args.key.strip()
    save_config(cfg)
    print(f"密钥已更新: {cfg['key']}")
    st = read_state()
    if st.get("pid") and is_running(st["pid"]):
        stop_service(quiet=True)
        time.sleep(0.5)
        start_background()
        print("服务已重启")
    print("请在另一台机器上执行同样的命令")


def cmd_getfile(args):
    """把本机存过的附件（收到的或发出的）导出到指定路径。"""
    import shutil
    mid = (args.msg or "").strip()
    msg = Store(paths()["db"]).get_message(mid)
    if not msg or msg["kind"] not in ("file", "image"):
        die("未知的附件消息（用 history 查看消息 id）")
    src = attach_dir() / mid
    if not src.exists():
        die("本机没有这份附件（原件未送达或已删除）")
    out = Path(str(args.out)).expanduser()
    if out.is_dir():
        try:
            name = json.loads(msg["body"] or "{}").get("name") or mid
        except Exception:
            name = mid
        out = out / name
    try:
        shutil.copyfile(str(src), str(out))
        print(f"已保存: {out}（{src.stat().st_size} 字节）")
    except Exception as e:
        die(f"保存失败: {e}")


def cmd_status(args):
    st = read_state()
    cfg = load_config()
    print(json.dumps({
        "running": bool(st.get("pid") and is_running(st["pid"])),
        "pid": st.get("pid"),
        "port": st.get("port") or cfg.get("port"),
        "device": cfg.get("device_name"),
        "device_id": cfg.get("device_id"),
        "tls": bool(cfg.get("tls")),
        "pinned_peers": len(load_pins()),
    }, ensure_ascii=False, indent=2))


def cmd_cert(args):
    cfg = load_config()
    try:
        fp, _ = ensure_self_cert(cfg)
    except RuntimeError as e:
        die(str(e))
    print("=== 本机 TLS 证书 ===")
    print(f"  指纹（给对方核对）: {fp_groups(fp)}")
    print(f"  完整指纹          : {fp}")
    print(f"  证书 CN           : {SERVICE}/{cfg['device_id']}")
    print(f"  证书文件          : {paths()['self_cert']}")
    print(f"  当前传输          : {'TLS 1.3' if cfg.get('tls') else '明文 HTTP'}")
    print(f"  已固定对端        : {len(load_pins())} 个")
    print("\n对方核对上面指纹后执行：")
    ip_hint = ""
    try:
        ip_hint = socket.gethostbyname(socket.gethostname())
    except Exception:
        pass
    print(f"  trust --target {ip_hint or '<本机IP>'}:{cfg.get('port')} "
          f"--expect {fp_groups(fp)}")


def cmd_trust(args):
    cfg = load_config()
    if args.list_pins:
        pins = load_pins()
        if not pins:
            print("还没有固定任何对端证书。")
            return
        print("=== 已固定的对端 ===")
        for fp, meta in sorted(pins.items(), key=lambda kv: kv[1].get("added", 0)):
            print(f"  {fp_groups(fp)}  设备 {(meta.get('device') or '?')[:8]}"
                  f"  来源 {meta.get('source') or '-'}  固定于 {fmt_ts(meta.get('added', 0))}")
        return
    if args.remove:
        if remove_pin(args.remove):
            print(f"已移除固定：{args.remove}")
        else:
            die(f"没有找到指纹以 {args.remove} 开头的固定记录")
        return
    if not args.expect:
        die("trust 必须带 --expect <对方告知的指纹>：人工核对是防中间人的唯一环节")
    want = normalize_expect(args.expect)
    if len(want) < 16:
        die("--expect 至少给出指纹前 4 组（16 个十六进制字符）")

    if args.target:
        ip, port = parse_target(args.target)
        try:
            st, obj = http_json(ip, port, "GET", "/api/v2/cert", cfg["key"],
                                tls_ctx=tls_client_ctx(cfg))
        except Exception as e:
            die(f"拉取对端证书失败：{e}\n  请确认对方已 serve；若对方 TLS 已开而本机未开，"
                f"改用 trust --pem <对方证书文件>")
        if st != 200 or not obj.get("cert_pem"):
            die(f"对端 /api/v2/cert 返回异常（HTTP {st}）")
        pem = obj["cert_pem"].encode("utf-8")
        source = args.target
    elif args.pem:
        try:
            pem = Path(args.pem).read_bytes()
        except Exception as e:
            die(f"读取证书文件失败：{e}")
        source = args.pem
    else:
        die("trust 需要 --target <IP[:端口]> 或 --pem <证书文件> 之一")

    try:
        fp = cert_fingerprint(pem)
    except Exception:
        die("不是有效的 PEM 证书")
    if not fp.startswith(want):
        die(f"指纹核对不符，已拒绝固定（存在中间人或拿错证书的风险）：\n"
            f"  实际: {fp_groups(fp)}\n  期望: {args.expect}")
    add_pin(fp, pem, source=source)
    dev = cn_device_from_pem(pem)
    print(f"已固定对端证书 {fp_groups(fp)}（设备 {dev[:8] or '?'}）。")
    print("双方都固定后各自执行 tls on，通信即切换到 TLS 1.3。")


def cmd_tls(args):
    cfg = load_config()
    mode = (args.mode or "status").strip().lower()
    if mode == "status":
        pinned = len(load_pins())
        state = "TLS 1.3" if cfg.get("tls") else "明文 HTTP"
        print(f"当前传输: {state}   已固定对端: {pinned} 个")
        if cfg.get("tls") and not pinned:
            print("[警告] TLS 已开但没有任何固定证书，serve 会拒绝启动。")
        return
    if mode == "on":
        try:
            ensure_self_cert(cfg)
        except RuntimeError as e:
            die(str(e))
        if not pinned_cadata():
            die("启用 TLS 前至少要固定一个对端证书：\n"
                "  trust --target <对方IP:端口> --expect <对方 cert 显示的指纹>")
        cfg["tls"] = True
        save_config(cfg)
        print("TLS 1.3 已启用。")
    else:
        cfg["tls"] = False
        save_config(cfg)
        print("TLS 已关闭，回到明文 HTTP（与 M1 行为一致）。")
    st = read_state()
    if st.get("pid") and is_running(st["pid"]):
        stop_service(quiet=True)
        time.sleep(0.5)
        r = start_background()
        print(f"服务已重启（PID {r.get('pid')}，端口 {r.get('port')}，"
              f"模式 {'TLS 1.3' if cfg.get('tls') else '明文'}）")
    else:
        print("下次 serve 时生效。")


# ---------------------------------------------------------------- CLI


def build_parser():
    ap = argparse.ArgumentParser(prog="lantelegram", description="局域网聊天（lan-telegram）")
    sub = ap.add_subparsers(dest="cmd")

    sp = sub.add_parser("serve", help="启动接收服务")
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--background", action="store_true")
    sp.set_defaults(func=lambda a: (print(json.dumps(start_background(), ensure_ascii=False))
                                    if a.background else cmd_serve(a)))

    sub.add_parser("stop", help="停止后台服务").set_defaults(func=lambda a: stop_service())
    sub.add_parser("status", help="服务状态").set_defaults(func=cmd_status)
    sub.add_parser("info", help="本机信息").set_defaults(func=cmd_info)
    sub.add_parser("contacts", help="已联系过的设备").set_defaults(func=cmd_contacts)

    sp = sub.add_parser("send", help="发送消息")
    sp.add_argument("--target", required=True, help="对方 IP[:端口]")
    sp.add_argument("--text", help="文本内容")
    sp.add_argument("--file", help="发送本地文件/图片路径")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("history", help="查看会话与历史")
    sp.add_argument("--with", dest="with_peer", default="", help="设备名 / IP")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--before", type=int, default=0, help="按时间戳翻页")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("key", help="查看或更换密钥")
    sp.add_argument("--new", action="store_true")
    sp.set_defaults(func=cmd_key)

    sp = sub.add_parser("pair", help="设置密钥")
    sp.add_argument("--key")
    sp.set_defaults(func=cmd_pair)

    sub.add_parser("cert", help="查看/生成本机 TLS 证书与指纹").set_defaults(func=cmd_cert)

    sp = sub.add_parser("trust", help="人工核对并固定对端证书指纹")
    sp.add_argument("--target", default="", help="对方 IP[:端口]，从其 /api/v2/cert 拉证书")
    sp.add_argument("--pem", default="", help="离线导入对方证书 PEM 文件")
    sp.add_argument("--expect", default="", help="对方口头告知的指纹（至少前 4 组）")
    sp.add_argument("--list", action="store_true", dest="list_pins", help="列出已固定对端")
    sp.add_argument("--remove", default="", help="按指纹前缀移除固定")
    sp.set_defaults(func=cmd_trust)

    sp = sub.add_parser("tls", help="启用/关闭 TLS 1.3（缺省查看状态）")
    sp.add_argument("mode", nargs="?", default="status", choices=["on", "off", "status"])
    sp.set_defaults(func=cmd_tls)

    sp = sub.add_parser("getfile", help="导出本机存过的附件（收到或发出的）")
    sp.add_argument("--msg", required=True, help="消息 id（history 可见）")
    sp.add_argument("--out", required=True, help="保存路径（目录或文件）")
    sp.set_defaults(func=cmd_getfile)

    return ap


def main():
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout
    ap = build_parser()
    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
