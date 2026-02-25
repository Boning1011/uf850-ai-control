# Devlog

Development timeline — newest first. Record milestone results and verified conclusions, not process noise.

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
