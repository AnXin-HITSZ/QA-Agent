// 对话状态与发送逻辑。send 走 SSE 流式:按 agent 轮次(step)把事件切成时间线段。
import { reactive, ref } from "vue";

import { chatStream } from "../api";

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
  streaming?: boolean;
}

export function useChat() {
  const messages = ref<Msg[]>([]);
  const loading = ref(false);
  const error = ref("");

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
      await chatStream(q, {
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
        onDone: (skill) => {
          reply.skill = skill;
        },
      });
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

  return { messages, loading, error, send };
}
