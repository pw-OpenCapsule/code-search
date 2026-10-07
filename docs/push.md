# 服务端推送接入

要取消每个客户端的飞书轮询，需要答题服务端在答案产生时主动推送。OCS 提供 agent 唤醒能力，飞书回复仍由服务端现有的唯一事件接收进程处理；客户端不创建共享应用的事件长连接。

## 客户端接口

```bash
<已接入的可信结果流命令> | python3 scripts/wait_reply.py <chat_id> <message_id> 900 \
  --source stdin --output '<现有私有目录>/answer-<message_id>.json' \
  --notify '<另一会话的 OCS 短 ID>'
```

尖括号里的结果流命令需要服务端提供，本仓库没有可直接连接公司服务的命令。`--notify` 可省略。OCS 只能通知显式指定的其他本机会话，不能用来唤醒发起脚本的同一会话。

每行是独立 JSON 对象，格式采用 lark-cli 已转换的消息格式，可以是 `{"messages":[...]}` 或 `{"data":{"messages":[...]}}`：

```json
{"messages":[{"message_id":"om_reply_1","reply_to":"om_question","sender":{"sender_type":"app","id":"bot_id"},"content":"第一部分 (1/2)"}]}
{"messages":[{"message_id":"om_reply_2","reply_to":"om_question","sender":{"sender_type":"app","id":"bot_id"},"content":"第二部分 (2/2)"}]}
```

等待脚本的 message_id 参数在这个例子里必须是 `om_question`。每条回复必须有稳定且唯一的 message_id、关联到问题的 reply_to、sender 和文本 content。可保留 create_time、message_app_link、分支和 commit 等信息，它们会一起保存到结果 JSON。原始飞书事件需先转换，不能直接混入结果流。

输入必须来自已验证的服务端连接；本脚本只做消息关联和分片收集，不认证 stdin 的来源。不会执行回复里的命令。chat_id 在 stdin 模式中仅保留命令兼容性，服务端必须校验用户有权读取该群和该问题。

## 服务端需完成的工作

1. 问题发送成功后，以 message_id 登记结果订阅。订阅权限绑定发问用户，不接受任意群消息读取。
2. 答案持久化后，按该 message_id 将结果发往已有的客户端连接（SSE/WebSocket 或其他可靠通道），客户端适配器转换为上述逐行 JSON。
3. 重连时补发已保存的结果；message_id 不能变化，分片总数必须一致。客户端按回复 ID 去重，并验证 1..N 分片齐全。
4. 非分片答案只发最终正文。不能把“已接收”“查询中”等进度文本放进 messages：当前协议把无分片标记的非空 app 回复视为最终答案。
5. 保留飞书群内回复作为可回查来源。推送连接失败时，可用原问题 ID 启动轮询补查，不重复发送问题。

接入完成的验收包括：客户端等待期间零飞书拉取请求；断线重连补发无漏片；同时提问不串答案；答案落盘后 OCS 投递回执与目标实际收到分别验证。当前仓库测试只覆盖客户端接口，服务端推送和真实 OCS 投递尚未验收。
