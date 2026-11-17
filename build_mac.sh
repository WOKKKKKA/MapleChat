#!/bin/bash
# ============================================================
# MapleChat macOS 一键构建
#   产物：dist/MapleChat.app  +  dist/MapleChat-mac-<arch>.dmg
#
# 用法（在仓库根目录，也就是 main.py 所在目录）：
#   chmod +x build_mac.sh
#   ./build_mac.sh
#
# 前置：已安装 Python 3.10+（建议 python.org 官方安装包，自带 Tk 框架版）
# 说明：本脚本必须运行在 macOS 上，PyInstaller 不支持跨平台交叉编译
# ============================================================
set -e
cd "$(dirname "$0")"

APP_NAME="MapleChat"
PY="${PY:-python3}"
ARCH="$(uname -m)"

echo "============================================"
echo " MapleChat macOS 构建   架构: $ARCH"
echo " 系统: $(sw_vers -productName) $(sw_vers -productVersion)"
echo "============================================"

# ---------- 1. 环境检查 ----------
echo "==> [1/7] 检查 Python 与 Tk"
"$PY" - <<'PYEOF'
import sys, tkinter
print("    Python : %s" % sys.version.split()[0])
print("    Tk     : %s" % tkinter.TkVersion)
if tkinter.TkVersion < 8.6:
    sys.exit("    Tk 版本过低，请安装 python.org 官方版 Python 3.10+")
PYEOF

# ---------- 2. 虚拟环境 ----------
echo "==> [2/7] 准备虚拟环境 .venv-mac"
if [ ! -d .venv-mac ]; then
  "$PY" -m venv .venv-mac
fi
# shellcheck disable=SC1091
source .venv-mac/bin/activate
python -m pip install -U pip wheel -q

# ---------- 3. 依赖 ----------
echo "==> [3/7] 安装依赖（首次约 3~6 分钟）"
pip install -r requirements-mac.txt -q

# ---------- 4. 清理 ----------
echo "==> [4/7] 清理旧产物"
rm -rf build dist "$APP_NAME.spec"

# ---------- 5. 打包 ----------
echo "==> [5/7] PyInstaller 打包 .app"
python -m PyInstaller \
  --noconfirm --clean \
  --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier com.maplechat.translator \
  --collect-all rapidocr_onnxruntime \
  --collect-all pynput \
  --hidden-import chatgen \
  --hidden-import plat \
  main.py

# ---------- 6. 资源落位 ----------
# 代码按 sys.argv[0] 的目录（也就是 .app/Contents/MacOS）找 terms.json，
# 这里显式复制过去，避免不同 PyInstaller 版本把数据文件放到 Resources/Frameworks 造成找不到
echo "==> [6/7] 放置资源文件并做 ad-hoc 签名"
cp -f terms.json "dist/$APP_NAME.app/Contents/MacOS/"
[ -f config.example.json ] && cp -f config.example.json "dist/$APP_NAME.app/Contents/MacOS/"

# 无开发者证书时用 ad-hoc 签名；有证书可改成本机 identity
codesign --force --deep --sign - "dist/$APP_NAME.app" 2>/dev/null || echo "    (ad-hoc 签名跳过)"

# ---------- 7. dmg ----------
echo "==> [7/7] 生成 dmg"
rm -f "dist/${APP_NAME}-mac-${ARCH}.dmg"
hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "dist/$APP_NAME.app" \
  -ov -format UDZO \
  "dist/${APP_NAME}-mac-${ARCH}.dmg" >/dev/null

echo
echo "============================================"
echo " 构建完成"
echo "   App : $(pwd)/dist/$APP_NAME.app"
echo "   DMG : $(pwd)/dist/${APP_NAME}-mac-${ARCH}.dmg"
echo
echo " 首次运行前请执行（解除隔离属性）："
echo "   xattr -dr com.apple.quarantine \"/Applications/$APP_NAME.app\""
echo " 然后在 系统设置 → 隐私与安全性 授予「屏幕录制」与「辅助功能」权限"
echo "============================================"
