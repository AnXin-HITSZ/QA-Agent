// 单例 markdown-it:把后端返回的 Markdown 渲染成安全 HTML。
// html:false → 关闭原始 HTML,LLM 输出无注入面;表格 / 有序列表默认开启。
// 隔离在这里,下一步 SSE 流式可增量复用。
import MarkdownIt from "markdown-it";
import hljsPlugin from "markdown-it-highlightjs";

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: false,
  typographer: false,
}).use(hljsPlugin, { inline: true });

// 从运行时对象取渲染规则类型,避免依赖具体版本的类型命名空间。
type RenderRule = NonNullable<typeof md.renderer.rules.link_open>;

// 外链在新标签打开,并加 rel 防止 opener 泄露。
const defaultLinkOpen: RenderRule =
  md.renderer.rules.link_open ??
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options));

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const href = String(tokens[idx].attrGet("href") ?? "");
  if (/^https?:\/\//i.test(href)) {
    tokens[idx].attrSet("target", "_blank");
    tokens[idx].attrSet("rel", "noopener noreferrer");
  }
  return defaultLinkOpen(tokens, idx, options, env, self);
};

export function renderMarkdown(src: string): string {
  return md.render(src ?? "");
}
