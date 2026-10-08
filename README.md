# 灵枢 × Hermes Agent 记忆插件

把 [灵枢（dsh-memory）](https://github.com/FuRongJun-1999/dsh-memory) 接进 **Hermes Agent** 的
`MemoryProvider` 接口，让 Hermes 拥有**跨会话的自动记忆**。

> 灵枢是一个白箱（white-box）长期记忆系统：对话沉淀成人类可读的 **md 认知图**，
> 检索靠确定性规则而非黑箱向量，全程可审计。

---

## 为什么需要这个插件

灵枢本身是一个标准 **MCP server**（`md_cg.mcp_server`），任何支持 MCP 的 Agent 都能挂载它。

但 **MCP 只提供"工具"**，拿不到 Hermes 记忆系统的**核心循环**：

| 能力 | 纯 MCP 挂载 | **本插件** |
|---|---|---|
| `prefetch()` 每轮前自动召回 | ❌ | ✅ |
| `sync_turn()` 每轮后自动写入 | ❌ | ✅ |
| `on_session_end()` 会话边界落盘 | ❌ | ✅ |
| `on_pre_compress()` 压缩前抽取 | ❌ | ✅ |
| `on_memory_write()` 镜像内置记忆写入 | ❌ | ✅ |
| `system_prompt_block()` 静态注入 | ❌ | ✅ |

**纯 MCP = 给模型多了一把工具（要主动调，容易漏）；本插件 = 真正的记忆循环（自动）。**

---

## 快速开始

### 1. 部署灵枢本体

```bash
git clone https://github.com/FuRongJun-1999/dsh-memory.git
# 记下路径，例如 /opt/dsh-memory
```

### 2. 安装本插件

```bash
# 放进 Hermes 的插件目录
cp -r dsh-memory-hermes-provider ~/.hermes/plugins/dsh-memory
```

### 3. 签发写入凭据

灵枢默认是**只读访客模式**，写入需要 token：

```bash
cd /opt/dsh-memory
MDCG_ROOT=~/.hermes/.dsh-memory/data/mdcg \
  python3 -m md_cg.tokens issue --role recorder --actor hermes --label hermes
```

把输出的 token（形如 `mdcg1.record.xxx`）写进 `~/.hermes/.env`：

```bash
echo 'MDCG_TOKEN=mdcg1.record.xxxxx' >> ~/.hermes/.env
```

### 4. 配置 Hermes

```bash
hermes config set memory.provider dsh-memory
hermes config set memory.dsh_memory.python python3
hermes config set memory.dsh_memory.root /opt/dsh-memory
hermes config set memory.dsh_memory.actor hermes
# 可选：自定义记忆库位置（默认 $HERMES_HOME/.dsh-memory/data/mdcg）
hermes config set memory.dsh_memory.mdcg_root /path/to/store
```

### 5. 重启

```bash
hermes gateway restart      # 如果跑网关
# 或 hermes chat
```

### 6. 验证

```bash
hermes memory status
```

---

## 配置项

| 键 | 说明 | 默认 |
|---|---|---|
| `python` | 运行 MCP server 的 Python 解释器 | `python` |
| `root` | 灵枢仓库路径（含 `md_cg/`） | 空（必填） |
| `mdcg_root` | 记忆库根目录（`MDCG_ROOT`） | `$HERMES_HOME/.dsh-memory/data/mdcg` |
| `actor` | 审计台账里的写入者标识 | `hermes` |

**token 有两种给法**（二选一）：
- `~/.hermes/.env` 里的 `MDCG_TOKEN=...`（推荐）
- `config.yaml` → `memory.dsh_memory.env.MDCG_TOKEN`

---

## 提供的工具

插件向模型暴露 4 个工具：

| 工具 | 作用 |
|---|---|
| `mdcg_recall` | 按查询召回相关记忆（带相关性分数与出处） |
| `mdcg_remember` | 持久化一条事实/偏好/决定（`gated=true` 会先过验证闸门） |
| `mdcg_verify` | 白箱验证某条候选记忆 |
| `mdcg_search` | 结构化搜索（按桶/相关度） |

---

## 设计要点

- **非阻塞写入**：`sync_turn()` 只入队，由独立 writer 线程异步落盘
- **召回缓存**：`prefetch()` 带 30 秒缓存 + LRU（128 条），避免每轮都打 MCP
- **Profile 隔离**：默认记忆库落在 `$HERMES_HOME/.dsh-memory/`，同机多 profile 不串库
- **零第三方依赖**：只用 Python 标准库
- **进程常驻**：MCP server 作为子进程常驻，`shutdown()` 时终止

---

## 已知限制

- Hermes **同一时间只能有一个 memory provider**（启用本插件会替换掉原来的）
- 灵枢是单维护者项目，官方 README 自承存在**多会话边界问题**；本插件把所有写入
  都按 session 作用域处理，以降低串库风险
- `prefetch()` 在**缓存未命中**时会同步等 MCP 返回（约 1–3 秒），有缓存时接近 0
- MCP 子进程的**长期稳定性**取决于上游；建议配合健康检查脚本观察

---

## ⚠️ 密级（clearance）机制：务必先读

灵枢对每条记忆打 **sensitivity 标签**，读取时必须 `token.clearance ≥ 节点密级`：

```
public < internal < private < secret
```

**关键结论（实测验证）：**

| 角色 | 密级上限 | can_admin |
|---|---|---|
| `designer` | **secret** | ✅ True |
| 其余全部（record / reflect / verify / output / sustain / orchestr / guest） | **internal** | ❌ False |

**也就是说：灵枢把「能读 private/secret」和「管理员权限」绑死在 `designer` 一个角色上**，
**不存在**「能读私密但不能管理」的中间态 —— 派生子令牌也绕不过（只能收窄，
换任何非 designer 角色都会被夹回 internal）。

### 这对插件意味着什么

1. **默认只读得到 `internal` 及以下**的节点
2. 如果你用 `designer` 之类的角色**导入过 private 数据**（会话内容默认就是 private），
   **插件用 internal token 时一条都召回不到** —— 而且**不报错**，静默过滤
3. 想让插件读到那些数据 → 插件 token 也必须是 `designer`（**等于给它全库管理权**）
4. 如果插件不需要管理权 → 更安全的做法是**导入时降密级为 internal**

> 建议：给插件用的 token 单独签发、单独记账；并在 `.env` 里保留 `MDCG_TOKEN` 的同时，
> 配置一份记忆库的定期备份（回滚用）。

---

## 导入历史对话

灵枢内置 `mdcg_ingest`（`op=ingest, action=jsonl`）可批量导入会话事件，
但**有三个坑**：

1. **水位线（watermark）**：`$MDCG_ROOT/_sources.json` 记录每个来源的最大时间戳 `t`；
   **时间戳早于水位线的事件全部跳过**。导入历史数据（时间戳很旧）会得到 `new_events: 0`。
   → 解决：删/清 `_sources.json`，或给事件重新赋**递增的、晚于当前水位**的时间戳。
2. **密级**：会话内容默认 `private`，写入方 token 必须 `clearance ≥ private`（即 designer），
   否则每条都 `denied`，错误是 `写入敏感度 private 超出 clearance internal`。
3. **格式**：必须是 DSH 会话事件结构：

```jsonl
{"type":"session","id":"xxx","cwd":"/root"}
{"type":"user/message","time":1700000000000,"seq":1,"data":{"content":[{"type":"text","text":"..."}]}}
{"type":"assistant/message","time":1700000001000,"seq":2,"data":{"message":{"content":[{"type":"text","text":"..."}]}}}
```

调用：

```python
cg(op="ingest", action="jsonl", path="/path/to/session.jsonl")
# 预演（统计但不写入）：
cg(op="ingest", action="jsonl", path="...", dry_run=True)
```

**导入前建议筛选**：会话记录里通常混有大量工具调用日志（`role=tool`、
带 `tool_calls` 的 assistant），这些进库只会污染召回。用纯规则过滤掉即可（零 LLM 成本）。

---

## 故障排查

| 现象 | 处理 |
|---|---|
| `hermes memory setup` 里看不到 | 确认 `plugin.yaml` 与 `__init__.py` 同目录；跑 `hermes plugins doctor ~/.hermes/plugins/dsh-memory` |
| `dsh-memory MCP server failed to start` | 确认 `python -m md_cg.mcp_server` 在 `root` 下能跑（`PYTHONPATH` 指向 `root`） |
| 工具返回 `AccessDenied ... 角色 guest` | 没配 `MDCG_TOKEN`，见上面的"签发写入凭据" |
| 工具返回 `Unknown tool: mdcg_*` | 需要 `MDCG_MCP_SURFACE=full`（插件已默认设置） |
| 召回为空 | 记忆库还没内容；先 `mdcg_remember` 几条 |
| **MCP 启动即退出、报"令牌格式非法"** | token 必须是**4 段**：`mdcg1.<role>.<tk_id>.<secret>`；用 `md_cg.tokens issue` 输出的**完整明文**，别截断 |
| **记录写不进去、`denied: N`** | 密级问题，见"密级机制"一节 |
| **导入历史得到 `new_events: 0`** | 水位线问题，见"导入历史对话"一节 |
| `prefetch` 变慢 | 检查 MCP 子进程是否堆积（`pgrep -f md_cg.mcp_server`）；必要时重启 gateway |
| 启动报错看不到原因 | 插件自 v0.1.5 起捕获子进程 stderr，失败时写入 `logs/errors.log`（含「令牌格式非法」「写入策略路径」等原始提示）|

---

## 许可

MIT（与上游 dsh-memory、Hermes Agent 一致）。
