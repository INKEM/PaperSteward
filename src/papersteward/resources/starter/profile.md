---
version: "1.0"
updated: 2026-10-21
profile_id: demo-profile
positioning:
  summary: "研究长文档检索与检索增强生成的可复现方法。"
  domain: "信息检索 / 自然语言处理"
  career_stage: "PhD"
  language: "zh"
core_questions:
  - id: Q1
    text: "如何在不增加标注成本的前提下提升长文档检索的跨段推理能力？"
  - id: Q2
    text: "检索增强生成如何减少引用不忠实与幻觉？"
  - id: Q3
    text: "如何用统一基准比较检索、重排与生成三层方法？"
subdirections:
  - id: S1
    name: "长文档检索与重排"
    priority: high
    target_share: 0.35
    keywords: ["long-document retrieval", "cross-segment reasoning", "reranking"]
  - id: S2
    name: "检索增强生成"
    priority: high
    target_share: 0.30
    keywords: ["RAG", "retrieval-augmented generation", "grounding"]
  - id: S3
    name: "检索与生成的评测"
    priority: medium
    target_share: 0.20
    keywords: ["evaluation", "benchmark", "faithfulness"]
  - id: S4
    name: "文档智能应用"
    priority: medium
    target_share: 0.15
    keywords: ["document understanding", "legal NLP", "enterprise search"]
include:
  - "method papers with reproducible experiments"
  - "surveys that connect retrieval, reasoning and generation"
exclude:
  - "pure prompt-engineering tricks without evaluation"
  - "applications outside document-centric tasks"
methods:
  - "dense retrieval"
  - "cross-encoder reranking"
data:
  - "long-document QA benchmarks"
reading:
  weekly_capacity: 10
  deep_skim_ratio: [0.3, 0.7]
  reading_goals: ["追 SOTA", "找选题"]
  push_time: "08:00"
authority:
  venues: ["ACL", "EMNLP", "SIGIR"]
negative_preferences:
  - "unreproducible results"
change_log:
  - version: "1.0"
    date: 2026-10-21
    reason: "initial profile"
---

# 研究方向档案

## 研究定位

围绕长文档检索与检索增强生成，关注可复现实验与跨段推理。

## 核心问题

1. 降低跨段推理的标注成本。
2. 减少 RAG 的引用不忠实。
3. 建立统一的检索—生成评测。

## 阅读节奏

每周 10 篇，精读/粗读约 3:7。
