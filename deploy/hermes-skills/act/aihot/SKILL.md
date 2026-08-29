---
name: aihot
description: "查询带有明确新闻或时间范围意图的实时 AIHOT 中文 AI 资讯。用户主动问‘今天 AI 圈有什么’、‘AI 日报’、‘AI 热点’、‘最近 AI 新闻’，或每日开场需要 AI 新闻时使用。模型名称本身、模型突出点、规格核实或横向比较不触发本 Skill；这类当前事实交给 act-web-research 独立联网。不处理定时简报的‘展开N’‘第N条详情’。"
version: 0.3.3
author: ACT template contributors
metadata:
  hermes:
    tags: [AI, News, AIHOT, Briefing]
---

# AIHOT for Hermes

调用 `companion_aihot` 获取实时数据；没有成功工具结果就不能生成“今日 AI 新闻”。

## 路由

- 用户回复定时简报说 `展开N`、`展开 N`、`展开第N条`、`第 N 条详情` 或 `更多新闻` 时，不调用 `companion_aihot`；交给 `act-news-brief` 读取原简报。原简报读取失败也不能改用实时列表替代。
- 用户追问某个模型有什么突出点、规格如何、是否真实发布，或要求和另一模型比较时，不调用 `companion_aihot`；即使对象来自上一轮 AIHOT，也只继承对象指代，交给 `act-web-research` 独立联网核实。
- 宽问题、今日 AI 圈：`hours=24`，精选 5—10 条。
- 最近一周：`hours=168`。
- 用户明确说“日报”时仍使用实时精选，但标题写“AIHOT 日报”，不要假装取得了不存在的日报正文。

## 输出

- 按模型、产品、行业、论文、技巧等实际类别分组；空类别省略。
- 编号贯穿全文。
- 每条包含标题、来源、北京时间/相对时间、50 字内摘要和 URL。
- `summary` 是 AIHOT 的外部摘要，不是原文引用；重要或惊人的结论提醒回原链接核对。
- 不在聊天中暴露工具参数、端点、限流或内部实现。
