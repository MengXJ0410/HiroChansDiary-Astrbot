# 希罗酱の日记 · HiroChansDiary

> 让 AstrBot 里的 AI 角色每天写一篇日记，**全部追加进同一本「日记本」**，可选定时把当天那篇分享给你。

一个 AstrBot 插件。不依赖 `computer_use_runtime`，也不给角色开放任何文件工具——日记由插件自己写，模型只负责生成正文。

```
2026年11月03日

## 我的生活
中午煮了面加两个蛋，洗完碗又整理书架，把旧本子翻出来擦了擦。

## 和他的聊天
他问我喜不喜欢写日记，我说喜欢，一直记着自己做过的善事。他说要给我弄个日记本，
后来又讲会话有上限、人会忘记，希望我把我们之间的事记下来。我听着堵，还是答应了。

真到要写的时候，反而不知道该从哪句落笔 ( ๑ • ̯ • ๑ ) 他说不希望忘记彼此，这句话我大概会记得很久。
```

## 特性

- **一本日记本**：所有条目追加到同一个 `日记本.md`，像真的笔记本一样越写越厚；**没有任何删除逻辑**，超过 500MB 自动开下一卷。
- **三段式结构**：`我的生活`（今天的小事）→ `和他的聊天`（当天聊天总结）→ 空一行的一小段无标题心里话。后两段都可以关掉。
- **会挑素材**：只取目标会话当天的聊天记录，多段对话按时间合并，超长从最早的开始丢。
- **没聊天也能写**：当天没有聊天记录时，聊天段落整段省略（提示词 + 代码双保险），不会出现「今天没聊天」这种废话。
- **三种触发**：每日定时写、定时写+分享、开关式「现在立刻写一篇」，另有凌晨补写漏掉的日期。
- **人格不打架**：把人格提示词原样带上，再追加一段「日记模式」提示词覆盖掉对话格式限制。
- **颜文字可选**：允许 `(≧◡≦)`、`(*/ω＼*)` 这类颜文字，也能一键关掉。

## 安装

**方式一：WebUI 安装（推荐）**

AstrBot → 插件管理 → 从 GitHub 安装，填：

```
https://github.com/MengXJ0410/HiroChansDiary-Astrbot
```

**方式二：手动放目录**

```bash
cd <AstrBot>/data/plugins
git clone https://github.com/MengXJ0410/HiroChansDiary-Astrbot.git astrbot_plugin_diary
# 或直接下载 zip 解压成 astrbot_plugin_diary/
```

重启 AstrBot，插件列表里出现「希罗酱の日记」即安装成功。首次加载会按 `_conf_schema.json` 生成配置文件，在 WebUI → 插件 → 希罗酱の日记 → 配置里改，保存会热重载插件。

> 具体插件目录位置取决于你的部署方式（Docker 一般是 `/AstrBot/data/plugins/`）。

## 必填的两项

| 配置 | 说明 |
| --- | --- |
| `umo` | 日记写进哪个会话的工作区，也是取聊天素材的来源。对机器人发 `/sid` 可查看当前会话的 UMO，形如 `aiocqhttp:FriendMessage:10000000`。 |
| `persona_id` | 用哪个人格来写。填人格名称，留空则只使用内置日记提示词。 |

其余项都有合理默认值，可以先跑起来再慢慢调。

## 产出在哪

```text
<AstrBot>/data/workspaces/<UMO 转义后的名字>/<diary_dir>/<book_file>
例：data/workspaces/aiocqhttp_FriendMessage_10000000/diary/日记本.md
```

条目之间用 `\n---\n\n` 分隔；即时日记的条目头带时间，例如 `2026年11月03日 15:30`，所以同一天可以既有定时的一篇、又有临时的一篇。

## 配置项

### 时间与任务

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `cron_expression` | `30 23 * * *` | 每日写日记时间。开了「定期分享」后此项不再使用。 |
| `timezone` | `Asia/Shanghai` | 必须和容器时区一致，否则日期与触发时间会错。 |
| `share_enabled` | `false` | 开启后改由 `share_cron` 一次性「写好并发送」。 |
| `share_cron` | `30 23 * * *` | 分享时间。 |
| `share_umo` | 空 | 分享到哪个会话。留空＝和 `umo` 相同。 |
| `catchup_days` | `0` | 补写最近 N 天缺失的日记，0 关闭。 |
| `catchup_cron` | `5 0 * * *` | 补写检查时间，仅在 `catchup_days > 0` 时生效。 |
| `instant_trigger` | `false` | 打开并保存＝立刻写一篇（带时间戳）并发出去。 |

### 日记本

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `diary_dir` | `diary` | 相对会话工作区根目录。 |
| `book_file` | `日记本.md` | 日记本文件名，分卷时变成 `日记本-2.md`、`日记本-3.md`…… |
| `book_max_mb` | `500` | 单卷大小上限。 |

### 写作口径

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `user_alias` | `他` | 日记里怎么称呼你。不会出现「主人」「您」。 |
| `chat_section_title` | `和他的聊天` | 聊天段落的小标题。 |
| `thought_enabled` | `true` | 结尾写一小段不加标题的心里话。 |
| `kaomoji_enabled` | `true` | 允许颜文字（每篇 1–2 个），关掉则明确禁止颜文字和 emoji。 |
| `extra_instruction` | 空 | 追加到系统提示词末尾，用来微调语气和篇幅。 |
| `persona_id` | `希罗` | 人格名称。 |
| `provider_id` | 空 | 指定模型提供方，例如 `sqk/smart`；留空用当前会话默认。 |

### 发送

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `notify` | `true` | 写完立刻发到目标会话。开了「定期分享」后失效，避免一天发两遍。 |
| `notify_prefix` | `这是今天的「希罗酱の日记」，想给你看一下` | 发消息前缀，留空直接发正文。 |
| `max_context_chars` | `8000` | 聊天记录字符上限，超了从最早的丢。 |
| `max_message_chars` | `1000` | 单条消息上限，超了按段落拆成多条发。 |

## 三个定时任务

| 任务 | 默认时间 | 出现条件 | 作用 |
| --- | --- | --- | --- |
| `diary_daily` | 每天 23:30 | `share_enabled = false` | 写当天日记，`notify` 开着就顺手发。 |
| `diary_share` | 每天 23:30 | `share_enabled = true` | **写好并发送**，接管写入。 |
| `diary_catchup` | 每天 00:05 | `catchup_days > 0` | 补写最近 N 天缺失的日记。 |

- 分享时如果当天条目已存在，就把那一篇重发一次；不存在才生成。**只发当天这一篇，不会把整本日记本发出去。**
- `initialize()` 会先删同名任务再重新注册，所以在 WebUI 里反复重载插件不会堆出一串重复任务。
- 任务用 `persistent=False` 注册：handler 只存在内存里，持久化反而会在重启后找不到 handler 而报错。

## 「现在立刻写一则日记」

`_conf_schema.json` 没有按钮类型，所以用「开关 + 保存即触发」模拟按钮：

1. 打开 `instant_trigger` 并保存，插件立刻以当前时刻为止写一篇；
2. 条目头带时间（`2026年11月03日 15:30`），追加进同一本日记本；
3. 发到目标会话。

开关状态记在插件数据目录的 `state.json` 里，只有「关 → 开」才触发一次，反复重载不会重复写。写完记得关掉，下次才能再触发。

## 手动指令

仅管理员可用：

```text
/diary              # 今天：没有就写，已有就重发
/diary 2026-11-02   # 指定日期
```

## 依赖的 AstrBot 内部 API

| 用途 | API |
| --- | --- |
| 定时任务 | `context.cron_manager.add_basic_job(..., persistent=False)` |
| 会话历史 | `context.conversation_manager.get_conversations()` / `get_curr_conversation_id()` |
| 人格 | `context.persona_manager.get_persona_v3_by_id()` |
| 模型 | `context.get_using_provider(umo=...)` → `text_chat()` |
| 主动发消息 | `context.send_message(umo, MessageChain(...))` |
| 工作区 | `get_astrbot_workspaces_path()` / `normalize_umo_for_workspace()` |

插件版本对应 AstrBot `>=4.0.0`，实测于 4.29。内部 API 变了就得跟着改。

## 常见问题

**为什么提示「没有可用的模型提供方」？**
AstrBot 启动时插件的 `initialize()` 早于 provider 加载，此时取不到模型。插件会轮询等待最多 120 秒，仍取不到才报错——通常是那个人格/会话还没配默认模型，或者 `provider_id` 填错了。

**为什么日记时间不对？**
先看容器时区：`docker exec <容器> date`。`timezone` 必须和它一致。

**为什么一天写了两篇？**
只有一种情况：既有定时的（条目头不带时间），又有即时日记/`/diary` 手动写的（条目头带时间）。两者设计上互不覆盖。

**为什么发送失败 `retcode 1200 无法获取用户信息`？**
目标 UMO 不对，或者机器人拿不到那个用户的资料。用 `/sid` 核对 `umo`。

**能改成不发消息、只写文件吗？**
把 `notify` 和 `share_enabled` 都关掉，只留 `cron_expression`。

**日记会丢吗？**
不会。代码里没有任何删除或覆盖日记本的逻辑，只有追加和分卷。

## 已知限制

- 聊天素材来自 `conversations.history`，日期藏在用户消息的 `<system_reminder>` 里；AstrBot 改了历史格式就得同步改 `_extract_day()`。
- 「今天是否已写」只扫日记本尾部 64KB；补写很久以前的日期可能扫不到，理论上会重复追加。
- 上下文压缩（`llm_compress`）会把很旧的对话压成摘要，补写很久以前的日期可能拿不到原始对话。
- 补写每次都是真实的模型调用；跨卷去重不生效（`_book_path()` 只指向当前卷）。
- 分卷后旧卷不参与去重和读取，只作为归档。

## License

[MIT](LICENSE)
