// 历史抽屉的开关 + 搜索聚焦信号。
// 模块级单例:App(渲染抽屉 / 遮罩)、ChatView 的会话工具胶囊、HistorySidebar(搜索框)
// 三处共享同一份状态,免去动态 <component> 上层层透传 props。
import { ref } from "vue";

// 抽屉是否展开(固定覆盖层,不占布局)。
const open = ref(false);
// 每次「搜索」按钮点击自增一次,HistorySidebar 监听它来聚焦搜索框。
const focusSearchSignal = ref(0);

// 边栏键:开合抽屉。
function toggleDrawer(): void {
  open.value = !open.value;
}

// 搜索键:打开抽屉并请求聚焦搜索框。
function openSearch(): void {
  open.value = true;
  focusSearchSignal.value += 1;
}

// 收起抽屉(切视图 / 点遮罩 / 从抽屉里导航后)。
function closeDrawer(): void {
  open.value = false;
}

export function useSidebar() {
  return { open, focusSearchSignal, toggleDrawer, openSearch, closeDrawer };
}
