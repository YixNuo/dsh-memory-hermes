# 更新日志

## v0.1.3

**修复**：

1. **`on_session_end()` 无长度限制** — 会把整段会话（含上下文压缩摘要）
   原样拼成一条 `session note` 写入库。一次压缩摘要曾产出 **1.5MB** 的单个节点，
   导致：
   - 该节点在**每次召回中都排第一**（包含全部历史关键词）
   - 单轮注入的 token 预算被它吃掉一半以上
   - 真正的记忆被挤出召回窗口
   → 增加两道截断护栏：单条消息 4000 字符、单次 flush 20000 字符，
   超出部分截断并标注 `…[已截断，原文 N 字符]`。

> `sync_turn()` 路径原本已有 `[:4000]` 截断，本版把 `on_session_end()` 补齐一致。

**说明**：

- 本插件只把**对话文本**（user / assistant 的 content）写入记忆，
  **不含工具调用参数与工具输出** —— 所以记忆库不会被工具日志污染。
- 已存在的超大节点需要手动清理（本插件不做删除操作）。

---

## v0.1.2

**修复**（这些 bug 会让插件在真实环境下**根本跑不起来**）：

1. **死锁**：`start()` 在持有 `self._lock` 的情况下调用 `stop()`，
   而 `stop()` 也要获取同一把锁 → 永久卡死。
   → 改为在释放路径上直接终止子进程，不再重入加锁。

2. **`_next_id` 未定义**：`_id()` 引用类属性 `_McpStdioClient._next_id`，
   但该类属性从未定义（只在 `__init__` 里设了实例属性）→ `AttributeError`，
   握手直接失败。
   → 增加类级计数器。

3. **读取线程每次调用都新建**：`_read_until()` 每次调用都启动一个新的
   stdout 读取线程，前一个线程仍在抢同一根管道 → 后续调用读不到行。
   → 改为**单个常驻 reader 线程** + 队列，所有调用从同一队列取。

4. **工具名不存在**：会话落盘调用了 `mdcg_session`，但上游没有该工具
   → 会话边界写入全部失败。
   → 改为 `cg(op="session", action="note", ...)`。

5. **`MDCG_MCP_SURFACE=full` 未设置**：默认 MCP surface 只暴露 `cg`/`stg`
   两个工具，插件调用的 `mdcg_*` 一律 "Unknown tool"。
   → `initialize()` 中默认注入。

6. **`MDCG_TOKEN` 未透传**：未配 token 时上游以只读 guest 身份运行，
   所有写入 `AccessDenied`。
   → 从 config 或环境变量取值后注入 MCP 子进程环境。

**新增**：

- README 增加「密级机制」「导入历史对话」两节，并补充排错条目
  （token 4 段格式 / denied / 水位线 / 进程堆积）。

---

## v0.1.0

- 首个可运行版本：`MemoryProvider` 接口完整实现
  （prefetch / sync_turn / on_session_end / on_pre_memory_write / system_prompt_block）。
