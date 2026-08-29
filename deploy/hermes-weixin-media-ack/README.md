# Hermes 微信媒体回执修复

用于修复锁定版 Hermes 微信适配器把 HTTP 200 的业务失败误判为图片发送成功的问题。

- 基础镜像锁定为 Hermes 0.20.6 / `v2026.8.27`：`nousresearch/hermes-agent@sha256:e0df6adebddf29b91112aefc999d4aaf6846c9eb544faca5672a16a13590ff79`（上游 commit `5fc308a70719a83cccdbba4c0e39c23f5a8239d5`）。
- 补丁只接受原始 `weixin.py` 哈希 `3354b015bd56c300bf251b131116714fbc0912462d4fdb8dd5311ad9d4e0e9a2`，上游文件不一致时构建直接失败。
- 媒体 `sendmessage` 遵循腾讯官方实现的响应语义：字典响应中没有非零 `ret/errcode` 即成功，包含空对象 `{}`；限流、会话失效和其他非零业务错误均返回失败，非字典异常响应也失败。
- 会话失效时只允许使用同一 `client_id` 去掉旧 `context_token` 重试一次。
- 原生 `web_extract` 额外锁定上游 `tools/web_tools.py` 哈希 `427dab1a…6c18`，schema 与运行时都要求每次恰好一个 URL；多 URL 调用返回可重试错误，规避 0.20.6 的批量结果错配问题，同时仍保留官方 `web_extract` 工具名与实现。
- 派生镜像不增加依赖、挂载或 ACT 权限；Telegram 的只读 `web` 工具由 Hermes 配置单独控制，不属于本补丁。回退恢复升级前镜像与完整 Hermes 状态快照。
- `repair_false_media_ack.py` 只允许处理 `delivery-manifests/*/manifest.json` 中尚未完成的图片回执，并把原时间戳和原因写入 `repair_history` 后原子替换文件。
- `confirm_observed_delivery.py` 只在用户明确确认看见图片后，把一个尚未完成的 image part 写成已送达，并保留确认时间、实际发送时间和证据说明；不得用于推测性补账。

离线验证应在构建后的镜像中运行：

```bash
python -m py_compile /opt/hermes/gateway/platforms/weixin.py
python -m py_compile /opt/hermes/tools/web_tools.py
python /tmp/smoke_media_ack.py
```
