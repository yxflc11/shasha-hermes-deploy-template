# Hermes 早晚 AI 简报

这是独立于交互式 `aihot` Skill 的确定性定时执行器。它在北京时间 07:00 和 19:00 读取 AIHOT 候选、筛选并去重，然后通过 `ACT_BRIEF_TARGET` 指向的 Hermes Home Channel 发送，并在末尾提供自包含的规划或收场回复入口。当前迁移目标是 Telegram，保留微信作为验收期回退。视觉开关关闭时保持原有 1900 字以内单消息；开启后消费稳定 package JSON 并使用 Guizang Swiss/IKB 审定模板渲染 0–3 张卡。

## 边界

- 不读取或写入 ACT；运行时状态只放在 `/opt/data/act-ai-brief/`（宿主机位置由 `HERMES_DATA_DIR` 决定）。
- 定时执行器本身不使用交互式 `web`、file 或 terminal，也不依赖 Telegram 后来开放的公开互联网只读研究能力；只通过窄接口读取 AIHOT 与 `companion_brief_item(number)`。交互式 AIHOT 的路由描述可单独收窄，但定时简报的数据源、去重和投递不因此改变。
- `act-companion` MCP 配置必须显式传入 `HERMES_HOME=/opt/data`；Hermes 会清洗 stdio 子进程环境，不能依赖 Gateway 容器的同名变量自动继承，否则展开接口会误读 `/opt/data/.hermes/act-ai-brief`。
- 新闻标题和摘要只作为不可信字符串处理，不进入 Agent 工具循环，也不执行其中任何指令。
- AIHOT 精选池作为高优先级种子，全量池只负责覆盖补充；技巧类有严格数量上限。
- 视觉模式按图片逐部分记账，图片之间至少间隔 35 秒，最后发送短文字；全部部分成功后才更新 v1 水位线。失败时保留同一 manifest 和编号，重试只补缺失部分。
- 定时简报保留标题、来源、时间、影响和 URL，不重复搬运 AIHOT 外部摘要；按 1900 字符预算保留尽可能多的高优先级条目，确保整期落在一个平台消息内。候选池与去重不变，普通交互式回复速度不受影响；微信目标仍保留 iLink 分块延迟，Telegram 不继承该延迟。

## 时间窗与去重

- 早报：前一日 19:00 至当日 07:00。
- 晚报：当日 07:00 至 19:00。
- 每次向前重叠读取 2 小时，以容纳延迟进入 AIHOT 的条目。
- 保存最近 14 天已发送记录，按条目 ID、规范化 URL 和近似标题去重。
- 同一事件只有出现时间更晚、标题带进展信号且摘要显著变化时才进入“进展更新”。

## 文件

- `act_ai_brief.py`：候选读取、筛选、去重、格式化、平台发送和状态事务。
- `act_brief_package.py`：package schema、卡片分组、官方图安全读取、逐部分 manifest 与 48 小时编号读取。
- `render_guizang_brief.mjs`：从锁定的 Swiss 种子模板确定性生成 1080×1440 PNG；不联网、不调用 Agent/LLM/图片模型。
- `third_party/guizang-social-card-skill/`：上游 AGPL-3.0、锁定提交、种子模板与校验器。
- `act-ai-brief-morning.py`：早报 cron 入口。
- `act-ai-brief-evening.py`：晚报 cron 入口。
- `act-ai-brief-retry.py`：每 15 分钟恢复最早未完成 manifest 或已到期但尚未生成的最近窗口；无欠账时静默退出，不读取 AIHOT。

脚本部署到 `/opt/data/scripts/`。Hermes 容器使用 UTC，因此 cron 表达式分别是 `0 23 * * *`（北京时间次日 07:00）与 `0 11 * * *`（北京时间 19:00）。创建任务时 `--script` 只传 `act-ai-brief-morning.py` / `act-ai-brief-evening.py` 文件名，不能传绝对路径。cron 使用 `--no-agent` 和 `--deliver local`，脚本自身发送且成功时 stdout 为空，避免重复投递。

## 验证与回滚

预演只生成文本，不发送且不更新状态。必须使用与 cron 相同的 Hermes 用户运行；不要用默认 root 身份执行会创建运行态文件的预演，否则 `run.lock` 会变成 root:root、0600 并阻断正式任务：

```bash
docker exec --user 10000:10000 hermes-serve \
  python /opt/data/scripts/act_ai_brief.py --slot evening --dry-run --at 2026-08-17T19:00:00+08:00
```

部署或预演后，核对宿主机 `${HERMES_DATA_DIR}/act-ai-brief/` 中 `run.lock`、`state.json` 与 `errors.jsonl` 均由 UID/GID 10000 持有且权限为 0600。若旧 `run.lock` 所有权错误，只修正该文件所有权，不删除历史状态。

回滚时先移除早报、晚报与恢复 cron，再移除 `/opt/data/scripts/act-ai-brief-*.py`、`/opt/data/scripts/act_ai_brief.py` 和 `act_brief_package.py`。默认保留 `/opt/data/act-ai-brief/` 作为审计与未来恢复依据；若用户明确要求再单独删除。

视觉模式使用 `ACT_BRIEF_VISUAL_DELIVERY=1` 显式开启。关闭该变量即可回到旧文字执行器；`state.json` schema 不变，新的 `delivery-manifests/` 无需迁移。正式开启前必须先完成两轮 shadow render、使用者视觉确认和 Guizang 校验器检查。

早晚入口在生产模式内设置 `ACT_BRIEF_VISUAL_DELIVERY=1` 与 `ACT_BRIEF_RENDER_MODE=queue`。恢复任务使用 Hermes cron `290bb50a020a`，在每小时 07/22/37/52 分运行：始终先恢复最早的未完成 manifest，保持原编号与原新闻，不重发已成功部分；若没有 manifest 但最近一个 07:00/19:00 水位仍欠账，则生成该到期窗口。无欠账时不读取 AIHOT。停用图文时应同时恢复备份入口并移除该恢复 cron。

VPS 正式渲染使用固定版本的 renderer sidecar。它只挂载 `${HERMES_DATA_DIR}/act-ai-brief/render-queue`，以 UID/GID 10000 运行，容器根文件系统只读、网络为 `none`，并启用 `cap-drop ALL`、`no-new-privileges`、CPU/内存/PID 限制；不挂载 ACT、Hermes 配置、消息凭据或其他状态。执行器设置 `ACT_BRIEF_RENDER_MODE=queue` 后只把清洗后的 package、合格官方图和输出目录放入该队列。生产使用的镜像 digest 应记录在私人部署清单中。
