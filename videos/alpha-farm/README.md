# Alpha 牧场

《Alpha 牧场》是一个约三分钟的中文 HyperFrames 项目，介绍 KOL 策略如何经过 AI 识别、以损定仓、双腿入场和安全控制后进入自动交易执行。

## 开发

```bash
npx hyperframes lint .
npx hyperframes inspect .
npx hyperframes preview
```

项目画幅为 1920×1080，时长约 180 秒，视觉规范见 `DESIGN.md`。

## 隐私

真实 KOL 头像、消息截图、私有数据、生成音频和渲染成片均放在 `.gitignore` 覆盖的目录中。提交前只保留脱敏示例数据，不要提交 Telegram 邀请链接、手机号、API 凭证、账户余额或真实资金规模。
