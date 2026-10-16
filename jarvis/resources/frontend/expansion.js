import { send, clearGameAudio, renderResult } from "./app.js";
import { VoxelPanel } from "./games/voxel/panel.mjs";
const $ = (id) => document.getElementById(id),
  frame = $("gameFrame");
const savedVolume = localStorage.getItem("companion.pvz.volume");
$("gameVolume").value =
  savedVolume === null
    ? 20
    : Math.round(Math.max(0, Math.min(1, Number(savedVolume))) * 100);
$("volumeValue").textContent = $("gameVolume").value + "%";
let selected = null,
  snapshot = null,
  port = null,
  connected = false,
  frameReady = false,
  capabilities = null,
  textSocket = null,
  activeBinding = null;
let audioDeadline = Infinity,
  audioResponse = null;
let voxelPanel = null,
  voxelSnapshot = null,
  voxelProjectGeneration = null;
let localSnapshot = null,
  localReceived = 0;
let latestSnapshot = null,
  snapshotFlight = null,
  criticalFlights = new Map();
let adviceBinding = null,
  adviceId = null;
let autoplayState = null;
function renderAutoplay() {
  if (!autoplayState) return;
  const scope =
    autoplayState.scope === "timed"
      ? `剩余 ${Math.ceil(autoplayState.remaining_ms / 1000)} 秒`
      : autoplayState.scope === "level"
        ? "到本关结束"
        : autoplayState.scope === "task"
          ? "完成你指定的操作"
          : "直到你说\u201C我自己来\u201D";
  $("gameControl").textContent = `正在代打 · ${scope}`;
}
function sendBridge(m) {
  const snapshot = m.type === "snapshot" && !m.payload.nonce;
  if (snapshot && snapshotFlight) {
    latestSnapshot = m;
    return;
  }
  if (!snapshot && criticalFlights.size >= 32) {
    port?.postMessage({ type: "transport_disconnected" });
    port?.close();
    port = null;
    $("gameStatus").textContent = "游戏通信拥堵\uFF0C自动操作已停止";
    return;
  }
  if (snapshot) snapshotFlight = m.message_id;
  else criticalFlights.set(m.message_id, performance.now());
  transmit("game.bridge.receive", m);
}
function transmit(type, payload) {
  if (textSocket?.readyState === 1)
    textSocket.send(
      JSON.stringify({
        type,
        payload,
      }),
    );
  else send(type, payload);
}
function command(action, extra = {}) {
  transmit("game.command", {
    action,
    ...extra,
    voxel_anchor: voxelSnapshot
      ? Object.fromEntries(
          [
            "game_instance_id",
            "player_life_id",
            "bridge_epoch",
            "snapshot_seq",
            "control_epoch",
            "candidate_set_id",
          ].map((k) => [k, voxelSnapshot[k]]),
        )
      : null,
    anchor: snapshot
      ? {
          snapshot_seq: snapshot.snapshot_seq,
          decision_id: snapshot.decision_id,
          candidate_set_id: snapshot.candidate_set_id,
          control_epoch: snapshot.control_epoch,
        }
      : null,
    game_instance_id: snapshot?.game_instance_id,
    bridge_epoch: snapshot?.bridge_epoch,
    active_game_epoch: selected?.active_game_epoch,
    advice_id:
      action === "play_once" && adviceBinding && valid(adviceBinding, false)
        ? adviceId
        : null,
  });
}
function valid(binding, forAudio = true) {
  if (!binding) return true;
  if (binding.voxel) {
    const s = voxelSnapshot;
    if (
      selected?.game_id !== "voxel" ||
      !s ||
      ["world_id", "game_instance_id", "player_life_id", "bridge_epoch"].some(
        (k) => binding[k] !== s[k],
      )
    )
      return false;
    if (
      binding.project_generation != null &&
      voxelProjectGeneration != null &&
      binding.project_generation !== voxelProjectGeneration
    )
      return false;
    if (["game", "history", "receipt", "analysis"].includes(binding.scope))
      return true;
    if (forAudio && performance.now() >= audioDeadline) return false;
    if (binding.field)
      return (
        JSON.stringify(binding.value) ===
        JSON.stringify(s.view.self[binding.field])
      );
    if (binding.scope === "advice")
      return (
        binding.hp === s.view.self.hp &&
        JSON.stringify(binding.hunger) === JSON.stringify(s.view.self.hunger) &&
        binding.day_phase === s.view.environment.day_phase &&
        JSON.stringify(binding.inventory) ===
          JSON.stringify(s.view.self.inventory_summary)
      );
    return true;
  }
  const current = selected?.game_id === "pvz_web" ? localSnapshot : snapshot;
  if (!(
    selected &&
    current &&
    binding.active_game_epoch === selected.active_game_epoch &&
    binding.game_instance_id === current.game_instance_id &&
    binding.bridge_epoch === current.bridge_epoch &&
    binding.game_id === selected.game_id
  ))
    return false;
  if (["history", "receipt", "game"].includes(binding.scope)) return true;
  if (binding.scope === "interface")
    return (
      current.phase === binding.phase &&
      (current.view.ui_stage || "unknown") === binding.ui_stage
    );
  if (
    selected?.game_id === "pvz_web" &&
    performance.now() - localReceived > 1500
  )
    return false;
  if (
    !current.focused ||
    current.paused ||
    (forAudio && performance.now() >= audioDeadline)
  )
    return false;
  if (
    current.observed_at_host_mono_ms - binding.host_observed_at_ms >
    binding.ttl_ms
  )
    return false;
  if (binding.field) return current.view[binding.field] === binding.value;
  if (binding.position_key) {
    const v = current.view,
      key =
        v.game !== "pvz_web"
          ? current.decision_id
          : {
              phase: current.phase,
              sun: v.sun,
              seeds: v.seed_bank.map((c) => [c.seed_id, c.ready, c.cost]),
              plants: v.plants.map((p) => [p.entity_id, p.row, p.col, p.state]),
              zombies: v.zombies.map((z) => [
                z.entity_id,
                z.row,
                z.zone,
                z.state,
              ]),
            };
    return JSON.stringify(binding.position_key) === JSON.stringify(key);
  }
  if (current.view.game === "pvz_web" && binding.action?.facts.row) {
    const f = binding.action.facts,
      v = current.view;
    if (current.phase !== "playing" || !v.normal_rules || v.auto_collect_sun)
      return false;
    const lane = v.zombies
      .filter((z) => z.row === f.row)
      .map((z) => [z.entity_id, z.zone, z.state]);
    const plants = v.plants
      .filter((p) => p.row === f.row)
      .map((p) => [p.entity_id, p.col, p.state]);
    const cell = v.plants
      .filter((p) => p.row === f.row && p.col === f.col)
      .map((p) => p.entity_id);
    if (
      binding.zombie_side &&
      JSON.stringify(
        v.zombies
          .filter((z) => z.row === f.row)
          .map((z) => [z.entity_id, z.x >= v.column_x[f.col - 1]]),
      ) !== JSON.stringify(binding.zombie_side)
    )
      return false;
    return (
      JSON.stringify(lane) === JSON.stringify(binding.lane_zombies) &&
      JSON.stringify(plants) === JSON.stringify(binding.lane_plants || []) &&
      JSON.stringify(cell) === JSON.stringify(binding.cell_plants) &&
      v.seed_bank.some((c) => c.seed_id === f.seed_id && c.ready) &&
      v.sun >= f.cost
    );
  }
  if (current.view.game === "pvz_web" && binding.action?.kind === "COLLECT_SUN")
    return (
      current.phase === "playing" &&
      current.view.suns.some(
        (s) => s.entity_id === binding.action.facts.target_id,
      )
    );
  return binding.decision_id === current.decision_id;
}
function handshake() {
  if (!frameReady || !selected || !["pvz_web"].includes(selected.game_id))
    return;
  const channel = new MessageChannel();
  port?.close();
  port = channel.port1;
  port.onmessage = (e) => {
    const m = e.data;
    if (
      !m ||
      m.protocol_version !== 1 ||
      m.session_id !== selected.session_id ||
      m.active_game_epoch !== selected.active_game_epoch
    )
      return;
    if (JSON.stringify(m).length > 1048576) return;
    if (m.type === "snapshot") {
      localSnapshot = {
        ...m.payload,
        game_instance_id: m.game_instance_id,
        bridge_epoch: m.bridge_epoch,
      };
      localReceived = performance.now();
      if (activeBinding && !valid(activeBinding)) {
        clearGameAudio();
        activeBinding = null;
      }
      if (adviceBinding && !valid(adviceBinding, false)) {
        $("advice").textContent = "局面已变化\uFF0C可以再问我下一步\u3002";
        adviceBinding = null;
        adviceId = null;
      }
    }
    sendBridge(m);
  };
  frame.contentWindow.postMessage(
    {
      type: "game.bind",
      session_id: selected.session_id,
      game_id: selected.game_id,
      active_game_epoch: selected.active_game_epoch,
    },
    location.origin,
    [channel.port2],
  );
  if (selected.game_id === "pvz_web")
    port.postMessage({
      type: "local_volume",
      value: Number($("gameVolume").value) / 100,
    });
}
window.addEventListener("message", (e) => {
  if (
    e.origin !== location.origin ||
    e.source !== frame.contentWindow ||
    !["game.ready"].includes(e.data?.type)
  )
    return;
  frameReady = true;
  handshake();
});
function receive(e) {
  const m = e.detail;
  if (m.type === "session") {
    connected = true;
    transmit("game.select", { game_id: $("gameChoice").value });
  }
  if (m.type === "game.selected") {
    voxelPanel?.close();
    voxelPanel = null;
    voxelSnapshot = null;
    voxelProjectGeneration = null;
    autoplayState = null;
    $("gameVolumeControl").hidden = !["pvz_web", "voxel"].includes(m.game_id);
    selected = m;
    snapshot = null;
    localSnapshot = null;
    capabilities = null;
    if (activeBinding) clearGameAudio();
    activeBinding = null;
    $("playOnce").textContent =
      m.game_id === "voxel" ? "这里放一块" : "这一步你来";
    if (m.game_id !== "voxel")
      $("gameState").closest("details").querySelector("summary").textContent =
        "当前公开局面";
    $("stageHint").textContent = "";
    latestSnapshot = null;
    snapshotFlight = null;
    criticalFlights.clear();
    adviceBinding = null;
    adviceId = null;
    $("advice").textContent = "可以问我当前资源和下一步\u3002";
    $("gameControl").textContent = "手动操作";
    $("startLawn").hidden = m.game_id !== "pvz_web";
    $("startTutorial").hidden = m.game_id !== "pvz_web";
    frame.style.height = "700px";
    $("autoplay").hidden = m.game_id !== "pvz_web";
    for (const id of [
      "askAdvice",
      "playOnce",
      "autoplay",
      "reclaim",
      "cancelAnalysis",
    ])
      $(id).disabled = true;
    frame.hidden = m.game_id === "voxel";
    if (m.game_id === "voxel") {
      port?.close();
      port = null;
      frame.src = "about:blank";
      voxelPanel = new VoxelPanel(frame.parentElement, { command });
      voxelPanel.connect(m.pairing_token);
      voxelPanel.setVolume(Number($("gameVolume").value) / 100);
      $("gameStatus").textContent = "VoxeLibre 开源生存沙盒";
      $("textInput").placeholder = "我手里还剩多少材料\uFF1F下一步做什么\uFF1F";
      $("advice").textContent =
        "点击\u201C进入游戏\u201D后就能移动和采集\uFF0C可以随时问我局面或记住一个地点\u3002";
      $("playOnce").textContent = "这里放一块";
      $("gameState").closest("details").querySelector("summary").textContent =
        "模型看到了什么";
      return;
    }
    if (m.game_id === "pvz_web") {
      frameReady = false;
      frame.src = "/games/pvz_web/game/index.html";
    } else {
      frame.src = "about:blank";
      port?.close();
      port = null;
    }
    $("gameStatus").textContent = "正在连接游戏";
  }
  if (m.type === "game.bridge.send") port?.postMessage(m.message);
  if (
    m.type === "game.bridge.ack" &&
    selected?.active_game_epoch === m.active_game_epoch
  ) {
    criticalFlights.delete(m.message_id);
    if (snapshotFlight === m.message_id) {
      snapshotFlight = null;
      const next = latestSnapshot;
      latestSnapshot = null;
      if (next) sendBridge(next);
    }
  }
  if (m.type === "audio") {
    const deadline =
      performance.now() + (m.remote_binding?.remaining_ms ?? Infinity);
    audioDeadline =
      m.response_id === audioResponse
        ? Math.min(audioDeadline, deadline)
        : deadline;
    audioResponse = m.response_id;
    if (!valid(m.remote_binding)) {
      e.preventDefault?.();
      return;
    }
    activeBinding = m.remote_binding;
  }
  if (m.type !== "event") return;
  const { type, payload: p } = m.event;
  if (selected?.game_id === "voxel") {
    if (type === "voxel.snapshot") {
      voxelSnapshot = p.snapshot;
      voxelPanel?.update(p.snapshot);
      $("gameState").textContent = JSON.stringify(p.snapshot.view, null, 2);
      $("gameStatus").textContent =
        p.snapshot.phase === "playing"
          ? "VoxeLibre \xB7 生存世界已连接"
          : "VoxeLibre \xB7 " + p.snapshot.phase;
      for (const id of ["askAdvice", "reclaim", "cancelAnalysis"])
        $(id).disabled = false;
      $("playOnce").disabled = !p.snapshot.action_candidates.length;
      if (activeBinding && !valid(activeBinding)) {
        clearGameAudio();
        activeBinding = null;
      }
    }
    if (type === "voxel.project") {
      voxelProjectGeneration = p.generation;
      voxelPanel?.updateProject(p.project);
      $("gameControl").textContent = p.authorization_active
        ? "正在按方案建造"
        : "手动操作 / 讨论设计";
    }
    if (
      type === "voxel.project.progress" &&
      voxelPanel?.project?.id === p.project_id
    )
      voxelPanel.root.querySelector("[data-project-status]").textContent =
        p.stage;
    if (type === "voxel.task") {
      voxelPanel?.updateTask(p.task);
      $("gameControl").textContent = p.active ? "正在执行你的任务" : "手动操作";
    }
    if (type === "voxel.task.step" && p.result.status === "SUCCEEDED")
      $("lastAction").textContent =
        {
          DIG: "已拆除一个目标方块",
          CRAFT: "已完成一次合成",
          ATTACK: "已执行一次攻击",
          MOVE: "已调整位置",
          SELECT: "已换用工具",
        }[p.action.kind] || "已执行";
    if (type === "voxel.project.step" && p.result.status === "SUCCEEDED")
      $("lastAction").textContent = [
        "PROJECT_PLACE",
        "PROJECT_JUMP_PLACE",
      ].includes(p.action.kind)
        ? "已放置一块\uFF0C继续建造\u3002"
        : "正在调整站位或材料\u3002";
    if (type === "voxel.control")
      $("gameControl").textContent =
        p.mode === "MANUAL" ? "手动操作" : "正在确认这一次放置";
    if (type === "voxel.action.completed")
      $("lastAction").textContent =
        p.status === "SUCCEEDED"
          ? "已放置一块\uFF0C接下来由你操作\u3002"
          : "本次操作\uFF1A" + p.status;
    if (type === "voxel.advice") {
      $("advice").textContent = p.text;
      transmit("game.command", {
        action: "advice_seen",
        advice_id: p.advice_id,
      });
    }
    if (type === "voxel.goal")
      $("stageHint").textContent = "当前目标\uFF1A" + p.text;
    if (type === "voxel.spoken") {
      $("transcript").textContent = p.text;
      if (["game_help", "game_advice", "game_project"].includes(p.purpose))
        $("advice").textContent = p.text;
    }
  }
  if (type === "bridge.connected") {
    capabilities = p.capabilities;
    $("gameStatus").textContent = "游戏已连接";
  }
  if (type === "bridge.snapshot") {
    snapshot = p;
    $("gameState").textContent = JSON.stringify(p.view, null, 2);
    autoplayState = p.autoplay || null;
    if (activeBinding && !valid(activeBinding)) {
      clearGameAudio();
      activeBinding = null;
    }
    $("askAdvice").disabled = false;
    $("playOnce").disabled =
      !!autoplayState ||
      capabilities?.mode !== "guarded_control" ||
      !p.awaiting_action;
    $("autoplay").disabled =
      !!autoplayState || !capabilities?.auto_continue || !p.awaiting_action;
    $("reclaim").disabled = false;
    $("cancelAnalysis").disabled = false;
    if (!p.awaiting_action)
      $("gameControl").textContent =
        p.view.normal_rules === false || p.view.auto_collect_sun
          ? "当前辅助选项下仅提供观察与建议"
          : p.paused
            ? "游戏已暂停"
            : p.phase === "unsupported"
              ? "当前场景由你操作"
              : "手动操作";
    if (p.view.game === "pvz_web") {
      const stage = p.view.ui_stage;
      const stages = {
        title: "先点游戏画面中央进入菜单",
        menu: "点击 Adventure 或\u201C新手关卡\u201D开始",
        select_cards: "选好植物卡\uFF0C再点游戏里的 Start",
        preparing: "正在准备草坪",
        playing: "陪玩已连接 \xB7 可以问建议或交给我代打",
        paused: "游戏暂停 \xB7 可以继续问我",
        ended: "本局已结束",
        unsupported: "当前关卡可手动玩\uFF0C代打暂不可用",
      };
      $("gameStatus").textContent = stages[stage] || "陪玩已连接";
      $("stageHint").textContent =
        stage === "select_cards"
          ? "可以问\u201C选什么植物\u201D\uFF0C我会推荐阵容\u3002"
          : stage === "preparing"
            ? "草坪准备中\uFF0C马上开始\u3002"
            : stage === "playing"
              ? "可以说\u201C帮我代打\u201D\u201C帮我代打一分钟\u201D或\u201C帮我代打这一关\u201D\uFF1B说\u201C我自己来\u201D收回\u3002"
              : stage === "menu"
                ? "可直接从 Adventure 开始新手教学\uFF1B全程可问我怎么操作\u3002"
                : "";
    }
    renderAutoplay();
  }
  if (type === "bridge.disconnected") {
    autoplayState = null;
    $("autoplay").disabled = true;
    $("gameStatus").textContent = "游戏连接中断\uFF0C手动操作仍可继续";
    snapshot = null;
    if (activeBinding) clearGameAudio();
    activeBinding = null;
    $("playOnce").disabled = true;
  }
  if (type === "remote.advice" && valid(p.binding, false)) {
    adviceBinding = p.binding;
    adviceId = p.advice_id;
    $("advice").textContent = p.text;
    transmit("remote.advice.seen", { advice_id: p.advice_id });
  }
  if (type === "remote.help" && valid(p.binding, false)) {
    adviceBinding = null;
    adviceId = null;
    $("advice").textContent = p.text;
  }
  if (type === "assistant.text") $("transcript").textContent = p.text;
  if (type === "asr.context_echo_suppressed") $("transcript").textContent = "";
  if (type === "bridge.action.submitting" && !autoplayState)
    $("gameControl").textContent = "等待游戏确认这一步";
  if (type === "bridge.action" && !autoplayState)
    $("gameControl").textContent =
      p.status === "CONFIRMED_REQUIRES_HUMAN_CONTINUATION"
        ? "这一步已执行\uFF0C请继续完成游戏中的选择"
        : p.status === "CONFIRMED"
          ? "已完成\uFF0C交还控制"
          : p.status === "UNKNOWN"
            ? "结果不明\uFF0C自动操作已停止"
            : p.status === "ACCEPTED"
              ? "游戏已接收\uFF0C等待结果确认"
              : "这一步未执行";
  if (type === "remote.action.completed")
    $("gameControl").textContent = "已完成\uFF1A" + p.text;
  if (type === "remote.autoplay.started") {
    autoplayState = {
      autoplay_id: p.autoplay_id,
      scope: p.scope,
      remaining_ms: p.duration_ms,
    };
    $("gameControl").textContent = p.text;
  }
  if (type === "remote.autoplay.progress") {
    $("lastAction").textContent = "刚刚\uFF1A" + p.text;
    renderAutoplay();
  }
  if (type === "remote.autoplay.stopped") {
    autoplayState = null;
    $("gameControl").textContent = p.text;
  }
  if (type === "remote.command.started")
    $("lastAction").textContent = "正在执行\uFF1A" + p.command.request;
  if (type === "remote.command.finished")
    $("lastAction").textContent =
      p.status === "complete"
        ? "指定操作已完成"
        : `已完成 ${p.done}/${p.total}，剩余目标暂不可操作`;
  if (type === "bridge.revoke_requested")
    $("gameControl").textContent = "撤销请求已发送";
  if (type === "bridge.revoked")
    $("gameControl").textContent = "游戏端已确认收回";
  if (type === "remote.action.failed") {
    const reasons = {
      stale_button_context: "局面已变化\uFF0C请重新点击这一手",
      referenced_advice_expired: "刚才那条建议已失效\uFF0C请先重新取得建议",
      one_action_inflight: "正在处理这一手",
      decision_busy: "正在思考\uFF0C请稍后再授权",
      wait: "现在先观察\uFF0C控制仍在你手里",
      abstain: "暂时没有可靠的操作建议",
      rejected_or_unavailable: "这一步已失效\uFF0C没有执行",
      fresh_state_unavailable: "未取得当前局面\uFF0C请检查连接",
    };
    $("gameControl").textContent = reasons[p.reason] || "这一步没有执行";
  }
  if (type === "remote.advice.failed")
    $("advice").textContent = "这次没有取得可靠建议\uFF0C你可以继续操作\u3002";
  if (type === "history.cleared") {
    $("advice").textContent = "历史已清空";
  }
  if (
    textSocket &&
    [
      "task.queued",
      "task.started",
      "task.completed",
      "task.failed",
      "task.restored",
    ].includes(type)
  )
    renderResult(p, transmit);
}
window.addEventListener("duplex.message", receive);
$("selectGame").onclick = () => {
  if (connected) {
    port?.postMessage({ type: "transport_disconnected" });
    transmit("game.select", { game_id: $("gameChoice").value });
  }
};
function activeCommand(action) {}
$("askAdvice").onclick = () => activeCommand("ask_move");
$("playOnce").onclick = () => activeCommand("play_once");
$("autoplay").onclick = () => activeCommand("autoplay");
$("reclaim").onclick = () => {
  if (selected?.game_id === "pvz_web")
    port?.postMessage({ type: "local_reclaim" });
  activeCommand("reclaim");
};
$("cancelAnalysis").onclick = () => activeCommand("cancel_analysis");
$("startLawn").onclick = () => {
  if (selected?.game_id === "pvz_web")
    port?.postMessage({ type: "local_start_lawn" });
};
$("startTutorial").onclick = () => {
  if (selected?.game_id === "pvz_web")
    port?.postMessage({ type: "local_start_tutorial" });
};
$("gameVolume").oninput = () => {
  const value = Number($("gameVolume").value) / 100;
  localStorage.setItem("companion.pvz.volume", String(value));
  $("volumeValue").textContent = $("gameVolume").value + "%";
  if (selected?.game_id === "pvz_web")
    port?.postMessage({
      type: "local_volume",
      value,
    });
  if (selected?.game_id === "voxel") voxelPanel?.setVolume(value);
};
$("proactive").onchange = () =>
  transmit("game.command", {
    action: "proactive",
    enabled: $("proactive").checked,
  });
$("textForm").onsubmit = (e) => {
  e.preventDefault();
  transmit("game.command", {
    action: "text",
    text: $("textInput").value,
  });
  $("textInput").value = "";
};
$("textConnect").onclick = () => {
  if (textSocket) {
    textSocket.close();
    return;
  }
  textSocket = new WebSocket(
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/games/text`,
  );
  textSocket.onopen = () =>
    textSocket.send(
      JSON.stringify({
        type: "start",
        user_id: localStorage.getItem("duplex-user-id") || "development",
      }),
    );
  textSocket.onmessage = (e) => receive({ detail: JSON.parse(e.data) });
  textSocket.onclose = () => {
    textSocket = null;
    connected = false;
    $("connect").disabled = false;
    $("textConnect").textContent = "先玩游戏";
    window.dispatchEvent(new CustomEvent("duplex.closed"));
  };
  $("connect").disabled = true;
  $("textConnect").textContent = "结束游戏";
};
$("connect").addEventListener("click", () => {
  $("textConnect").disabled = true;
});
$("stop").addEventListener("click", () => {
  if (textSocket) transmit("stop_speaking", {});
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) port?.postMessage({ type: "local_reclaim" });
});
window.addEventListener("blur", () => {
  if (!document.hasFocus()) port?.postMessage({ type: "local_reclaim" });
});
setInterval(() => {
  if (activeBinding && !valid(activeBinding)) {
    clearGameAudio();
    activeBinding = null;
  }
}, 200);
