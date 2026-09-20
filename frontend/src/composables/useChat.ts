// 对话状态与发送逻辑 + 历史会话(后端 Redis 为准)。
// 模块级单例:App 与 HistorySidebar 共享同一份状态,不必层层透传 props。
// send 走 SSE 流式:按 agent 轮次(step)把事件切成时间线段。
import { reactive, ref } from "vue";

import {
  chatStream,
  deleteConversation,
  getConversation,
  listConversations,
  type ConversationSummary,
  type Source,
} from "../api";

// 一次工具调用的活动项。
export interface ToolActivity {
  name: string;
  args?: Record<string, unknown>;
  done?: boolean;
}

// 一个 agent 轮次(step)= 一段思考/回答文本 + 该轮发起的工具调用。
// tools.length > 0 → 思考段(该轮以调用工具收尾);最后一段 tools.length === 0 → 最终答案。
export interface Step {
  text: string;
  tools: ToolActivity[];
}

export interface Msg {
  role: "user" | "assistant";
  content: string; // 用户消息文本;助手消息改用 steps,content 留空
  steps?: Step[]; // 助手 ReAct 时间线,按 step 顺序
  skill?: string | null;
  sources?: Source[]; // 本次回答引用的知识库来源(done 事件回填)
  streaming?: boolean;
}

// ── 模块级单例状态 ──
const messages = ref<Msg[]>([]);
const loading = ref(false);
const error = ref("");
// 会话线程 ID:首轮由后端 meta 事件回传,存下后每次提问回传以续接跨轮记忆。
const threadId = ref<string | null>(null);
// 历史会话列表(最近活跃在前)+ 后端是否开启跨轮记忆(false 时前端隐藏历史栏)。
const conversations = ref<ConversationSummary[]>([]);
const historyEnabled = ref(false);

// 拉取历史会话列表;后端未连通时静默置空,不打扰主流程。
async function loadConversations(): Promise<void> {
  const { enabled, items } = await listConversations();
  historyEnabled.value = enabled;
  conversations.value = items;
}

async function send(text: string): Promise<void> {
  const q = text.trim();
  if (!q || loading.value) return;

  error.value = "";
  messages.value.push({ role: "user", content: q });

  // 占位的助手消息用 reactive,流式过程中原地增量更新(引用即代理,变更可追踪)。
  const reply = reactive<Msg>({
    role: "assistant",
    content: "",
    steps: [],
    skill: null,
    sources: [],
    streaming: true,
  });
  messages.value.push(reply);
  loading.value = true;

  // 惰性创建到第 i 段(含),返回该段代理;各段也用 reactive 以便原地增量。
  const ensureStep = (i: number): Step => {
    const steps = reply.steps!;
    while (steps.length <= i) steps.push(reactive<Step>({ text: "", tools: [] }));
    return steps[i];
  };

  try {
    await chatStream(
      q,
      {
        onMeta: (id) => {
          if (id) threadId.value = id;
        },
        onToolCall: (name, args, step) => {
          ensureStep(step).tools.push({ name, args, done: false });
        },
        onToolResult: (name, step) => {
          // 在该轮里从后往前找同名未完成的活动,标记完成。
          const s = reply.steps?.[step];
          const act = s ? [...s.tools].reverse().find((a) => a.name === name && !a.done) : undefined;
          if (act) act.done = true;
        },
        onToken: (content, step) => {
          ensureStep(step).text += content;
        },
        onDone: (skill, sources) => {
          reply.skill = skill;
          reply.sources = sources;
        },
      },
      threadId.value,
    );
    // 一轮结束刷新历史:首轮让新对话冒出来,后续更新标题 / 条数 / 时间。
    void loadConversations();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
    // 全程没吐出任何内容 → 移除空占位,避免留一条空回答。
    const empty = !reply.steps?.some((s) => s.text || s.tools.length);
    if (empty) {
      messages.value = messages.value.filter((m) => m !== reply);
    }
  } finally {
    reply.streaming = false;
    loading.value = false;
  }
}

// 开一通新对话:丢掉线程 ID(后端下次自动新开一个记忆桶)并清空界面。
function newConversation(): void {
  if (loading.value) return;
  threadId.value = null;
  messages.value = [];
  error.value = "";
}

// 打开一通历史会话:拉后端最新状态回放成 Q&A,并续接其线程记忆。
// 回放只还原最终答案文本(把每条助手答案塞进单一 step),不重建当时的工具时间线。
async function openConversation(tid: string): Promise<void> {
  if (loading.value || threadId.value === tid) return;
  error.value = "";
  try {
    const detail = await getConversation(tid);
    messages.value = detail.messages.map((m) =>
      m.role === "user"
        ? ({ role: "user", content: m.content } as Msg)
        : ({
            role: "assistant",
            content: "",
            steps: [{ text: m.content, tools: [] }],
            skill: null,
            streaming: false,
          } as Msg),
    );
    threadId.value = detail.thread_id;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
}

// 删除一通历史会话(后端连带清掉它在 Redis 的记忆);删的是当前通就转新对话。
async function removeConversation(tid: string): Promise<void> {
  try {
    await deleteConversation(tid);
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
    return;
  }
  if (threadId.value === tid) newConversation();
  await loadConversations();
}

export function useChat() {
  return {
    messages,
    loading,
    error,
    threadId,
    conversations,
    historyEnabled,
    send,
    newConversation,
    openConversation,
    removeConversation,
    loadConversations,
  };
}
