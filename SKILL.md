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

### Step 3 — 后台等回复（agent 主动被唤醒，不要傻等）

机器人通常 **3～10 分钟**回复，长答案会拆成 `(1/2)` `(2/2)` 分片。**以后台任务方式**运行轮询，进程退出 = 查询结束，你会被自动唤醒，期间可以继续干别的：

```bash
# 仓库自带脚本（skill 安装目录 scripts/wait-reply.sh）；没有脚本时用下面的内联版
./scripts/wait-reply.sh <oc_xxx> <om_xxx> 900 20
```

内联版（无脚本依赖，逻辑相同——轮询群消息，筛 `reply_to == 提问message_id && sender_type == "app"`，分片集齐才算完成）：

```bash
CHAT_ID=<oc_xxx>; MSG_ID=<om_xxx>; deadline=$(( $(date +%s) + 900 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  replies=$(lark-cli im +chat-messages-list --chat-id "$CHAT_ID" --json 2>/dev/null \
    | jq --arg mid "$MSG_ID" '[.data.messages[]? | select(.reply_to == $mid and .sender.sender_type == "app")]')
  status=$(jq -r '
    def part: .content | capture("\\((?<k>\\d+)/(?<n>\\d+)\\)\\s*$") // null;
    if length == 0 then "waiting"
    elif ([.[] | part] | all(. == null)) then "complete"
    else ([.[] | part | select(. != null) | .n | tonumber] | max) as $N
      | if ([.[] | part | select(. != null)] | length) >= $N then "complete" else "partial" end
    end' <<<"$replies")
  [ "$status" = "complete" ] && { jq -r 'sort_by(.message_position|tonumber) | .[].content' <<<"$replies"; break; }
  sleep 20
done
```

### Step 4 — 整理答案

把分片按顺序拼好，提炼成结构化结论（仓库 → 文件:行号 → 结论）回给用户，附上群消息直达链接（回复消息里的 `message_app_link`）。

**必须做分支校验**：检查机器人回复里写的实际查询分支是否和用户工作分支一致；机器人没写分支、或分支不一致时，要在答案里显著标注「⚠️ 该结论基于 <分支> 镜像，可能与你的 <分支> 不一致，行号仅供参考」，必要时追问机器人重查指定分支。

## 注意事项 / 禁忌

- **群只支持代码查询**，不要往群里发无关内容（机器人会拒绝闲聊）。
- **禁止用 `lark-cli event consume` 等事件方式监听回复**：lark-cli 是全员共享的同一个应用（app），飞书事件在同一应用的多个长连接间负载均衡——你建连接会随机"抢走"服务端答题机器人的事件，直接弄坏整个查询服务。只用轮询。
- 发消息前把**收件群 + 消息内容**给用户过目（首次使用时）；用户已明确发起查询的，直接发。
- 等待期间不要每隔几秒高频轮询，20 秒一次足够。
- 超时（15 分钟）没回复：提醒用户稍后用 `+chat-messages-list` 再查，或去群里看。

## 常见错误

| 现象 | 原因 / 处理 |
|------|------------|
| 机器人不回复 | 没 @ 到——确认 `<at user_id="...">` 用的是 `chat.members bots` 返回的 bot_id |
| 发送报权限错误 | 缺 `im:message.send_as_user` 等 scope → `lark-cli auth login --scope "<missing_scope>"` |
| `+chat-search` 搜不到群 | 用户没入群 → 发入群链接 |
| 回复只看到一半 | 长答案分片 `(1/2)`…，等分片集齐再整理 |
| **查错了分支** | 提问没带分支名，机器人默认查 `origin/dev`——发问前先 `git branch --show-current`，提问模板里固定带分支并要求机器人注明实际查询分支 |
| 把回复当最终事实 | 镜像可能落后于用户本地未推送的提交——结论要标注来源分支，行号差异以本地为准 |
