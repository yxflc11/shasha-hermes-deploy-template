# Shasha Hermes Deploy Template

这是“鲨鲨”系统的可迁移公开壳子：保留部署结构、受控捕获、ACT 只读查询、每日陪伴、简报、人格协议与自动测试，不保存任何真实账户或运行数据。

## 仓库结构

- `deploy/hermes-p2-compose.yaml`：参数化 Compose 模板
- `deploy/hermes-companion/`：每日陪伴、公开文章提取与 Vault 外暂存
- `deploy/hermes-plugins/`：显式捕获插件
- `deploy/hermes-skills/`：业务 Skills、人格模板和评估样本
- `deploy/hermes-ai-brief/`：定时 AI 简报
- `deploy/hermes-weixin-media-ack/`：微信媒体回执补丁与验证
- `scripts/act-context-reader.py`：ACT 只读 MCP
- `scripts/act-*-import.py`：从远端 journal 向本地 ACT 受控导入
- `tests/`：安全边界和行为回归测试
- `PRIVATE_OVERLAY.md`：仓库外私人人格的加载方法

## 私有层

公开仓库只提供基础层。真实部署需要在服务器本地提供：

- Hermes 渠道 Token 与模型 API Key
- Dashboard 凭据
- Bot 用户名、允许用户 ID 和渠道账户
- ACT Vault 数据
- 个性化称呼、用户身份与私人偏好
- journal、session、state.db、日志、简报水位和备份

这些内容由 `.env`、服务器权限为 0600 的配置或独立私人覆盖层提供，永不提交。

## 启动前

1. 复制 `.env.example` 为 `.env`，只在本机填写。
2. 把 `deploy/hermes-skills/act/act-shark-companion/persona.txt` 视为通用基础层；按 [PRIVATE_OVERLAY.md](PRIVATE_OVERLAY.md) 从仓库外加载私人版本。
3. 确认 ACT 只以只读方式挂载。
4. 运行测试和 `./scripts/privacy-audit.sh`。
5. 第一次公开发布必须建立全新 Git 历史，不能继承私人 ACT 仓库历史。

本仓库不包含 Hermes 上游本体；部署前需按 `.env.example` 指定经过验证的镜像或自行构建。

本仓库整体采用 AGPL-3.0-only，以兼容仓库内锁定的 Guizang 衍生渲染部分。详见 [LICENSE](LICENSE) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
