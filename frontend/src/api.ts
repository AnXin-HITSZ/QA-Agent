// 后端对话接口的类型与调用:非流式 /api/v1/chat + 流式 /api/v1/chat/stream。

export interface ChatRequest {
  message: string;
  thread_id?: string | null;
}

export interface ChatResponse {
  skill: string | null; // 命中的 Skill id;null 表示走通用问答
  content: string;
}

// 流式事件回调。后端 SSE:token/tool_call/tool_result 均带 step(agent 轮次号),done 附 skill。
export interface StreamHandlers {
  onToolCall?: (name: string, args: Record<string, unknown>, step: number) => void;
  onToolResult?: (name: string | null, step: number) => void;
  onToken?: (content: string, step: number) => void;
  onDone?: (skill: string | null) => void;
}

export async function chat(message: string): Promise<ChatResponse> {
  let res: Response;
  try {
    res = await fetch("/api/v1/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message } satisfies ChatRequest),
    });
  } catch {
    throw new Error("没连上后端。确认后端已在 127.0.0.1:8000 运行,然后重试。");
  }
  if (!res.ok) {
    throw new Error(`后端返回错误(HTTP ${res.status})。稍后重试,或查看后端日志。`);
  }
  return (await res.json()) as ChatResponse;
}

// EventSource 不支持 POST,这里用 fetch + ReadableStream 手动解析 SSE 帧(无新依赖)。
export async function chatStream(message: string, handlers: StreamHandlers): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/v1/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message } satisfies ChatRequest),
    });
  } catch {
    throw new Error("没连上后端。确认后端已在 127.0.0.1:8000 运行,然后重试。");
  }
  if (!res.ok || !res.body) {
    throw new Error(`后端返回错误(HTTP ${res.status})。稍后重试,或查看后端日志。`);
  }

  const dispatch = (frame: string): void => {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of frame.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) continue; // 空行 / 注释(含 keep-alive ping)
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    if (!dataLines.length) return;
    let payload: Record<string, unknown> | null = null;
    try {
      payload = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
    } catch {
      payload = null; // 兼容非 JSON 的收尾标记
    }
    const step = Number(payload?.step ?? 0); // agent 轮次号,据此把事件归入对应段
    switch (event) {
      case "tool_call":
        handlers.onToolCall?.(
          String(payload?.name ?? ""),
          (payload?.args as Record<string, unknown>) ?? {},
          step,
 );
        break;
      case "tool_result":
        handlers.onToolResult?.((payload?.name as string) ?? null, step);
        break;
      case "token": {
        const content = payload?.content;
        if (typeof content === "string" && content) handlers.onToken?.(content, step);
        break;
      }
      case "done":
        handlers.onDone?.((payload?.skill as string) ?? null);
        break;
    }
  };

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const sep = /\r?\n\r?\n/;
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let m: RegExpExecArray | null;
    while ((m = sep.exec(buf)) !== null) {
      const frame = buf.slice(0, m.index);
      buf = buf.slice(m.index + m[0].length);
      if (frame.trim()) dispatch(frame);
    }
  }
  if (buf.trim()) dispatch(buf); // flush 尾帧
}
