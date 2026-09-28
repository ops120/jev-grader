# 安全说明

## 密钥的处理方式

- JEV API Key 只从 `.env`（或环境变量 `jevkey` / `TYPESAFE_API_KEY`）读取，
  **只在服务端使用**，前端从不接触；`.env` 已被 `.gitignore` 排除。
- 仓库里任何位置都不应有真实密钥。若你怀疑密钥已泄露：
  立即到 https://console.typesafe.ai/keys 吊销并重建，然后检查 git 历史
  （`git log -S "apikey_" --all`）。

## 部署边界

这是一个**本机工具**：服务默认监听 `127.0.0.1:8848`，且会挂载 `data/` 目录
（`/data/...`，用于展示数学卷的试卷原图）。它**没有鉴权、没有多租户隔离**，
因此：

- 不要把它直接暴露到公网；
- 如需局域网共享，请自行加反向代理与访问控制，并知悉 `/api/upload` 会写文件
  到 `data/banks/`、`/api/solve` 会消耗你的 API 配额。

## 报告漏洞

请通过 GitHub Security Advisories（仓库 → Security → Report a vulnerability）
私密报告，不要开公开 issue。我们会尽快确认并修复；修复前请勿公开细节。
