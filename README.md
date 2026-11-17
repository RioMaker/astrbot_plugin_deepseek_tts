# DeepSeek TTS

为 AstrBot 接入 DeepSeek 官方 TTS，让可可子使用语音回复。支持海星女声 `stella`、暗潮男声 `tide`，提供配置与试听页面。

- 插件标识：`astrbot_plugin_deepseek_tts`
- 版本：`0.1.0`
- AstrBot：`>=4.29.4,<5`
- 当前支持：QQ / OneBot v11（`aiocqhttp`）。其他平台尚未开放。

## 安装与配置

1. 在 AstrBot 插件管理中，通过文件上传安装本插件 ZIP；也可以把插件目录放到 `data/plugins/astrbot_plugin_deepseek_tts` 并重载。
2. 打开 **DeepSeek TTS → 插件详情 → 语音设置与试听**（新版 WebUI 称为“可视化视图”）。原生插件配置表单也可编辑相同设置。
3. 填写 **DeepSeek 官方 API Key**，选择音色并保存。也可以在 **AstrBot 进程** 中设置 `DEEPSEEK_API_KEY`；插件配置中的非空 Key 优先使用。只在另一终端设置环境变量不会影响已经启动的 AstrBot。
4. 在页面填写一段短文，点击“生成试听”。试听使用当前选中的音色和已保存的 Key，不会自动修改默认音色。
5. 在可可子人格的工具权限中允许 `deepseek_tts_speak`，并确保当前配置文件、会话启用了本插件。对她说“用语音介绍一下自己”。

配置页不会回传已保存的密钥。密钥输入框留空会保留原值，勾选“清除已保存的密钥”才清除配置值；环境变量不会因此被删除。密钥保存在 AstrBot 的插件配置体系中，不在插件源码目录中。

默认使用海星女声，启用插件，**按需语音、纯语音输出**。不会默认把所有 AI 回复转换为付费语音。选择“自动朗读 AI 回复”并保存后，可开启自动转换。

## 回复方式

| 方式 | 行为 |
| --- | --- |
| AI 语音工具 | AI 调用 `deepseek_tts_speak(text)`，发送到当前会话。适用于用户要求语音，也适用于自定义唤醒后的工具式回复。 |
| 自动朗读普通 AI 回复 | 在消息发送前合成语音，可选纯语音或文字加语音，图片等其他消息组件保留。 |
| 自动朗读流式回复 | 完整文字已经发送后追加一段语音，不重复发送文字；无法撤回已发送的流式文字。 |
| 指令 | `/ds语音 内容`（别名 `/dstts 内容`）或 `/ds语音试听`。 |

自动朗读只处理 AI 正常回复，不朗读插件指令结果、模型错误或未完成的流式片段。自定义唤醒通过原生发送工具直接发送的文本不经过普通回复装饰流程，请在这种模式下使用本插件的语音工具。插件不会接管其他工具或修改可可子的人格内容。

同一轮工具调用最多处理一次语音，工具已处理过后不会再自动合成。工具的成功结果明确区分 `delivery: audio` 与 `delivery: text`，避免模型把文字回退描述成语音成功。模型仍需遵守工具说明，避免自行重复回复。

如果该会话已经启用 AstrBot 内置 TTS，插件跳过普通回复的自动转换，避免两个 TTS 同时处理；本插件的独立指令和 AI 工具仍可用。建议只启用一种自动 TTS。需要纯语音的普通自动回复时，请关闭 AstrBot 流式输出。

## 配置项

| 项目 | 默认值 | 说明 |
| --- | --- | --- |
| 启用 | 开启 | 控制会话中的语音功能；管理页面仍可试听。 |
| API Key | 空 | 留空读取 AstrBot 的 `DEEPSEEK_API_KEY`。 |
| 默认音色 | `stella` | 海星女声 / 暗潮男声。 |
| 自动朗读 | 关闭 | 开启后朗读普通 AI 回复。 |
| 输出方式 | `voice` | `voice` 纯语音，`both` 文字加语音。 |
| 字数上限 | 500 | 1～2000 个 Unicode 字符；自动回复超过上限时保留完整文字。 |
| 超时 | 60 秒 | 10～180 秒，包含整个音频下载。 |
| 同会话间隔 | 3 秒 | 0～300 秒；从请求开始计时，失败请求也计入间隔。 |
| Markdown 过滤 | 开启 | 请求 DeepSeek 去除 Markdown 格式后朗读。 |
| 允许的 QQ 群 | 空列表 | 空列表允许所有启用插件的群；否则只允许填写的群号。 |
| 允许私聊 | 开启 | 独立于群白名单。 |

最多同时合成两段语音，繁忙时不无限排队。试听最多 100 字，并受配置的字数上限约束；试听属于真实调用，会使用官方账户额度。请求不自动重试，避免在服务响应不明时重复计费。

普通回复合成失败时保留原文字；指令和 AI 工具在合成或语音发送失败时尝试以文字发送。协议端未确认的发送无法保证对方实际收听，需在真实 QQ 环境中验证。

## 音频与数据

仅使用固定官方地址 `https://api.deepseek.com/v1/tools/tts`，发送 `text`、`voice_id`、`format: wav`、`filter_markdown`，不接受第三方代理地址。请求不会携带聊天历史，也不会发送到其他会话。

插件校验响应类型、PCM 数据和已知文件长度，处理 HTTP 传输中断，并将官方 WAV 流中的未知长度文件头整理成标准 WAV。音频样本不变，无需额外安装 FFmpeg。官方当前接口没有语速调节或自定义声音克隆参数。

会话音频保存在 `data/plugin_data/astrbot_plugin_deepseek_tts/audio/`。新文件使用随机名；每次保存会清理超过 24 小时的本插件 WAV，并在缓存超过 256 MiB 时清理较旧文件，最近 10 分钟的文件保留以供平台读取。清理仅匹配本插件的随机文件名。页面试听音频不写入该缓存，由浏览器临时保存，关闭页面后释放。

## 本地开发与验证

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/python.exe tools/check_ui.py
```

`tools/check_ui.py` 使用本机 Chrome 和模拟 AstrBot bridge 验证页面，包括音色切换、试听音频、保存、错误恢复、暗色主题与手机布局，不调用真实 API。可通过 `--browser` 指定 Chromium 浏览器路径。

真实 API 冒烟测试（会计费，必须明确配置环境变量）：

```powershell
.venv/Scripts/python.exe tools/smoke.py --voice both --output artifacts/live
```

2026-11-17 使用本插件请求代码实测：海星、暗潮均返回 HTTP 200，分别生成 5.32 秒和 6.04 秒的 24 kHz WAV。本地测试不等于真实 AstrBot / OneBot / QQ 已部署验证。完整测试范围和当前限制见 [CHANGELOG.md](CHANGELOG.md)。

## 官方参考

- [DeepSeek TTS 指南](https://api-docs.deepseek.com/zh-cn/guides/tts/)
- [DeepSeek TTS API](https://api-docs.deepseek.com/zh-cn/api/tts/)
- [AstrBot 消息事件与发送前钩子](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
- [AstrBot 插件可视化视图与 Bridge](https://docs.astrbot.app/dev/star/guides/plugin-pages.html)
- [AstrBot 插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)

配置页面保留 `pages/settings` 目录与 `AstrBotPluginPage` 别名，同时支持新版 `AstrBotPluginView`，与目标 AstrBot 4.29.4 兼容。
