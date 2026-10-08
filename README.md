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

## 故障排查

| 现象 | 处理 |
|---|---|
| `hermes memory setup` 里看不到 | 确认 `plugin.yaml` 与 `__init__.py` 同目录；跑 `hermes plugins doctor ~/.hermes/plugins/dsh-memory` |
| `dsh-memory MCP server failed to start` | 确认 `python -m md_cg.mcp_server` 在 `root` 下能跑（`PYTHONPATH` 指向 `root`） |
| 工具返回 `AccessDenied ... 角色 guest` | 没配 `MDCG_TOKEN`，见上面的"签发写入凭据" |
| 工具返回 `Unknown tool: mdcg_*` | 需要 `MDCG_MCP_SURFACE=full`（插件已默认设置） |
| 召回为空 | 记忆库还没内容；先 `mdcg_remember` 几条 |

---

## 许可

MIT（与上游 dsh-memory、Hermes Agent 一致）。
