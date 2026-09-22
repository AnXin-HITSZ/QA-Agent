// 待办分类的可选值:最近自定义过的 ∪ 现有待办里出现过的 ∪ 默认一个「报销」。
//
// 为什么要落 localStorage:分类只从「现有待办」推导的话,用户建了「采购」、事后把那条
// 待办删掉 / 做完清掉,「采购」就从 chips 里消失了,下次还得重敲。存一份最近自定义的
// 就不受待办增删影响。纯前端状态,后端不参与(后端对 category 不做强校验)。
import { computed, ref } from "vue";

import { useTodos } from "./useTodos";

const STORAGE_KEY = "qa:todo-cats:v1";
// 默认分类:只预置最常用的一个。「未分类」不在这里 —— 它是空分类的展示名(不是一枚真分类),
// 由 TodoDrawer 单独渲染成排在最前的 chip。
const DEFAULTS = ["报销"];
// 最近自定义最多记几条(超出的从尾部挤掉)。
const MAX_RECENT = 8;

// 读 localStorage:隐私模式 / 被禁用时会抛,读不出就当空 —— 不能让抽屉因此崩掉。
function readRecent(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x): x is string => typeof x === "string" && x.trim() !== "");
  } catch {
    return [];
  }
}

// 模块级单例:抽屉打开期间反复进出表单,同一份即可。
const recent = ref<string[]>(readRecent());

// 记一条自定义分类(默认分类不必记)。落盘失败不影响本次会话 —— recent 已在内存生效。
function remember(cat: string): void {
  const c = cat.trim();
  if (!c || DEFAULTS.includes(c)) return;
  recent.value = [c, ...recent.value.filter((x) => x !== c)].slice(0, MAX_RECENT);
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(recent.value));
  } catch {
    // 忽略:存储不可用(隐私模式 / 配额满)时退化为「本会话内有效」
  }
}

export function useTodoCategories() {
  const { items } = useTodos();

  // 拼接顺序:最近自定义 → 现有待办里出现过的 → 默认。去重,保序。
  const categories = computed<string[]>(() => {
    const out: string[] = [];
    const seen = new Set<string>();
    const push = (c: string): void => {
      const t = c.trim();
      if (!t || seen.has(t)) return;
      seen.add(t);
      out.push(t);
    };
    recent.value.forEach(push);
    items.value.forEach((t) => push(t.category));
    DEFAULTS.forEach(push);
    return out;
  });

  return { categories, remember };
}
