// 搜索命中的分段:把一段文本按关键词切成普通段 / 命中段,交给视图用 <mark> 点亮。
// 分词规则必须与后端检索一致(按空白切、全部命中才算),否则会出现
// 「后端说这通命中了、前端在片段里却找不到词」的错位。

export interface Segment {
  t: string;
  hit: boolean;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function highlightSegments(text: string, query: string): Segment[] {
  if (!text) return [];
  const terms = query.split(/\s+/).filter(Boolean);
  if (!terms.length) return [{ t: text, hit: false }];
  const re = new RegExp(`(${terms.map(escapeRe).join("|")})`, "gi");
  const out: Segment[] = [];
  // 带捕获组 split:偶数下标是普通文本,奇数下标是命中词。
  text.split(re).forEach((part, i) => {
    if (part) out.push({ t: part, hit: i % 2 === 1 });
  });
  return out;
}
