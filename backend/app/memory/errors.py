"""长期记忆的异常类型。

与计量 / 认证同一种姿态：**没配依赖不等于猜着跑**。
- 记忆是软依赖（没配 MySQL / Qdrant 时聊天照常，只是不带长期记忆）；
- 但记忆自己的接口与后台任务绝不静默假装成功：配置缺失抛 MemoryNotConfigured
  （接口层转 503 + 明确原因），数据问题抛 MemoryNotFound / MemoryConflict。
"""

from __future__ import annotations


class MemoryNotConfigured(RuntimeError):
    """未配置 MySQL（MYSQL_URL）：长期记忆不可用（聊天侧静默跳过，接口层转 503）。"""


class MemoryDisabled(RuntimeError):
    """显式关闭（MEMORY_ENABLED=false）：不报错、不起任务，接口层说明现状。"""


class MemoryNotFound(LookupError):
    """要操作的记忆不存在，或不属于当前用户（对外统一 404，不区分两者）。"""


class MemoryExtractionError(RuntimeError):
    """模型输出无法按提取协议解析（结构错误，有限重试后仍未通过）。

    失败必须**可见**：任务进 failed、last_error 写明原因，绝不把「解析不了」静默当成
    「这次没有新事实」——那会让用户以为记忆系统正常工作，其实一直在丢内容。
    """


class MemoryConflict(RuntimeError):
    """与现有数据冲突（如同一用户已有相同正文的有效记忆，且调用方要求拒绝重复）。"""


class MemoryLeaseLost(RuntimeError):
    """本次认领的租约已不属于自己（过期后被别人接管 / 认领凭证对不上）。

    这不是「业务冲突」而是「执行者已经失去了写权限」：拿到它就**绝不能**落库、
    发布索引或把任务标成成功 —— 只能停下来，让接管者去做。单独成类是为了不被
    `except MemoryConflict` 之类的兜底顺手吞掉（那正是「旧 Worker 失去租约后仍能
    写入事实」的成因）。
    """


class MemoryDecisionRejected(RuntimeError):
    """整条维护决策不生效(不合法 / 越权 / 重复 / 冲突 / 超限)。

    它不是「部分成功」:调用方要么整条决策重来(重新召回候选 + 重新决策,或带纠正提示
    有限重试),要么如实失败。绝不允许「能做的先做掉」—— 那正是「非法维护决策被部分执行」
    的成因:DELETE + ADD 这种成对动作只做一半,用户的记忆就残了。
    """


class MemoryStaleGeneration(MemoryConflict):
    """本次写入所依据的记忆代次已过期：期间用户「彻底删除」过记忆，整批作废。

    单独成类，是因为**它的语义与「正文重复」完全不同**：重复是正常的去重结果，
    代次过期是「用户要求清掉的东西一概不许再出现」—— 落库路径必须把它原样上抛，
    绝不能被 `except MemoryConflict` 顺手记成「已存在相同事实，跳过」（那会让任务
    显示成功、用户以为记下了，其实一条都没写）。
    """
