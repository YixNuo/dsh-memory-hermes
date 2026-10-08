# 更新日志

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
