# Devlog

Development timeline — newest first. Record milestone results and verified conclusions, not process noise.

---

## 2026-02-25 — 连续运动 + 自动错误恢复机制验证

**验证结果：**
- 3D Lissajous figure-8 轨迹在模拟器上连续运行，无需人工干预
- SDK 回调机制 (`register_error_warn_changed_callback`) 可实时捕获错误
- 自动恢复 4 步序列可行：`clean_error()` → `motion_enable(True)` → `set_mode(0)` → `set_state(0)`

**关键发现：错误处理不需要点 Web UI 弹窗**
- 所有错误（碰撞、超速、超限等）都可以通过 SDK 的 `clean_error` + 重新使能来自动恢复
- Web UI 弹窗只是前端行为，SDK 层面完全可以绕过
- 错误码 31（碰撞异常电流）是最常见的碰撞检测码

**踩坑：**
- `set_collision_sensitivity()` 在 Docker 模拟器上会永久阻塞 — 模拟器固件不支持该命令，需要超时跳过
- `arm.state` 初始化就是 4（停止态），每次连接后都需要执行完整的使能序列

**脚本：** `scripts/loop_motion.py` — 可用 `--sensitivity` 参数控制碰撞灵敏度（真机用）

---

## 2026-02-25 — MVP: Python SDK → Docker Simulator 通路验证通过

**验证结果：**
- 完整通路跑通：`Python script (.venv) → xArm-Python-SDK → TCP 127.0.0.1:30000 → Docker container (850 firmware) → UFactory Studio Web UI`
- 测试脚本 `test_sim_connection.py`：连接、启用、移动到两个位置、读回位置确认，全部成功
- SDK 版本 xarm-python-sdk 1.17.3，固件 v2.4.0

**踩坑记录：**
- Git Bash 在 Windows 上会把 `/bin/bash` 重写为 Windows 路径，Docker 命令里要用双斜杠 `//bin/bash`
- Docker 容器用 `-d` 启动会立即退出，需要在 entrypoint 末尾加 `sleep infinity` 保持运行
- `.venv` 要先加 `.gitignore` 再创建，避免误提交

**结论：** Vibe coding / agent 写代码操控 Docker 模拟器这条路可行，可以继续在此基础上迭代。

---

## 2026-02-25 — Project Setup

- 建立项目 repo，写 Design Brief（三层架构方向），配好文档结构
- 三层架构是工作方向但明确标记为 tentative

**Open questions:**
- VLM 选型（Claude Vision / GPT-4V / other）
- 是否用本地模型做前置过滤
- TouchDesigner 集成时机
