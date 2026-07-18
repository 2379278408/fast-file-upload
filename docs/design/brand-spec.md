# MonkeyCode 文件工作台视觉基线

来源：现有线上产品的 HTML 与 `styles.css`。以下 OKLCH 值由现有颜色转换并归一为可复用设计令牌。

## 核心令牌

```css
:root {
  --bg: oklch(96% 0.012 257);
  --surface: oklch(100% 0 0);
  --fg: oklch(25% 0.035 258);
  --muted: oklch(54% 0.03 257);
  --border: oklch(91% 0.012 257);
  --accent: oklch(55% 0.22 263);

  --font-display: "Outfit", "Avenir Next", -apple-system, BlinkMacSystemFont, sans-serif;
  --font-body: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-mono: "JetBrains Mono", "SFMono-Regular", Consolas, monospace;
}
```

## 布局姿态

- 现有 24px 大圆角与重阴影收敛为 10–16px 圆角、1px 发丝边框；仅浮层保留阴影。
- 主蓝只用于主操作和当前选中态；传输成功、在线与进度使用青绿色语义色。
- 从纵向面板堆叠改为固定侧栏 + 顶部状态栏 + 双栏工作区，让上传、文件库和活动记录同时可见。
- 数量、容量、速度与时间使用等宽数字；标题继续保留 Outfit 的几何识别。
- 桌面优先，但在 1024px、768px 和 430px 下重排导航与内容，不压缩桌面卡片。
