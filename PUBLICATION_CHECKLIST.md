# 公开发布检查表

- [ ] 当前目录不是从私人仓库复制来的 `.git` 历史
- [ ] `.env`、凭据、journal、数据库、日志和备份均未跟踪
- [ ] 真实姓名、称呼、账户、IP、域名和绝对路径均已替换
- [ ] 完整语言库、私人情绪样本和完整评估集只存在于仓库外覆盖层
- [ ] `./scripts/privacy-audit.sh` 通过
- [ ] Python 测试通过
- [ ] Compose 只读挂载 ACT，Dashboard 只绑定本机
- [ ] 根目录 AGPL-3.0-only 声明、Guizang 完整许可证与第三方来源记录保留完整
- [ ] 人工检查 `git diff --cached` 中的每一个文件
- [ ] 首次推送后启用 GitHub Secret Scanning
