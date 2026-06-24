# 前端改造摘要：主题 / 响应式 / 移动端侧边栏

## 目标

1. **多主题切换**：在设置页切换主题，持久化到 `localStorage`。
2. **响应式布局**：PC 与手机自适应。
3. **移动端侧边栏**：默认隐藏，通过汉堡按钮打开左侧抽屉。

---

## 主题列表（8 套）

| 主题 | 风格 |
|------|------|
| `default` | 当前默认亮色 |
| `vscode` | VS Code 暗色 |
| `game-engine` | 暗色 + 琥珀主色 |
| `synthwave84` | 暗紫 + 柔和霓虹点缀 |
| `mint` | 护眼薄荷绿（低饱和） |
| `paper` | 护眼羊皮纸（低饱和） |
| `ocean` | 护眼海洋蓝（低饱和） |
| `lavender` | 护眼薰衣草紫（低饱和） |

实现方式：`index.css` 中为每套主题写 `[data-theme="xxx"]` 变量覆盖；`preferenceStore` 持久化用户选择；`ThemeProvider` 在 `useLayoutEffect` 中把 `data-theme` 写入 `<html>`。

---

## 响应式外壳

- `AppLayout.tsx`：PC 保持固定侧边栏 `pl-64`；手机下隐藏固定侧边栏，主内容区全宽。
- `Sidebar.tsx`：拆分为 `SidebarContent` + 固定外壳，手机通过左侧 `Sheet` 抽屉承载。
- `Header.tsx`：手机端显示汉堡按钮，点击打开抽屉；导航后抽屉自动关闭。
- `CodeCanvas.tsx`：工具栏按钮文字在小屏隐藏，允许换行。

---

## 设置页整合

`/settings`（`Profile.tsx`）采用 `grid grid-cols-1 lg:grid-cols-2`：

- 个人信息
- 账户
- 主题
- 语言（原 Header 语言入口已移除并迁移至此）

四张卡片 PC 端两两一排、平均分配；手机端竖排。

---

## 文件变更

### 新增
- `frontend/src/stores/preferenceStore.ts`
- `frontend/src/components/common/ThemeProvider.tsx`

### 修改
- `frontend/src/index.css`
- `frontend/src/main.tsx`
- `frontend/src/App.tsx`
- `frontend/src/stores/index.ts`
- `frontend/src/components/layout/Sidebar.tsx`
- `frontend/src/components/layout/Header.tsx`
- `frontend/src/components/layout/AppLayout.tsx`
- `frontend/src/pages/settings/Profile.tsx`
- `frontend/src/locales/{en,zh-CN,ja,ko}/settings.json`
- `frontend/src/locales/{en,zh-CN,ja,ko}/common.json`
- `frontend/src/pages/code/CodeCanvas.tsx`

### 删除
- `frontend/src/pages/settings/Appearance.tsx`（功能合并到 Profile）

---

## 验证

- [ ] 8 套主题可切换，刷新后保持。
- [ ] PC 侧边栏常驻，手机侧边栏默认隐藏、汉堡按钮可打开。
- [ ] 设置页四张卡片 PC 两列、手机单列。
- [ ] `npm run build` 通过。
