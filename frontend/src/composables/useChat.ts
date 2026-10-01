// 对话状态与发送逻辑 + 历史会话(后端 Redis 为准)。
// 模块级单例:App 与 HistorySidebar 共享同一份状态,不必层层透传 props。
// send 走 SSE 流式:按 agent 轮次(step)把事件切成时间线段。
import { reactive, ref } from "vue";

import {
  chatStream,
  deleteConversation,
  getConversation,
  listConversations,
  StreamAborted,
  type ConversationSummary,
  type Source,
  type SopImageReference,
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
  uid: number; // 列表 key:换对话时数组整体替换,用下标当 key 会让 DOM 被复用(折叠状态串台)
  images?: SopImageReference[];
  role: "user" | "assistant";
  content: string; // 用户消息文本;助手消息改用 steps,content 留空
  steps?: Step[]; // 助手 ReAct 时间线,按 step 顺序
  skill?: string | null;
  sources?: Source[]; // 本次回答引用的知识库来源(done 事件回填)
  streaming?: boolean;
  stopped?: boolean; // 用户点了「停止」而中断(与出错区分:不报错,只在答案末尾留一句说明)
}

// ── 模块级单例状态 ──
const messages = ref<Msg[]>([]);
const loading = ref(false);
const error = ref("");
// 会话线程 ID:首轮由后端 meta 事件回传,存下后每次提问回传以续接跨轮记忆。
const threadId = ref<string | null>(null);
// 历史会话列表(最近活跃在前)+ 后端是否开启跨轮记忆(false 时前端隐藏历史栏)。
// historyDegraded:记忆已启用但这次读取失败(超时/Redis 错误)—— 与"未启用"分开显示,可重试。
const conversations = ref<ConversationSummary[]>([]);
const historyEnabled = ref(false);
const historyDegraded = ref(false);
// 搜索:关键词由后端做内容检索(前端只有标题与条数,搜不了正文);空串 = 全部。
const searchQuery = ref("");
const searching = ref(false);
// 列表请求序号:搜索与刷新共用 —— 打字快时后发请求会作废先前在途的响应,避免旧结果盖新结果。
let listSeq = 0;
// 在流的那次请求,供「停止」中断;用户主动停不算失败,只在消息上留标记。
let inflight: AbortController | null = null;
// 消息 uid 发号器:只在本次会话里保证唯一,足够当列表 key。
let nextUid = 0;

function stop(): void {
  inflight?.abort();
}

// 拉取历史会话列表(带当前关键词);失败时进入 degraded(可重试)而非误报"未启用"。
async function loadConversations(): Promise<void> {
  const seq = ++listSeq;
  searching.value = true;
  const { enabled, degraded, items } = await listConversations(searchQuery.value);
  if (seq !== listSeq) return; // 期间又发起了新的加载 / 搜索,这份结果已过期
  historyEnabled.value = enabled;
  historyDegraded.value = degraded;
  conversations.value = items;
  searching.value = false;
}

// 设置搜索关键词并重拉列表;防抖交给输入框(后端每趟都要扫全部 checkpoint,不能逐字打)。
function setSearch(q: string): void {
  const next = q.trim();
  if (next === searchQuery.value) return;
  searchQuery.value = next;
  void loadConversations();
}

async function send(text: string): Promise<void> {
  const q = text.trim();
  if (!q || loading.value) return;

  error.value = "";
  messages.value.push({ uid: nextUid++, role: "user", content: q });

  // 占位的助手消息用 reactive,流式过程中原地增量更新(引用即代理,变更可追踪)。
  const reply = reactive<Msg>({
    uid: nextUid++,
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

  const ac = new AbortController();
  inflight = ac;

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
        onDone: (skill, sources, images) => {
          reply.skill = skill;
          reply.sources = sources;
          reply.images = images;
        },
      },
      threadId.value,
      ac.signal,
    );
    // 一轮结束刷新历史:首轮让新对话冒出来,后续更新标题 / 条数 / 时间。
    void loadConversations();
  } catch (e) {
    if (e instanceof StreamAborted) {
      // 用户自己按的停止:保留已生成的部分,不当失败报错。
      reply.stopped = true;
    } else {
      error.value = e instanceof Error ? e.message : String(e);
      // 全程没吐出任何内容 → 移除空占位,避免留一条空回答。
      const empty = !reply.steps?.some((s) => s.text || s.tools.length);
      if (empty) {
        messages.value = messages.value.filter((m) => m !== reply);
      }
    }
  } finally {
    reply.streaming = false;
    loading.value = false;
    if (inflight === ac) inflight = null;
  }
}

// 开一通新对话:先中断在流的回答,再丢掉线程 ID(后端下次自动新开一个记忆桶)并清空界面。
function newConversation(): void {
  stop();
  threadId.value = null;
  messages.value = [];
  error.value = "";
}

// 打开一通历史会话:拉后端最新状态回放成 Q&A,并续接其线程记忆。
// 回放只还原最终答案文本(把每条助手答案塞进单一 step),不重建当时的工具时间线。
async function openConversation(tid: string): Promise<void> {
  if (threadId.value === tid) return;
  stop(); // 有回答正在流 → 先中断,免得旧流继续往已经切走的消息上写
  error.value = "";
  try {
    const detail = await getConversation(tid);
    messages.value = detail.messages.map((m) =>
      m.role === "user"
        ? ({ uid: nextUid++, role: "user", content: m.content } as Msg)
        : ({
            uid: nextUid++,
            role: "assistant",
            content: "",
            steps: [{ text: m.content, tools: [] }],
            images: m.images ?? [],
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
    historyDegraded,
    searchQuery,
    searching,
    setSearch,
    send,
    stop,
    newConversation,
    openConversation,
    removeConversation,
    loadConversations,
  };
}
