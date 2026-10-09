"""评测工具包:LoCoMo 数据适配、评分协议与报告生成(与 memory_eval.py 配套)。

- `locomo`    原始 LoCoMo 数据 → 评测数据集(schema v2)的适配、校验与历史文本渲染;
- `scoring`   F1 / BLEU-1 本地评分与可选的 LLM 裁判协议(默认不发起任何模型调用);
- `reporting` 汇总 results / scores / build 成 report.json 与 report.md。

约定:这三个模块**不连数据库、不读配置里的密钥**;只有 `scoring` 的裁判函数接受
调用方注入的模型对象(被测代码之外不自己拿模型)。
"""
