// 单例 markdown-it:把后端返回的 Markdown 渲染成安全 HTML。
// html:false → 关闭原始 HTML,LLM 输出无注入面;表格 / 有序列表默认开启。
// 代码高亮用 core + 按需注册语言,不引 highlight.js 全量包(190 种语言 ≈ 1 MB,曾占主包 89%)。
import MarkdownIt from "markdown-it";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import diff from "highlight.js/lib/languages/diff";
import ini from "highlight.js/lib/languages/ini";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

// 常用语言足够覆盖问答里的代码块;别名(sh、ts、yml、html…)各语言定义自带。
const LANGS = {
  bash,
  diff,
  ini,
  javascript,
  json,
  markdown,
  python,
  sql,
  typescript,
  xml,
  yaml,
};

for (const [name, def] of Object.entries(LANGS)) {
  hljs.registerLanguage(name, def);
}

// 只给「显式声明且已注册」的语言着色。返回空串 = 不接管,
// markdown-it 会退回默认转义渲染,保持素色 —— 不猜语言,免得给文件名 / 路径乱上色。
function highlightCode(code: string, lang: string): string {
  const name = lang.trim().toLowerCase();
  if (!name || !hljs.getLanguage(name)) return "";
  try {
    return hljs.highlight(code, { language: name, ignoreIllegals: true }).value;
  } catch {
    return "";
  }
}

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: false,
  typographer: false,
  highlight: highlightCode,
});

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
