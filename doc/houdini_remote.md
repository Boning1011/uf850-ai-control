# Houdini Remote Control (Claude Code ↔ Houdini)

通过 Houdini 的 Command Port，Claude Code 可以直接操作正在运行的 Houdini 实例。

## Setup

1. 打开 Houdini
2. **Windows → HScript Textport**，输入：
   ```
   openport 9090
   ```

## 工作原理

```
Claude Code → hcommand.exe → TCP port 9090 → Houdini
```

- **hcommand** 发送 HScript 命令到 Houdini
- HScript 的 `python -c "..."` 可以执行完整的 Python/hou 代码
- Python `print()` 输出不会回传，需要写文件做中转

## 常用操作

### HScript 直接命令（简单操作）
```bash
HCMD="/c/Program Files/Side Effects Software/Houdini 21.0.631/bin/hcommand.exe"

"$HCMD" 9090 "opcf /obj/geo1"              # 进入节点
"$HCMD" 9090 "opls"                          # 列出子节点
"$HCMD" 9090 "opadd sphere sph1"             # 创建节点
"$HCMD" 9090 "opwire -n sph1 mountain1"      # 连线
"$HCMD" 9090 "opparm mountain1 height 0.5"   # 设参数
```

### Python 复杂操作（通过文件中转）
1. 写脚本到 `tmp/hou_cmd.py`
2. 执行：
   ```bash
   "$HCMD" 9090 "python -c \"exec(open('C:/Users/vvox/Documents/GitHub/uf850-ai-control/tmp/hou_cmd.py').read())\""
   ```
3. 读结果：`tmp/hou_out.txt`

### Python 脚本模板
```python
import hou

geo = hou.node("/obj/geo1")
# ... 操作 ...

with open("C:/Users/vvox/Documents/GitHub/uf850-ai-control/tmp/hou_out.txt", "w") as f:
    f.write("Done\n")
```

## Headless 模式（hython）

不需要运行 Houdini，直接生成 HDA / .hip 文件：
```bash
"/c/Program Files/Side Effects Software/Houdini 21.0.631/bin/hython.exe" script.py
```

可以：创建节点、打包 HDA、添加参数、嵌入 Python 脚本、导出几何体。

## 注意事项

- Command Port 默认只接受 localhost 连接
- `tmp/` 目录用于 Python 脚本和输出中转
- Houdini 版本：21.0.631（最新安装版本）
