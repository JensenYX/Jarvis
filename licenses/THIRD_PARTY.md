# Third-party components

The original Jarvis harness is licensed under Apache-2.0. The components below retain their own licenses.

| Component | How it is used | License |
| --- | --- | --- |
| Realtime-Venus | Downloaded audio and audio-visual front models | Apache-2.0 |
| MiniCPM-o | Downloaded duplex speech model and runtime | Upstream code and model licenses in the downloaded repository |
| NVIDIA NemotronLabs VoiceChat | Downloaded speech model and NVIDIA NeMo Speech runtime | OpenMDW-1.1 for model weights; upstream runtime license |
| JEV-Omni | Downloaded multimodal judgment model and support code | Apache-2.0 |
| Gemma | Downloaded visual description model | Upstream model license |
| Qwen3-ASR | Downloaded speech recognition model | Apache-2.0 |
| TypeSafe SDK | Hosted JEV client | Upstream SDK license |
| Luanti | Downloaded and built voxel game engine | LGPL-2.1-or-later |
| VoxeLibre | Downloaded voxel survival game | GPL-3.0-or-later for game code; artwork and audio retain their upstream licenses |
| Jarvis VoxeLibre bridge | Distributed Lua game integration | GPL-3.0-or-later |
| Luanti patches | Distributed changes to the client | LGPL-2.1-or-later |
| VoxeLibre patch | Distributed change to minecart handling | GPL-3.0-or-later |
| Plants-vs-Zombies-MODDED/game | Downloaded web game | Personal, noncommercial use under the upstream license |

Game source code and license files remain in the installation directory. The repository contains integration code and patches; installation obtains the game engines and their assets from the publishers. The PvZ adapter runs the web fan game. CS2 and League of Legends use the user's shared game window.

Full GPL-3.0 and LGPL-2.1 texts are included in this directory. Model and game downloads also include the publisher's license files.
