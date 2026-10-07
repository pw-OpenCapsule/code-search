---
name: code-search
description: 当用户想查公司代码但本地没有对应仓库时使用——"某接口在哪个仓库哪个文件"、"谁调用了 X / X 调了谁"、"前端页面对应哪个后端接口"、"报错日志对应哪段代码"、"这个改动会牵连哪些链路/影响面"、"PR/分支改动范围"、跨仓库上下游追查。通过飞书「代码查询」群 @Lark CLI 机器人远程查询公司代码镜像。
---

# code-search — 飞书「代码查询」群远程代码查询

## 原理

公司全量代码镜像在远端 `/var/lib/codex-review/work/`（多仓库，默认看 `origin/dev`）。飞书「代码查询」群里的 **Lark CLI** 机器人接到 @ 提问后会检索镜像，几分钟内回复**仓库 / 文件路径 / 行号**级别的答案。

本 skill 的工作 = 用本机 lark-cli 把问题发进群并 @ 机器人 → 后台等回复 → 整理答案给用户。

## 前置条件（按顺序检查）

### 1. lark-cli 已安装并登录

```bash
lark-cli auth status
```

正常输出 `identities.user.status == "ready"` 即可。**未安装 / 未登录时按以下四步指引用户**（第 2、3 步在后台运行，从输出中提取授权链接发给用户，用户在浏览器完成后命令自动退出）：

```bash
# ① 安装
npx @larksuite/cli@latest install
# ② 配置应用凭证（后台跑，提取授权链接发给用户）
lark-cli config init --new
# ③ 登录（同上）
lark-cli auth login --recommend
# ④ 验证
lark-cli auth status
```

详细图文指引：https://portwind.jp.larksuite.com/wiki/JVLmw2zdXiM455kl1TqjuWW8p3b

### 2. 用户已加入「代码查询」群

```bash
lark-cli im +chat-search --query "代码查询" --json
```

搜不到群（`chats` 为空）说明用户还没入群，把入群链接发给用户，等用户加入后再继续：

> https://applink.larksuite.com/client/chat/chatter/add_by_link?link_token=d68s12ba-a2f6-46a2-a4ba-a1042ekmi29k

## 查询流程

### Step 1 — 定位群和机器人

```bash
# chat_id
lark-cli im +chat-search --query "代码查询" --json
# 机器人 open_id（bot_name == "Lark CLI" 的 bot_id）
lark-cli im chat.members bots --params '{"chat_id":"<oc_xxx>"}' --as user
```

### Step 2 — 先定分支，再发问（必须 @ 机器人）

**分支是最容易出错的环节**：镜像默认看 `origin/dev`，但用户实际工作分支可能不同，查错分支的答案会误导后续改动。发问前按优先级确定分支：

1. 用户问题涉及的仓库在本地有 → `git branch --show-current` 取当前分支，写进问题
2. 用户在对话里提过分支 / PR → 用它
3. 都没有 → 不要瞎猜，问题里写明「基于 dev 分支」，并在最终答案里向用户标注这个假设

@ 必须用 `<at>` 标签写在 `--text` 里，必须 `--as user`：

```bash
lark-cli im +messages-send --chat-id <oc_xxx> --as user --json \
  --text '<问题> <at user_id="<机器人bot_id>">Lark CLI</at>'
```

记下返回的 `message_id`（om_xxx），等回复要用。

**提问模板**（机器人没有你的本地上下文，问题必须自包含）：

```
<项目名/业务名> <分支名> 分支：<具体问题（接口路径/方法名/类名/报错关键字）>。
请注明你实际查询的分支和 commit。 @Lark CLI
```

- 带上：接口路径 / 方法名 / 类名 / 报错关键字
- 带上：项目名或业务名（如 wininfuture-service）+ **分支名**
- 固定加一句「请注明你实际查询的分支」——这样答案可校验
- 有报错就贴日志文本；一次问一个问题

### Step 3 — 收集回复

先记下提问时间，再立即启动脚本。默认等待 900 秒，每 20 秒查询一次；完整答案输出到 stdout。用 harness 支持的后台任务运行，保留 task/session ID；只有 harness 支持后台完成通知时，任务退出才会主动通知你。

```bash
# <skill-dir> 是本 skill 的安装目录，不是用户项目的当前目录。
bash <skill-dir>/scripts/wait-reply.sh <oc_xxx> <om_xxx> 900 20 \
  --start '<提问时刻的 ISO 8601 时间>' --bot-id '<机器人 sender.id>'
```

需要 Python 3.9+，不需要 jq。Windows 直接运行 `python <skill-dir>/scripts/wait_reply.py`，其余参数相同。`--bot-id` 按返回消息里的 `sender.id` 匹配；若成员列表 ID 与 sender ID 类型不同，不要混用，先确认对应关系。省略时接收回复此问题的 app 消息。

脚本会翻页、读取话题回复、按 message_id 去重，只有 `(1/N)` 至 `(N/N)` 全部集齐才完成。指定问题出现 thread_id 后，后续只查询该话题。请求失败会退避重试；连续三次失败单独报错，不当成“机器人没回复”。无分片标记的非空 bot 回复按单条最终答案处理；服务端的进度通知必须使用独立消息类型或另行约定完成标记。

#### OCS 通知另一个 agent

用户明确指定需要通知的 agent 时，可以追加 `--output` 和 `--notify`。目标必须是显式的本机会话地址，不能猜收件人。OCS 抑制自我唤醒，这个参数用于通知其他会话；通知当前会话仍使用 harness 的后台任务机制。

```bash
bash <skill-dir>/scripts/wait-reply.sh <oc_xxx> <om_xxx> 900 20 \
  --output '<现有私有目录>/answer-<om_xxx>.json' \
  --notify '<目标 OCS 短 ID>'
```

OCS 缺失时按其官方安装方式安装：

```bash
curl -fsSL https://raw.githubusercontent.com/leeguooooo/open-cross-session/main/install.sh | sh
```

结果先保存为新 JSON 文件（包含内容、回复消息和链接，不覆盖已有文件），再发送一次 OCS 通知；通知只带问题 ID 和本地文件路径。脚本用 `code-search-<问题ID哈希>` 作为 worker 发送身份。退出码 5 表示结果已保存、通知未确认，检查回执，不要重发。OCS 返回成功也不代表目标已读。

#### 服务端接入推送后

`--source stdin` 接收逐行 JSON，期间不调用 lark-cli。只能使用答题服务端或其唯一事件接收进程提供的可信结果流，不要在客户端建立共享飞书应用的事件长连接。具体输入格式见 [推送接入说明](docs/push.md)。服务端尚未接入时继续用默认轮询；OCS 只负责通知 agent，不负责接收飞书回复。

退出码：`0` 答案完整；`2` 等待超时（参数错误也由 argparse 返回 2）；`3` 依赖/输出路径错误；`4` API/数据/流错误；`5` 结果已保存但 OCS 通知未确认；`130` 手动中断。先检查退出码，再按成功或失败处理。

### Step 4 — 整理答案

把分片按顺序拼好，提炼成结构化结论（仓库 → 文件:行号 → 结论）回给用户，附上群消息直达链接（回复消息里的 `message_app_link`）。

**必须做分支校验**：检查机器人回复里写的实际查询分支是否和用户工作分支一致；机器人没写分支、或分支不一致时，要在答案里显著标注「⚠️ 该结论基于 <分支> 镜像，可能与你的 <分支> 不一致，行号仅供参考」，必要时追问机器人重查指定分支。

## 注意事项 / 禁忌

- **群只支持代码查询**，不要往群里发无关内容（机器人会拒绝闲聊）。
- **禁止用 `lark-cli event consume` 等事件方式监听回复**：lark-cli 是全员共享的同一个应用（app），飞书事件在同一应用的多个长连接间负载均衡——你建连接会随机"抢走"服务端答题机器人的事件，直接弄坏整个查询服务。客户端使用安全轮询，或由服务端已有接收进程转发的推送流。
- 发消息前把**收件群 + 消息内容**给用户过目（首次使用时）；用户已明确发起查询的，直接发。
- 等待期间不要每隔几秒高频轮询，20 秒一次足够（lark-cli 全员共享一个 app，限流按 app 计，自觉省着用）。
- **多人同时提问是支持的**：回复靠 `reply_to` 串到各自的提问，不会拿错答案；但服务端答题大概率排队，高峰期回复会明显变慢——超时可以从 900 调大到 1800。
- 超时没回复：提醒用户稍后用 `+chat-messages-list` 再查，或去群里看。

## 常见错误

| 现象 | 原因 / 处理 |
|------|------------|
| 机器人不回复 | 没 @ 到——确认 `<at user_id="...">` 用的是 `chat.members bots` 返回的 bot_id |
| 发送报权限错误 | 缺 `im:message.send_as_user` 等 scope → `lark-cli auth login --scope "<missing_scope>"` |
| `+chat-search` 搜不到群 | 用户没入群 → 发入群链接 |
| 回复只看到一半 | 长答案分片 `(1/2)`…，等分片集齐再整理 |
| **查错了分支** | 提问没带分支名，机器人默认查 `origin/dev`——发问前先 `git branch --show-current`，提问模板里固定带分支并要求机器人注明实际查询分支 |
| 把回复当最终事实 | 镜像可能落后于用户本地未推送的提交——结论要标注来源分支，行号差异以本地为准 |
