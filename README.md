# lark-agent

用**你本人的飞书身份**自动回复指定好友的私聊：服务器上常驻运行，轮询白名单私聊 → 大模型（默认 DeepSeek）生成回复 → 通过 [飞书 CLI](https://github.com/larksuite/cli) 以你的名义发出。并在飞书里提供一个「控制台」群，发一个字即可开关。

```
朋友发消息 ──▶ 服务器每 10 秒轮询 ──▶ 等对方停顿 8 秒 ──▶ DeepSeek 生成回复 ──▶ lark-cli 以你本人身份发出
你在控制台群发「开 / 关 / 暂停 N / 状态」──▶ 3 秒内生效
```

## 功能

- **只回白名单**：`config.json` 里列出的私聊，其他会话一律不碰
- **本人插话自动让位**：你亲自在该会话发消息，Agent 暂停 30 分钟
- **防抖**：对方连发多条时，等停顿后一次性回复
- **限流**：每个会话每小时最多 30 条，防刷屏、控成本
- **人设与护栏**（`persona.md`）：被问是否 AI 时如实说明；不替本人做承诺；不泄露隐私；不代表本人表达感情
- **控制台群**：开 / 关 / 暂停 N / 状态 / 帮助；重新开启时不回复积压消息（只接最后一条 10 分钟内未回的）

## 两种回复引擎

| `backend` | 说明 |
|---|---|
| `api`（默认） | 直接调 OpenAI 兼容接口（默认 DeepSeek），需要 `.env` 里的 `DEEPSEEK_API_KEY` |
| `claude_cli` | 调用 Claude Code 的 `claude -p` 生成回复；Claude Code 背后接什么模型由你自己配置 |

`claude_cli` 模式下以 `--tools ""` 关闭全部工具（只生成文字，不能读写文件或执行命令），并用 `--system-prompt` 传入 `persona.md`、`--no-session-persistence` 不留会话、`--strict-mcp-config` 不加载 MCP，在空目录 `claude-sandbox/` 中运行。可在 `config.json` 的 `claude_cli` 里设置 `bin`、`model`、`timeout`、`extra_args`。

运行中可在控制台群发「模式 api」或「模式 claude」即时切换，无需重启。

## 为什么用轮询而不是事件订阅

飞书的消息事件只推送给**机器人**，不会推送「你本人收到的消息」。要以本人身份收发，只能用 `user_access_token` 轮询，本项目用 lark-cli 完成。

## 快速开始

前置：一台 Linux 服务器（Python 3.8+，Node 18+），一个飞书自建应用（`lark-cli config init --new` 创建），DeepSeek API Key。

```bash
# 1. 安装飞书 CLI
sudo npm install -g @larksuite/cli

# 2. 配置应用（App Secret 从 stdin 读入，不进命令历史）
lark-cli config init --app-id <你的 App ID> --app-secret-stdin

# 3. 以本人身份登录，只申请消息相关权限；打开输出的链接授权
lark-cli auth login --domain im --scope "im:message.send_as_user"

# 4. 获取代码与配置
git clone <本仓库> ~/lark-agent && cd ~/lark-agent
cp config.example.json config.json   # 填 owner_open_id、chat_id 等
read -rsp "DeepSeek API Key: " k && echo "DEEPSEEK_API_KEY=$k" > .env && chmod 600 .env

# 5. 注册为 systemd 服务（开机自启、崩溃重启）
sudo cp lark-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lark-agent
journalctl -u lark-agent -f
```

`lark-agent.service` 默认用户为 `ubuntu`、目录为 `/home/ubuntu/lark-agent`，不同请自行修改。

## 获取各种 ID

| 字段 | 命令 |
|---|---|
| `owner_open_id` | `lark-cli contact +get-user` |
| 对方 `peer_open_id` | `lark-cli contact +search-user --query 名字` |
| 私聊 `chat_id` | 先给对方发一条：`lark-cli im +messages-send --as user --user-id <ou_xxx> --text 你好`，返回值里有 `chat_id` |
| `control_chat_id` | `lark-cli im +chat-create --as user --name "自动回复控制台"` |

## 运维

| 操作 | 命令 |
|---|---|
| 看日志 | `journalctl -u lark-agent -f` |
| 切换回复引擎 | 控制台群发「模式 api」/「模式 claude」 |
| 临时停用 | 控制台群发「关」，或 `touch ~/lark-agent/PAUSE` |
| 改人设 / 白名单 | 编辑 `persona.md` / `config.json` 后 `sudo systemctl restart lark-agent` |
| 登录过期 | `lark-cli auth status` 查看，过期后重新 `lark-cli auth login` |

## 注意

- 飞书开放接口禁止跨组织私聊（错误码 230038），对方需与你在同一组织。
- 中国大陆服务器无法访问 Anthropic Claude API（返回 403），所以默认使用 DeepSeek；接口兼容 OpenAI 格式，改 `api_base` 和 `model` 即可换其他模型。
- 以本人身份自动回复请提前告知对方，并保留 `persona.md` 中的诚实与不承诺规则。
