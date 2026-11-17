# MapleChat

冒险岛国际服怀旧聊天 OCR 翻译工具 —— 截屏识别聊天栏 → 云端 LLM 翻译 → 悬浮窗实时显示，同时提供反向翻译和中英双向快捷组句。

纯本地运行，Python + Tkinter 单进程，不需要注入游戏、不读取游戏内存。

![主界面](screenshot-main.png)

---

## 功能特性

| 模块 | 说明 |
| --- | --- |
| **实时聊天翻译** | 框选聊天区域后按固定间隔截屏 + RapidOCR 识别 + DeepSeek 翻译，译文推到始终置顶的悬浮窗 |
| **悬浮窗** | 无边框半透明，可拖动/滚轮调透明度/`Ctrl+滚轮` 调字号，双击复制全文 |
| **打字回复（反向翻译）** | 输入中文 → 回车直译成英文并自动复制，直接在游戏聊天框粘贴发送 |
| **快捷组句** | 本地零成本组句器，复刻 mscw generator 的 8 大场景 / 51 个动作，不消耗任何 API 额度 |
| **术语库** | 391 个词条 / 11 个分组 / 740 个别名，OCR 后与翻译前双向纠错，避免技能名、地图名、道具名被乱翻 |
| **深色卡片式 UI** | 分区折叠布局，窗口与悬浮窗都做了屏幕边界自适应 |

![悬浮窗](screenshot-overlay.png)

---

## 快速开始

### 方式一：直接运行 EXE（推荐）

1. 下载仓库中的 `MapleChat.exe` 与同目录的 `_internal` 文件夹，**两者必须放在同一目录**
2. 双击 `启动.vbs`（无黑框启动）或直接双击 `MapleChat.exe`
3. 首次运行在「翻译设置」里填入自己的 DeepSeek API Key，点「重新框选」划定游戏聊天栏区域
4. 按 `Ctrl+Alt+S` 开始翻译

> `config.json` 存有 API Key 与屏幕坐标，**不会**提交到本仓库；首次运行会自动生成，也可以从 `config.example.json` 复制一份。

### 方式二：从源码运行

环境要求：Windows 10/11 + Python 3.11

```bat
python -m venv C:\MapleChat_rt
C:\MapleChat_rt\Scripts\python.exe -m pip install -r requirements.txt
C:\MapleChat_rt\Scripts\python.exe main.py
```

也可以直接双击 `安装环境.bat` 自动建环境装依赖，再双击 `启动.bat`（带控制台，方便看日志）或 `启动（无黑框）.vbs`。

> 虚拟环境刻意放在 `C:\MapleChat_rt` 这类短路径下：`onnxruntime` / `opencv` 的默认包路径在项目目录内会触发 Windows 260 字符长路径限制，装不上。

自检（不启动界面，只验证 OCR / 截屏 / 术语库 / 剪贴板 / 组句链路）：

```bat
MapleChat.exe --selftest
```

---

## 全局热键

| 热键 | 功能 |
| --- | --- |
| `Ctrl+Alt+S` | 开始 / 停止实时翻译 |
| `Ctrl+Alt+D` | 快译剪贴板内容（备选 `Ctrl+Alt+G`） |
| `Ctrl+Alt+Q` | 打开快捷组句 |
| `Ctrl+Alt+H` | 显示 / 隐藏悬浮窗 |
| `Ctrl+Alt+M` | 显示 / 隐藏主窗口 |

界面内快捷键：

- 打字回复框：`回车` 直译并复制，`Shift+回车` 换行
- 悬浮窗：双击复制全文，滚轮调透明度，`Ctrl+滚轮` 调字号

---

## 配置说明

`config.json`（首次运行自动生成）

| 字段 | 说明 |
| --- | --- |
| `api_key` | DeepSeek API Key，为空时无法翻译 |
| `api_base` | 接口地址，默认 `https://api.deepseek.com` |
| `model` | 模型名，默认 `deepseek-chat` |
| `region` | 聊天栏截屏区域 `[x, y, w, h]`，由「重新框选」写入 |
| `target_lang` | 目标语言，默认简体中文 |
| `interval_ms` | 截屏识别间隔，默认 700ms |
| `scale` / `binarize` | OCR 放大倍数与二值化，小字号聊天框建议开启 |
| `only_when_foreground` | 仅当游戏窗口在前台时才截屏，省额度 |
| `window_keyword` | 前台窗口标题关键字，留空则不限 |
| `terms_enabled` | 是否启用术语库纠错 |
| `overlay` | 悬浮窗位置、尺寸、透明度、字号、最大行数 |

---

## 术语库

`terms.json` 收录 391 个词条、11 个分组、740 个别名，数据整理自 [mscw-guidebook.com](https://mscw-guidebook.com)，版本标记 `2026-11-17`。结构为分组 → 中文名 → 英文别名列表，可直接编辑扩充，OCR 命中别名时会优先按术语翻译，避免专有名词被模型自由发挥。

---

## 成本

只翻译真正发生变化的聊天行，走 `deepseek-chat`，单条消息约 **0.0002 元**。日常挂机按千条/天估算，一天不到两毛钱。快捷组句完全离线，不产生费用。

---

## 合规说明

- 只通过系统截屏读取**屏幕上已渲染的像素**，不注入进程、不读写游戏内存、不修改客户端文件、不拦截网络封包
- 对外唯一输出是系统剪贴板与一个独立悬浮窗，游戏客户端感知不到本工具存在
- 严格的 OCR 截屏方案在原理上等同于「用眼睛看 + 手动打字」，但仍请自行评估所在服务器的用户协议风险

---

## 已知限制

- 游戏必须使用**窗口化 / 无边框窗口**模式：独占全屏下系统截屏只能拿到纯黑画面
- OCR 对小字号、低对比度、花体字的识别率有限，建议把聊天栏字号调大、开启 `binarize`
- 悬浮窗与游戏同屏显示需要游戏处于窗口化模式

---

## 目录结构

```
MapleChat_v4/
├─ MapleChat.exe          打包后的主程序
├─ _internal/             打包运行时（Python 解释器 + 模型 + 依赖）
├─ main.py                主程序源码（OCR / 翻译 / 悬浮窗 / UI）
├─ chatgen.py             快捷组句数据与生成逻辑
├─ terms.json             术语库
├─ config.example.json    配置模板
├─ requirements.txt       源码运行依赖
├─ 使用说明.md             详细使用文档
├─ 启动.vbs                无黑框启动 EXE
├─ 启动.bat / 调试启动.bat  源码启动（带控制台便于排错）
└─ 安装环境.bat            一键创建运行环境
```

---

## License

MIT
