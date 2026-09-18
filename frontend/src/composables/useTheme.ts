// 亮 / 暗主题:初始跟随系统偏好,手动切换后记住选择(localStorage)。
import { onMounted, ref } from "vue";

type Theme = "light" | "dark";

const STORAGE_KEY = "lab-qa-theme";

// 模块级单例,保证各组件读到同一状态。
const theme = ref<Theme>("light");

function apply(t: Theme): void {
  document.documentElement.setAttribute("data-theme", t);
}

export function useTheme() {
  onMounted(() => {
    const saved = localStorage.getItem(STORAGE_KEY) as Theme | null;
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const initial: Theme = saved ?? (prefersDark ? "dark" : "light");
    theme.value = initial;
    apply(initial);
  });

  function toggle(): void {
    const next: Theme = theme.value === "dark" ? "light" : "dark";
    theme.value = next;
    apply(next);
    localStorage.setItem(STORAGE_KEY, next);
  }

  return { theme, toggle };
}
