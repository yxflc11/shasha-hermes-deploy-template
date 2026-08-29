# 仓库外私人覆盖层

公开仓库只提供通用 `persona.txt`。完整人格、私人称呼、语言偏好和完整评估集应放在公开仓库之外，并使用 0700 目录与 0600 文件权限。

建议结构：

```text
/private/path/shasha-persona/
├── brief-copy.private.json
└── hermes-skills/
    ├── act/act-shark-companion/
    │   ├── persona.txt
    │   └── references/language-library.md
    └── evals/
        ├── evals.full.json
        └── review.full.md
```

配置脚本已经接受外部普通文件，不要求它位于本仓库：

```bash
SHASHA_PRIVATE_OVERLAY=/private/path/shasha-persona
python3 deploy/hermes-companion/configure_telegram.py \
  --prompt-file "$SHASHA_PRIVATE_OVERLAY/hermes-skills/act/act-shark-companion/persona.txt" \
  --check
```

`--check` 通过后再执行正式写入。若 Telegram 已有不同提示，必须提供脚本要求的当前 SHA-256 与显式替换参数；不要静默覆盖。

部署脚本只读取该外部文件并写入 Hermes 平台提示，不复制回仓库。公开 Git 状态应始终保持干净。

定时简报的固定私人文案通过 Compose 只读挂载加载：

```bash
docker compose --env-file .env \
  -f deploy/hermes-p2-compose.yaml \
  -f deploy/compose.private-overlay.example.yaml \
  config
```

覆盖文件把仓库外目录挂载到 `/run/shasha-private:ro`，并设置 `ACT_BRIEF_COPY_FILE=/run/shasha-private/brief-copy.private.json`。未启用覆盖文件时，系统使用公开的通用简报文案。
