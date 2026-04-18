# 🔍 ComfyUI 健康检查器

轻量级 ComfyUI 插件健康监控工具，实时检测插件导入状态并输出彩色摘要报告。

## 特性

- 📦 **自动统计** 已安装插件总数（文件夹 + .py 文件）
- ✅ **检测导入成功** 通过实时日志捕获
- ❌ **列出失败插件** 带完整路径便于调试
- 📊 **健康度百分比** 计算
- 🧠 **节点类总数** 统计
- 🎨 **彩色控制台输出**（ANSI 颜色）
- ⏱️ **延迟报告**（等所有插件加载完成后）

## 安装

### 方法一：ComfyUI Manager（推荐）
1. 打开 ComfyUI Manager
2. 点击 "Custom Nodes Manager"
3. 搜索 "HealthCheck"
4. 安装并重启

### 方法二：Git Clone
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/love530love/ComfyUI-HealthCheck.git
```

### 方法三：手动安装
1. 下载 `ComfyUI_HealthCheck.py`
2. 复制到 `ComfyUI/custom_nodes/`
3. 重启 ComfyUI

## 使用说明

无需配置。ComfyUI 启动后查看控制台输出即可。

## 工作原理

1. **伪装成节点**：注册虚拟节点避免自身被标记为 IMPORT FAILED
2. **日志捕获**：重定向 stdout/stderr 检测 "(IMPORT FAILED)" 标记
3. **延迟输出**：等待 15 秒确保所有插件加载完成
4. **彩色报告**：ANSI 颜色编码的控制台摘要

## 兼容性

- Windows / Linux / macOS
- Python 3.8+
- ComfyUI 0.1.0+

## 许可证

MIT License