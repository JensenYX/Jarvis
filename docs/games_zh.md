# 游戏与屏幕分享

[English](games.md) · [简体中文](games_zh.md) · [README](../README_zh.md)

## PvZ

按模型指南启动 Venus-Audio 和 ASR，安装网页游戏并启动应用：

```bash
jarvis setup pvz
jarvis serve --game pvz
```

打开 **http://localhost:8785**。“先玩游戏”进入游戏与文字会话，“开启语音陪玩”启用语音。通过新手关卡或五路草坪按钮进入关卡，可以询问当前阳光、适合的植物或下一步操作。

- “给个建议”请求建议。
- “这一步你来”委派一次操作。
- “帮我代打”开始持续代打，也可以通过语音指定时长或当前关卡。
- “我来”收回控制。

浏览器报告可见状态和合法动作。每次提交的操作绑定当前快照与短时授权，由游戏确认结果。下载的网页同人游戏遵循上游个人非商业许可证。

## Luanti / VoxeLibre

体素集成会构建 Luanti 引擎并安装 VoxeLibre。在 x86-64 Linux 上先安装构建工具：

```bash
sudo apt-get install build-essential cmake ninja-build pkg-config patch dpkg-dev
```

```bash
jarvis setup voxel --jobs 8
jarvis games start voxel
```

安装器下载对应源码、应用 bridge 补丁并构建客户端和服务端。启动命令依次启动显示服务、游戏音频服务、Jarvis 应用、游戏服务端和玩家客户端。使用语音交互时，保持语音模型服务运行。

打开 **http://localhost:8795**，点击“先玩游戏”或“开启语音陪玩”，再点击“进入游戏”。浏览器控制持久世界中的玩家。

| 按键 | 操作 |
| --- | --- |
| W / A / S / D | 移动 |
| 鼠标 | 转向 |
| 空格 / Shift | 跳跃 / 潜行 |
| 鼠标左键 / 右键 | 采集 / 放置 |
| 滚轮 | 切换快捷栏物品 |
| E 或 I | 背包 |
| Escape | 释放鼠标 |

可以询问背包、地标、资源或建造任务。玩家输入优先于委派的移动；停止或打断后的操作，需要重新核对当前状态与授权才能继续。

```bash
jarvis games status voxel
jarvis games stop voxel
```

世界保存在 `JARVIS_DATA_DIR/local_games/voxel/user/worlds`，停止服务会保留世界。启动器使用显示 `:91`、游戏端口 `30991` 和应用端口 `8795`，进程记录与日志保存在数据目录中。

## CS2 与英雄联盟

启动 Omni 模型服务后运行：

```bash
jarvis serve --mode omni --game cs2
```

英雄联盟使用 `--game lol`。打开 **http://localhost:8765**，开始对话并分享游戏窗口。浏览器的分享选择器决定 Jarvis 接收哪个窗口；浏览器提供选项时，可同时分享游戏音频。

屏幕帧、游戏音频和麦克风分别输入。Jarvis 根据因果观察回答问题、提供建议。视觉事实带有效期，场景切换或来源丢失会使当前证据失效；关于过去游戏过程的问题使用保留的历史证据。

通过陪玩方式选择安静、简短、适量或主动交流。“停止共享”结束屏幕输入，“停止说话”中断播放，“结束对话”关闭会话。

## 远程访问

转发当前应用的端口：

```bash
ssh -N -L 8785:127.0.0.1:8785 user@server
```

VoxeLibre 使用 `8795`，Omni 使用 `8765`。在自己的电脑浏览器中打开 localhost 地址，麦克风和窗口选择器就在本机工作。使用 HTTPS 部署时，也可以直接从客户端浏览器访问。
