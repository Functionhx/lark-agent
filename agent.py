#!/usr/bin/env python3
"""Feishu auto-reply agent: polls whitelisted chats via lark-cli (as the owner) and replies with DeepSeek."""
import json, os, subprocess, sys, time, logging, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
STATE_PATH = os.path.join(BASE, "state.json")
PAUSE_FLAG = os.path.join(BASE, "PAUSE")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("agent")


def load_env():
    path = os.path.join(BASE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_state():
    try:
        return json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception:
        return {"chats": {}}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    json.dump(state, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def lark(*args):
    out = subprocess.run(["lark-cli", *args], capture_output=True, text=True, timeout=60)
    text = out.stdout
    if "{" not in text:
        raise RuntimeError(f"lark-cli no json: {out.stderr[-300:]}")
    d = json.loads(text[text.index("{"):])
    if not d.get("ok"):
        raise RuntimeError(f"lark-cli error: {json.dumps(d.get('error'), ensure_ascii=False)[:300]}")
    return d["data"]


def fetch_messages(chat_id, n):
    data = lark("im", "+chat-messages-list", "--as", "user", "--chat-id", chat_id,
                "--order", "desc", "--page-size", str(n), "--no-reactions", "--format", "json")
    return list(reversed(data.get("messages") or []))  # oldest -> newest


def send_text(chat_id, text):
    data = lark("im", "+messages-send", "--as", "user", "--chat-id", chat_id, "--text", text)
    return data.get("message_id")


def as_text(m):
    t = m.get("msg_type")
    c = m.get("content") or ""
    if t in ("text", "post"):
        return c
    return {"image": "[图片]", "sticker": "[表情包]", "file": "[文件]", "audio": "[语音]",
            "media": "[视频]"}.get(t, f"[{t}]")


def deepseek(messages):
    body = json.dumps({"model": CFG["model"], "messages": messages,
                       "temperature": CFG.get("temperature", 1.0), "max_tokens": 400}).encode()
    req = urllib.request.Request(CFG["api_base"].rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def build_prompt(chat_cfg, history):
    system = open(os.path.join(BASE, "persona.md"), encoding="utf-8").read()
    system = system.replace("{PEER_NAME}", chat_cfg["name"]).replace("{OWNER_NAME}", CFG["owner_name"])
    msgs = [{"role": "system", "content": system}]
    for m in history:
        role = "assistant" if m["sender"]["id"] == CFG["owner_open_id"] else "user"
        text = as_text(m)
        if msgs[-1]["role"] == role and role != "system":
            msgs[-1]["content"] += "\n" + text
        else:
            msgs.append({"role": role, "content": text})
    return msgs


def handle_chat(chat_cfg, st, now):
    cid = chat_cfg["chat_id"]
    peer = chat_cfg["peer_open_id"]
    history = fetch_messages(cid, CFG.get("history", 20))
    seen = set(st.setdefault("seen", []))
    sent = set(st.setdefault("sent", []))

    if not st.get("initialized"):  # baseline: don't reply to backlog, except her very last unanswered message
        st["seen"] = [m["message_id"] for m in history]
        st["initialized"] = True
        last = history[-1] if history else None
        if last and last["sender"]["id"] == peer:
            try:
                age = now - time.mktime(time.strptime(last["create_time"], "%Y-%m-%d %H:%M"))
            except Exception:
                age = 1e9
            if age <= 600:
                st["pending_since"] = now - CFG.get("debounce_seconds", 8)
        return

    new = [m for m in history if m["message_id"] not in seen]
    for m in new:
        st["seen"].append(m["message_id"])
        sid = m["sender"]["id"]
        if sid == CFG["owner_open_id"] and m["message_id"] not in sent:
            st["paused_until"] = now + CFG["takeover_minutes"] * 60
            st.pop("pending_since", None)
            log.info("[%s] owner replied manually -> pause %d min", chat_cfg["name"], CFG["takeover_minutes"])
        elif sid == peer:
            st["pending_since"] = now
    st["seen"] = st["seen"][-200:]

    if not st.get("pending_since") or now < st.get("paused_until", 0):
        return
    if now - st["pending_since"] < CFG.get("debounce_seconds", 8):  # wait for her to finish typing
        return
    hour = int(now // 3600)
    if st.get("rate_hour") != hour:
        st["rate_hour"], st["rate_count"] = hour, 0
    if st["rate_count"] >= CFG.get("max_replies_per_hour", 30):
        log.warning("[%s] hourly reply cap reached", chat_cfg["name"])
        return

    reply = deepseek(build_prompt(chat_cfg, history))
    if not reply:
        return
    mid = send_text(cid, reply)
    st["sent"] = (st["sent"] + [mid])[-200:]
    st["rate_count"] += 1
    day = time.strftime("%Y-%m-%d")
    if st.get("day") != day:
        st["day"], st["day_count"] = day, 0
    st["day_count"] += 1
    st.pop("pending_since", None)
    log.info("[%s] replied: %s", chat_cfg["name"], reply.replace("\n", " ")[:120])


HELP = ("🎛️ 自动回复控制台\n"
        "开 — 立即开启自动回复\n"
        "关 — 立即关闭\n"
        "暂停 N — 暂停 N 分钟后自动恢复（如：暂停 60）\n"
        "状态 — 查看当前状态\n"
        "帮助 — 显示本说明")


def is_enabled(state, now):
    if os.path.exists(PAUSE_FLAG):
        return False
    until = state.get("resume_at")
    if until and now >= until:  # timed pause expired
        state["enabled"], state["resume_at"] = True, None
        for st in state["chats"].values():
            st["initialized"] = False
    return state.get("enabled", True)


def status_text(state, now):
    on = is_enabled(state, now)
    lines = [f"当前：{'🟢 自动回复中' if on else '🔴 已关闭'}"]
    if state.get("resume_at") and not on:
        lines.append("将于 " + time.strftime("%H:%M", time.localtime(state["resume_at"])) + " 自动恢复")
    if os.path.exists(PAUSE_FLAG):
        lines.append("（服务器上有 PAUSE 文件，删掉后才会恢复）")
    for c in CFG["chats"]:
        st = state["chats"].get(c["chat_id"], {})
        taken = st.get("paused_until", 0) > now
        lines.append(f"· {c['name']}：今天已回 {st.get('day_count', 0)} 条" + ("，你正在亲自聊（暂让位）" if taken else ""))
    return "\n".join(lines)


def handle_control(state, now):
    cid = CFG.get("control_chat_id")
    if not cid:
        return
    ctl = state.setdefault("control", {})
    msgs = fetch_messages(cid, 10)
    if not ctl.get("initialized"):
        ctl["seen"], ctl["sent"], ctl["initialized"] = [m["message_id"] for m in msgs], [], True
        return
    for m in msgs:
        mid = m["message_id"]
        if mid in ctl["seen"]:
            continue
        ctl["seen"] = (ctl["seen"] + [mid])[-100:]
        if mid in ctl["sent"] or m["sender"]["id"] != CFG["owner_open_id"] or m.get("msg_type") != "text":
            continue
        cmd = (m.get("content") or "").strip().lower()
        if cmd in ("开", "开启", "on", "start"):
            if not is_enabled(state, now):
                for st in state["chats"].values():  # re-baseline so it doesn't answer the whole backlog
                    st["initialized"] = False
            state["enabled"], state["resume_at"] = True, None
            reply = "✅ 已开启自动回复"
        elif cmd in ("关", "关闭", "off", "stop"):
            state["enabled"], state["resume_at"] = False, None
            reply = "⛔ 已关闭自动回复，你自己来聊吧"
        elif cmd.startswith("暂停") or cmd.startswith("pause"):
            digits = "".join(ch for ch in cmd if ch.isdigit())
            minutes = int(digits) if digits else 30
            state["enabled"], state["resume_at"] = False, now + minutes * 60
            reply = f"⏸️ 暂停 {minutes} 分钟，" + time.strftime("%H:%M", time.localtime(state["resume_at"])) + " 自动恢复"
        elif cmd in ("状态", "status", "?", "？"):
            reply = status_text(state, now)
        elif cmd in ("帮助", "help"):
            reply = HELP
        else:
            continue  # ignore free-form notes in the control chat
        sent_id = send_text(cid, reply)
        ctl["sent"] = (ctl["sent"] + [sent_id])[-100:]
        log.info("control: %s -> %s", cmd, reply.splitlines()[0])


def main():
    load_env()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        log.error("DEEPSEEK_API_KEY missing in .env")
        sys.exit(1)
    state = load_state()
    log.info("agent started, chats=%s", [c["name"] for c in CFG["chats"]])
    backoff, last_poll = 0, 0
    while True:
        now = time.time()
        try:
            handle_control(state, now)
            if is_enabled(state, now) and now - last_poll >= CFG["poll_seconds"]:
                last_poll = now
                for c in CFG["chats"]:
                    handle_chat(c, state["chats"].setdefault(c["chat_id"], {}), now)
            save_state(state)
            backoff = 0
        except Exception as e:
            backoff = min(backoff * 2 or 10, 300)
            log.error("loop error: %s (retry in %ds)", e, backoff)
            save_state(state)
            time.sleep(backoff)
        time.sleep(CFG.get("control_poll_seconds", 3))


if __name__ == "__main__":
    main()
