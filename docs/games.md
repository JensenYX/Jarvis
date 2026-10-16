# Games and screen sharing

[English](games.md) · [简体中文](games_zh.md) · [README](../README.md)

## PvZ

Start Venus-Audio and ASR as described in the model guide. Install the web game, then launch the game application:

```bash
jarvis setup pvz
jarvis serve --game pvz
```

Open **http://localhost:8785**. Click **先玩游戏** for a game and text session, or **开启语音陪玩** for voice. Use the tutorial or lawn buttons to enter a level. Try asking about current sun, suitable plants or the next move.

- **给个建议** requests advice.
- **这一步你来** delegates one action.
- **帮我代打** starts continuous play. Voice requests can also specify a duration or the current level.
- **我来** returns control to you.

The browser reports visible state and legal actions. Each submitted move is tied to the current snapshot and a short-lived grant; the game confirms its result. The downloaded fan game uses its upstream personal, noncommercial license.

## Luanti / VoxeLibre

The voxel integration builds the Luanti engine and installs VoxeLibre. On an x86-64 Linux host, install build tools first:

```bash
sudo apt-get install build-essential cmake ninja-build pkg-config patch dpkg-dev
```

```bash
jarvis setup voxel --jobs 8
jarvis games start voxel
```

Setup downloads the supported sources, applies the bridge patches and builds the client and server. The start command launches the display, game-audio service, Jarvis application, server and player client. Keep the speech-model services running for voice interaction.

Open **http://localhost:8795**. Click **先玩游戏** or **开启语音陪玩**, then **进入游戏**. The browser controls the player in the persistent server world.

| Control | Action |
| --- | --- |
| W / A / S / D | Move |
| Mouse | Look around |
| Space / Shift | Jump / sneak |
| Left / right mouse | Dig / place |
| Scroll wheel | Select hotbar item |
| E or I | Inventory |
| Escape | Release the pointer |

Ask about inventory, landmarks, resources or a building task. Player input takes priority over delegated movement. A stopped or interrupted action remains subject to fresh state and authorization before it resumes.

```bash
jarvis games status voxel
jarvis games stop voxel
```

World data lives under `JARVIS_DATA_DIR/local_games/voxel/user/worlds`. Stopping the services preserves the world. The launcher uses display `:91`, game port `30991` and application port `8795`; its processes and logs are recorded under the data directory.

## CS2 and League of Legends

Start the Omni model services, then run:

```bash
jarvis serve --mode omni --game cs2
```

For League of Legends, use `--game lol`. Open **http://localhost:8765**, start a conversation, and share the game window. The browser's chooser determines which window Jarvis receives. Choose game audio when the browser offers it.

Screen frames, game audio and microphone audio remain separate inputs. Jarvis uses causal observations to answer questions and give advice. Claims have validity periods; changing the scene or losing the source invalidates current evidence. Questions about earlier play use retained historical evidence.

Use the commentary selector to choose quiet, brief, balanced or active interaction. **停止共享** stops screen input; **停止说话** stops playback; **结束对话** closes the session.

## Remote access

Forward the application you are using:

```bash
ssh -N -L 8785:127.0.0.1:8785 user@server
```

Use `8795` for VoxeLibre or `8765` for Omni. Open the localhost URL on your own computer so the microphone and screen chooser operate there. A deployment with HTTPS can be opened directly from the client browser.
