# code-search

> 让你的 AI agent 通过飞书「代码查询」群，远程查询公司全量代码镜像——跨仓库追上下游、定位接口实现、分析改动影响面，回答精确到 **仓库 / 文件 / 行号**。

![code-search](assets/code-search-doodle.png)

## 它解决什么问题

本地只 clone 了一两个仓库，但问题横跨整条链路：

- 「`/ops/round/add` 这个接口的 `rValue` 改成数组，会牵连哪些仓库哪些文件？」
- 「前端这个页面调的是后端哪个接口？」
- 「这段报错日志对应哪段代码？」
- 「`CanViewOdds` 定义在哪？上游谁在调？」

公司全量代码镜像在远端 `/var/lib/codex-review/work/`，飞书「代码查询」群里的 **Lark CLI** 机器人接到 @ 提问后检索镜像，几分钟内回复文件路径 + 行号 + 关键代码的完整分析。

装上本 skill 后，你只要**跟自己的 agent（Claude Code / Cursor 等）说人话**，发问、@ 机器人、等回复、整理答案全部由 agent 自动完成——可用 harness 的后台完成通知继续处理；也支持通过 OCS 通知另一个 agent。

## 安装

### ① 装 skill（一次）

```bash
npx skills add pw-OpenCapsule/code-search -g
```

### ② 前置：lark-cli（没装过才需要）

```bash
npx @larksuite/cli@latest install
lark-cli config init --new      # 输出授权链接，浏览器里点完即可
lark-cli auth login --recommend # 同上
lark-cli auth status            # 验证
```

详细图文指引见 [Lark-cli 安装文档](https://portwind.jp.larksuite.com/wiki/JVLmw2zdXiM455kl1TqjuWW8p3b)。装了 skill 之后这一步也可以直接让 agent 带你走完。

### ③ 加入「代码查询」群（一次）

[点这里加入](https://applink.larksuite.com/client/chat/chatter/add_by_link?link_token=d68s12ba-a2f6-46a2-a4ba-a1042ekmi29k)。不加群发不了问题，agent 检测到你没入群时也会把这个链接发给你。

## 用法 — 跟 agent 说

| 场景 | 你说 |
|------|------|
| 查定义 / 调用点 | 帮我查下 wininfuture-service 里 `CanViewOdds` 定义在哪、谁在调 |
| 查改动影响面 | `/ops/round/add` 的 rValue 入参改成数组，影响面有多大？去代码查询群问下 |
| 前后端对应 | 管端「对局编辑」页面对应后端哪个接口？ |
| 报错定位 | 这段报错对应哪段代码？（贴日志） |
| 指定分支 | 基于 feature/xxx 分支查……（不说分支默认查 dev） |

agent 会自动：定位群 → @Lark CLI 机器人发问（带上分支）→ 后台等回复（3～10 分钟，期间继续干别的活）→ 后台完成通知（需要 harness 支持）→ 校验分支 → 整理成结构化答案给你。

## 实际效果

提问（agent 代发）：

> wininfuture-service 里 CanViewOdds 这个函数定义在哪个文件哪一行？有哪些调用点？ @Lark CLI

约 3 分钟后机器人回复：

> 定义：`internal/permissions/round_permissions.go:98`
> 调用点（origin/dev）共 3 处：
> `internal/service/game_service.go:162` / `game_service.go:435` / `predict_service.go:363`
> （含每处的关键代码片段，并注明所查分支）

## 提问质量直接决定答案质量

机器人没有你的本地上下文，问题必须自包含：

- ✅ 带 接口路径 / 方法名 / 类名 / 报错关键字
- ✅ 带 项目名或业务名 + **分支名**（最容易踩的坑——不带分支默认查 `origin/dev`）
- ✅ 固定加一句「请注明你实际查询的分支」
- ❌ 一次塞多个问题
- ❌ 在群里闲聊（机器人只回代码问题）

## 工作机制

客户端仍通过飞书发问。默认等待模式每 20 秒拉取回复，自动翻页、读取问题的话题回复；按消息 ID 去重，按分片编号拼接。指定问题有话题 ID 后，后续只查询该话题。完整答案才退出，请求错误与等待超时分别返回。

```bash
bash scripts/wait-reply.sh <chat_id> <message_id> 900 20
```

需要 Python 3.9+ 和已登录的 lark-cli，不再需要 jq。原来的四个位置参数保持兼容。延迟启动等待任务时，用 `--start '<提问时间 ISO 8601>'` 保留查询窗口。Windows 可直接运行 `python scripts/wait_reply.py ...`。

### 通过 OCS 通知其他 agent

[open-cross-session](https://github.com/leeguooooo/open-cross-session) 提供跨会话消息与唤醒。等待脚本可以在答案完整后保存 JSON，再通知指定的本机 agent：

```bash
bash scripts/wait-reply.sh <chat_id> <message_id> 900 20 \
  --output '<现有私有目录>/answer-<message_id>.json' \
  --notify '<目标 OCS 短 ID>'
```

通知只包含问题 ID 和结果文件路径，不把公司代码写进 OCS 消息。文件按 0600 权限新建，不覆盖已有结果。OCS 通知只尝试一次，结果未知时不会重复发送。OCS 会抑制自我唤醒：通知其他会话用它，通知发起查询的当前会话仍依赖 harness 的后台完成机制。

### 取消轮询需要服务端推送

本仓库只有客户端 skill，答题服务端不在这里。`--source stdin` 已提供结果流入口，收到完整答案后可以保存并通过 OCS 通知；该模式不调用飞书 API。服务端接入步骤和输入格式见 [docs/push.md](docs/push.md)。目前默认路径仍是轮询，不能把本地 OCS 通知当成服务端推送已上线。

客户端不要用共享应用的 `lark-cli event consume` 监听回复：它会和答题服务端争用同一个应用的事件接收连接。改为推送时，应由服务端现有的唯一接收进程转发结果。

## 验证

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/wait-reply.sh
```

测试使用合成回复和模拟 OCS，不向真实群或 agent 发消息。CI 在 Linux/macOS、Python 3.9/3.13 上运行同样的检查。

## 相关项目

- openapi-lark — OpenAPI 接口文档同步到飞书 wiki（查接口文档用它，查代码用本项目）
- [lark-cli](https://www.npmjs.com/package/@larksuite/cli) — 本 skill 依赖的飞书命令行

## License

MIT
