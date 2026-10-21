---
version: "1.0"
updated: 2026-10-21
profile_id: invalid-profile
core_questions:
  - id: Q1
    text: "只有一个核心问题。"
subdirections:
  - id: S1
    name: "唯一子方向"
    priority: high
    target_share: 0.8
include:
  - "anything"
reading:
  weekly_capacity: 5
  deep_skim_ratio: [0.2, 0.3]
---

# 非法档案

这个 fixture 至少有两处错误：核心问题少于 3 个，target_share 与
deep_skim_ratio 之和都不为 1。
