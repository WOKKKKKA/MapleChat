# MapleChat · macOS 版使用说明

冒险岛国际服（怀旧服）聊天 OCR 实时翻译工具 —— macOS 版本。
功能与 Windows 版完全一致：截屏识别 → 本地 OCR → DeepSeek 翻译 → 置顶悬浮窗 → 一键复制回复，另有反向翻译（中→英）与本地快捷组句。

---

## 一、运行前提

| 项目 | 要求 |
|---|---|
| 系统版本 | macOS 12 Monterey 及以上 |
| 芯片 | Apple Silicon（M1/M2/M3/M4）与 Intel 均可 |
| Python | 3.10+（**建议 python.org 官方安装包**，自带 Tk 框架） |
| 游戏 | 冒险岛无原生 Mac 客户端，需在 Parallels / CrossOver / 云游戏中以**窗口化或无边框**方式运行 |

> 独占全屏下系统无法截取游戏画面，截图会全黑。游戏必须窗口化。

---

## 二、首次运行：三项权限（必做）

macOS 没有这些权限，程序能启动但核心功能不可用。

### 1. 屏幕录制（截屏必需）
`系统设置` → `隐私与安全性` → `屏幕录制` → 勾选 `MapleChat`
如未出现，先运行一次程序再回来勾选。勾选后**必须退出并重开程序**才生效。

### 2. 辅助功能（全局热键必需）
`系统设置` → `隐私与安全性` → `辅助功能` → 勾选 `MapleChat`
没有这项权限，`Ctrl+Option+S` 等热键不会有任何反应。

### 3. 解除隔离属性（从网上下载的包必需）
终端执行：

```bash
xattr -dr com.apple.quarantine /Applications/MapleChat.app
```

若仍提示"无法打开，因为无法验证开发者"，在 Finder 里 **右键 → 打开**，点一次"打开"即可。

---

## 三、热键（Mac 键盘）

| 热键 | 功能 |
|---|---|
| `Ctrl+Option+S` | 开始 / 停止扫描翻译 |
| `Ctrl+Option+D` | 快译（翻译剪贴板内容） |
| `Ctrl+Option+Q` | 快捷组句 |
| `Ctrl+Option+H` | 显示 / 隐藏悬浮窗 |
| `Ctrl+Option+M` | 显示 / 隐藏主窗口 |

> Windows 版是 `Ctrl+Alt+*`，Mac 键盘上的 `Option` 就是同一个键（⌥）。
> 界面按钮与热键功能等价，热键失效时可直接点按钮。

---

## 四、配置文件位置

macOS 遵循苹果规范，配置放在用户目录，**替换或升级 App 不会丢失 API Key 与框选坐标**：

```
~/Library/Application Support/MapleChat/config.json
```

Windows 版是放在程序目录，两者不同。若要迁移配置，把旧机的 `config.json` 拷到上述路径即可。

---

## 五、Retina 屏幕说明

Retina 屏物理像素是逻辑点的 2 倍。程序已自动处理换算，框选区域与截屏画面一致，无需手动调整。

如遇到截图区域偏移，先确认：
1. 只在一台显示器上框选（多屏拼接暂未适配跨屏坐标）
2. 框选后不要再改动显示器缩放设置，改了需要重新框选

---

## 六、自己构建（无预编译包时）

PyInstaller 不支持跨平台交叉编译，**必须在 Mac 上构建**。

```bash
# 1. 安装 Python（如未安装）
#    推荐 https://www.python.org/downloads/macos/ 的 3.11 版本

# 2. 进入项目目录
cd /path/to/MapleChat_v4

# 3. 一键构建
chmod +x build_mac.sh
./build_mac.sh
```

产物：

- `dist/MapleChat.app` —— 双击即用
- `dist/MapleChat-mac-arm64.dmg` —— 分发用（Apple Silicon 上是 arm64，Intel 上是 x86_64）

### 构建后自检

```bash
dist/MapleChat.app/Contents/MacOS/MapleChat --selftest
```

输出 `ALL PASS` 表示 OCR 引擎、截屏、术语库、剪贴板、组句引擎全部正常。
若 `screen grab` 一项失败，是还没授予「屏幕录制」权限。

---

## 七、常见问题

| 现象 | 原因与处理 |
|---|---|
| 截图全黑 / 识别不到字 | 游戏是独占全屏 → 改成窗口化；或未授予屏幕录制权限 |
| 热键完全没反应 | 未授予「辅助功能」权限；或与其他软件热键冲突，改用界面按钮 |
| 翻译没反应 / 报 API 错误 | `config.json` 里 DeepSeek Key 未填或已欠费 |
| 首次识别很慢 | OCR 模型冷启动，第一次约 1~2 秒，之后稳定在 0.5~1 秒 |
| 打了 dmg 但拖不进应用程序 | 先 `xattr -dr com.apple.quarantine` 再拖 |
| 提示"已损坏，无法打开" | 同上，这是未签名 App 的隔离属性问题，不是文件损坏 |

> 自 v0.6 起，全局热键改为轮询系统 HID 键状态（`CGEventSourceKeyState`），不再使用 pynput 的事件监听。
> 原因是 pynput 的键码翻译会在子线程里经 ctypes 调用 Carbon 输入源 API（`TISGetInputSourceProperty`），
> 而 HIToolbox 的 `islGetInputSourceListWithAdditions` 断言必须主队列执行，触发即 `EXC_BREAKPOINT` 整进程闪退。
> 升级后热键触发延迟不超过 50ms，属正常。

---

## 八、合规说明

本工具仅截取屏幕像素做文字识别，不注入游戏进程、不读内存、不挂钩子、不模拟游戏键盘操作。
输出只有两个：系统剪贴板与一个独立的置顶窗口。请自行遵守游戏服务条款。
