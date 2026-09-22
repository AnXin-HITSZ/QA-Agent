// 待办抽屉的开关（右侧固定覆盖层，不占布局）。
// 模块级单例：App（渲染抽屉 / 遮罩）与 ChatView 的工具键共享同一份开合状态，
// 与左侧历史抽屉的 useSidebar 对称，各管一侧。
import { ref } from "vue";

const open = ref(false);

// 工具键「待办」：开合抽屉。
function toggle(): void {
  open.value = !open.value;
}

// 收起（点遮罩 / 抽屉内关闭键 / 切视图）。
function close(): void {
  open.value = false;
}

export function useTodoDrawer() {
  return { open, toggle, close };
}
