# 🔍 ComfyUI HealthCheck

A lightweight health check plugin for ComfyUI that monitors custom node import status and provides colorful summary reports.

## Features

- 📦 **Auto-count** total installed plugins (folders + .py files)
- ✅ **Detect successful imports** via real-time log capture
- ❌ **List failed plugins** with full paths for easy debugging
- 📊 **Health percentage** calculation (success rate)
- 🧠 **Total node classes** count
- 🎨 **Colorful console output** (ANSI colors)
- ⏱️ **Delayed reporting** (after all plugins loaded)

## Installation

### Method 1: ComfyUI Manager (Recommended)
1. Open ComfyUI Manager
2. Click "Custom Nodes Manager"
3. Search for "HealthCheck"
4. Install and restart

### Method 2: Git Clone
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/love530love/ComfyUI-HealthCheck.git
```

### Method 3: Manual
1. Download `ComfyUI_HealthCheck.py` from [Releases](https://github.com/love530love/ComfyUI-HealthCheck/releases)
2. Copy to `ComfyUI/custom_nodes/`
3. Restart ComfyUI

## Usage

No configuration needed. After ComfyUI starts, check the console output:

```
============================================================
                    🚀 ComfyUI Plugin Health Report
============================================================
📦 Total Plugins: 306 (folders: 289, .py: 17)
✅ Successful: 306
❌ Failed: 0
📊 Health: 100.0%
🧠 Node Classes: 6890

🎉 All plugins loaded successfully!
============================================================
```

## How It Works

1. **Disguised as Node**: Registers a dummy node to avoid "IMPORT FAILED"
2. **Log Capture**: Hijacks stdout/stderr to detect "(IMPORT FAILED)" markers
3. **Delayed Output**: Waits 15s for all plugins to finish loading
4. **Color Report**: ANSI-colored summary in console

## Compatibility

- Windows / Linux / macOS
- Python 3.8+
- ComfyUI 0.1.0+

## Links

- GitHub: https://github.com/love530love/ComfyUI-HealthCheck
- Issues: https://github.com/love530love/ComfyUI-HealthCheck/issues
- CSDN Blog: https://aicity.blog.csdn.net/article/details/156202270

## License

MIT License