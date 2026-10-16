import { ControlGuard } from '../control-guard.mjs';
import { boardGeometry, supportsLawn } from './scene.mjs';
const w = window,
  revision = 'pvzm-277c1bf-bridge1',
  id = () => crypto.randomUUID();
const names = {
  oSunFlower: ['向日葵', 'economy'],
  oPeashooter: ['豌豆射手', 'attack'],
  oWallNut: ['坚果', 'block'],
  oPotatoMine: ['土豆地雷', 'instant'],
  oSnowPea: ['寒冰射手', 'attack'],
  oRepeater: ['双发射手', 'attack'],
  oCherryBomb: ['樱桃炸弹', 'instant'],
  oSquash: ['倭瓜', 'instant'],
  oChomper: ['大嘴花', 'attack'],
};

const effects = {
  oSunFlower: '产生阳光；不攻击。',
  oPeashooter: '向右攻击同一路僵尸，不能打身后的僵尸。',
  oWallNut: '阻挡接触的僵尸，不造成伤害；僵尸已经越过的格子不能拦住它。',
  oPotatoMine: '需要约15秒准备，准备期间不能攻击；就绪后接触引爆，单次使用。',
  oSnowPea: '向右攻击同一路僵尸并减速。',
  oRepeater: '向右连续发射两颗豌豆。',
  oCherryBomb: '种下后爆炸，清除附近三路范围的僵尸；单次使用。',
  oSquash: '压扁附近僵尸，单次使用。',
  oChomper: '吞噬近距离面前的一只僵尸，之后长时间咀嚼；不是持续远程输出。',
};
const zombieNames = {
  oZombie: '普通僵尸',
  oZombie2: '普通僵尸',
  oZombie3: '普通僵尸',
  oConeheadZombie: '路障僵尸',
  oBucketheadZombie: '铁桶僵尸',
  oFlagZombie: '旗帜僵尸',
};
let port = null,
  binding = null,
  seq = 0,
  eventSeq = 0,
  entityIds = new WeakMap(),
  cardIds = new WeakMap(),
  decision = id(),
  candidateSet = id();
let fingerprint = '',
  candidateTime = 0,
  publicCandidates = [],
  previousPlants = new Map(),
  playing = false,
  battleReady = false,
  ended = false,
  ready = false;
let trustedInputAt = -Infinity,
  systemPaused = false,
  recentEvents = [],
  sunClicks = new Map();
let zombieMotion = new Map();
const heldPointers = new Set(),
  heldKeys = new Set();
const original = {};
let gameVolume = 0.2;
function applyVolume(value) {
  gameVolume = Math.max(0, Math.min(1, Number(value)));
  if (!Number.isFinite(gameVolume)) gameVolume = 0.2;
  w.companionGameVolume = gameVolume;
  try {
    localStorage.setItem('companion.pvz.volume', String(gameVolume));
  } catch {}
  if (w.masterGain) w.masterGain.gain.value = w.oS?.Silence ? 0 : gameVolume;
  for (const audio of Object.values(w.oAudio || {}))
    if (audio instanceof HTMLMediaElement) audio.volume = gameVolume;
  for (const audio of document.querySelectorAll('audio,video')) audio.volume = gameVolume;
}
try {
  const saved = localStorage.getItem('companion.pvz.volume');
  if (saved !== null) gameVolume = Number(saved);
} catch {}
applyVolume(gameVolume);
document.addEventListener(
  'play',
  (e) => {
    if (e.target instanceof HTMLMediaElement) e.target.volume = gameVolume;
  },
  true,
);
const uid = (object, map) => {
  if (!map.has(object)) map.set(object, id());
  return map.get(object);
};
const label = (object) =>
  names[object.EName]?.[0] ||
  zombieNames[object.EName] ||
  String(object.CName || object.EName || '未知').slice(0, 160);
function supportedLawn() {
  return supportsLawn(w.oS, w.oGd?.$LF);
}
const phase = () =>
  ended
    ? 'ended'
    : !playing
      ? 'menu'
      : !w.oSym?.Timer
        ? 'paused'
        : supportedLawn()
          ? 'playing'
          : 'unsupported';
function visible(id) {
  const element = document.getElementById(id);
  if (!element || !element.getClientRects().length) return false;
  const style = getComputedStyle(element);
  return style.display !== 'none' && style.visibility !== 'hidden';
}
function uiStage() {
  if (ended) return 'ended';
  if (!playing) {
    if (
      visible('dSelectCard') &&
      w.oS?.CanSelectCard &&
      document.getElementById('dSelectCard').classList.contains('show')
    )
      return 'select_cards';
    return !w.oS?.Lvl ? (visible('dAdventure') ? 'menu' : 'title') : 'preparing';
  }
  if (!w.oSym?.Timer) return 'paused';
  return !battleReady ? 'preparing' : supportedLawn() ? 'playing' : 'unsupported';
}
function normalRules() {
  return !w.oS?.AutoSun && w.oSym?.NowStep === 1 && w.oSym?.TimeStep === 10;
}
function focused() {
  try {
    return !document.hidden && (document.hasFocus() || parent.document.hasFocus());
  } catch {
    return !document.hidden && document.hasFocus();
  }
}
function available() {
  return (
    (uiStage() === 'select_cards' || (battleReady && phase() === 'playing' && w.oS.Chose === 0)) &&
    focused() &&
    normalRules() &&
    !heldPointers.size &&
    !heldKeys.size
  );
}
function grid(row, col) {
  return Array.from({ length: 4 }, (_, layer) => w.oGd.$[`${row}_${col}_${layer}`]);
}
function legal(slot, row, col, { manual = false } = {}) {
  if (
    !battleReady ||
    phase() !== 'playing' ||
    document.hidden ||
    !normalRules() ||
    (!manual && w.oS.Chose !== 0)
  )
    return false;
  const card = w.ArCard?.[slot],
    p = card?.PName?.prototype;
  if (
    !p ||
    !names[p.EName] ||
    !card.CDReady ||
    !card.SunReady ||
    !Number.isFinite(p.SunNum) ||
    w.oS.SunNum < p.SunNum
  )
    return false;
  if (
    !Number.isInteger(row) ||
    !Number.isInteger(col) ||
    row < 1 ||
    row > w.oS.R ||
    col < 1 ||
    col > w.oS.C ||
    w.oGd.$LF[row] !== 1
  )
    return false;
  return !!p.CanGrow(grid(row, col), row, col);
}
function laneSignature(row) {
  const zombies = Object.values(w.$Z || {}).filter((z) => z && z.R === row && z.HP > 0);
  const plants = Object.values(w.$P || {}).filter(
    (p) => p && p.R === row && p.C >= 1 && p.C <= w.oS.C && p.HP > 0,
  );
  return JSON.stringify([
    zombies.map((z) => [uid(z, entityIds), zone(z), !!z.isAttacking]).sort(),
    plants.map((p) => [uid(p, entityIds), p.C, !!p.Sleep]).sort(),
  ]);
}
function relativeToCell(row, col) {
  return JSON.stringify(
    Object.values(w.$Z || {})
      .filter((z) => z && z.R === row && z.HP > 0)
      .map((z) => [uid(z, entityIds), (z.ZX ?? z.X ?? z.AttackedLX) >= w.GetX(col)])
      .sort(),
  );
}
function zone(z) {
  const x = z.ZX ?? z.X ?? z.AttackedLX;
  return x < 270 ? 'near' : x < 570 ? 'middle' : 'far';
}
function event(kind, actor, evidence, extra = {}) {
  const payload = {
    event_id: id(),
    event_seq: ++eventSeq,
    kind,
    actor,
    evidence,
    observed_at_host_mono_ms: performance.now(),
    snapshot_seq: seq,
    control_epoch: guard.controlEpoch,
    ...extra,
  };
  if (kind !== 'human_input') {
    recentEvents.push({
      kind,
      actor,
      text: extra.text || '',
      row: extra.row ?? null,
      col: extra.col ?? null,
    });
    recentEvents = recentEvents.slice(-2);
  }
  send('event', payload);
  return payload.event_id;
}
function plant(slot, row, col, commandId = null) {
  if (!legal(slot, row, col, { manual: !commandId })) return { confirmed: false };
  const before = new Set(Object.values(w.$P || {})),
    card = w.ArCard[slot],
    cost = card.PName.prototype.SunNum,
    sun = w.oS.SunNum;
  w.oS.ChoseCard = slot;

  original.GrowPlant(grid(row, col), w.GetX(col), w.GetY(row), row, col);
  const created = Object.values(w.$P || {}).find(
    (p) => p && !before.has(p) && p.R === row && p.C === col,
  );

  if (!created || w.oS.SunNum !== sun - cost) return { confirmed: false };
  const eventId = event(
    'plant',
    commandId ? 'jev' : 'human',
    commandId ? 'own_command' : 'native_hook',
    {
      entity_id: uid(created, entityIds),
      row,
      col,
      text: label(created),
      ...(commandId ? { command_id: commandId } : {}),
    },
  );
  previousPlants.set(uid(created, entityIds), created);
  return { confirmed: true, eventId };
}
function collectible(sunId) {
  const sun = w.ArSun?.[sunId],
    element = document.getElementById(sunId);
  if (!sun?.C || !element || !Number.isFinite(sun.N)) return false;
  const box = element.getBoundingClientRect();
  return (
    box.width > 0 &&
    box.height > 0 &&
    box.bottom > 0 &&
    box.top < innerHeight &&
    box.right > 0 &&
    box.left < innerWidth
  );
}
function collect(sunId, commandId) {
  if (!collectible(sunId)) return { confirmed: false };
  const sun = w.ArSun[sunId],
    instance = guard.instanceId;
  sunClicks.set(sunId, { sun, instance, commandId });
  original.ClickSun.call(w, sunId);
  if (sun.C !== 0) {
    sunClicks.delete(sunId);
    return { confirmed: false };
  }

  setTimeout(() => {
    if (instance === guard.instanceId && guard.status(commandId).status === 'ACCEPTED') {
      const result = guard.complete(commandId, { confirmed: false });
      send('action_result', result);
      observe();
    }
  }, 2000);
  return { pending: true };
}
function selectSeed(type, selected, commandId) {
  const card = w.ArPCard?.[type];
  if (uiStage() !== 'select_cards' || !card?.PName || !!card.Select === selected)
    return { confirmed: false };
  w.SelectCard(type);
  if (!!card.Select !== selected) return { confirmed: false };
  return {
    confirmed: true,
    eventId: event('choose', 'jev', 'own_command', {
      command_id: commandId,
      text: (selected ? '选中' : '取消选择') + label(card.PName.prototype),
    }),
  };
}
function shovelTarget(c) {
  if (!battleReady || phase() !== 'playing' || w.oS.Chose !== 0 || !visible('imgShovel'))
    return null;
  const target = Object.values(w.$P || {}).find(
    (p) => p && uid(p, entityIds) === c.plantId && p.R === c.row && p.C === c.col && p.HP > 0,
  );
  if (!target) return null;
  const layers = grid(c.row, c.col),
    top = [...layers].reverse().find(Boolean);
  return top === target && target.PKind === 1 ? target : null;
}
function shovel(c, commandId) {
  const target = shovelTarget(c);
  if (!target) return { confirmed: false };
  original.ShovelPlant.call(w, [grid(c.row, c.col), target]);
  if (Object.values(w.$P || {}).includes(target)) return { confirmed: false };
  previousPlants.delete(c.plantId);
  return {
    confirmed: true,
    eventId: event('shovel', 'jev', 'own_command', {
      command_id: commandId,
      entity_id: c.plantId,
      row: c.row,
      col: c.col,
      text: label(target),
    }),
  };
}
const guard = new ControlGuard({
  available,
  validate(c) {
    if (c.kind === 'SHOVEL') return !!shovelTarget(c);
    if (c.kind === 'SELECT_SEED' || c.kind === 'DESELECT_SEED')
      return (
        uiStage() === 'select_cards' &&
        w.ArPCard?.[c.plantType] &&
        !!w.ArPCard[c.plantType].Select !== (c.kind === 'SELECT_SEED') &&
        (c.kind === 'DESELECT_SEED' || (w.ArPCard.SelNum || 0) < 10)
      );
    return c.kind === 'COLLECT_SUN'
      ? collectible(c.sunId) && uid(w.ArSun[c.sunId], entityIds) === c.sunEntity
      : legal(c.slot, c.row, c.col) &&
          uid(w.ArCard[c.slot], cardIds) === c.seedId &&
          w.ArCard[c.slot].PName.prototype.SunNum === c.cost &&
          laneSignature(c.row) === c.lane &&
          relativeToCell(c.row, c.col) === c.relative;
  },
  execute(c, commandId) {
    if (c.kind === 'SHOVEL') return shovel(c, commandId);
    if (c.kind === 'SELECT_SEED' || c.kind === 'DESELECT_SEED')
      return selectSeed(c.plantType, c.kind === 'SELECT_SEED', commandId);
    return c.kind === 'COLLECT_SUN'
      ? collect(c.sunId, commandId)
      : plant(c.slot, c.row, c.col, commandId);
  },
});

function send(type, payload, messageId = id()) {
  if (!port || !binding) return;
  port.postMessage({
    protocol_version: 1,
    type,
    message_id: messageId,
    ...binding,
    game_instance_id: guard.instanceId,
    bridge_epoch: guard.bridgeEpoch,
    payload,
  });
}
function hello() {
  send('hello', {
    build: 'PVZM 277c1bf',
    adapter_revision: revision,
    capabilities: {
      mode: 'guarded_control',
      actions: ['PLANT', 'COLLECT_SUN', 'SHOVEL', 'SELECT_SEED', 'DESELECT_SEED'],
      native_input_modes: ['keyboard_mouse', 'pointer'],
      observation_fields: ['sun', 'seed_bank', 'plants', 'zombies', 'suns', 'phase'],
      auto_continue: true,
    },
  });
}
function observe(nonce = null, messageId = null, focus = null) {
  const currentPhase = phase(),
    paused = currentPhase === 'paused',
    hasFocus = focused();
  const { rows, cols } = boardGeometry(w.oS);
  const plants = Object.values(w.$P || {}).filter(
    (p) =>
      p &&
      Number.isInteger(p.R) &&
      Number.isInteger(p.C) &&
      p.R >= 1 &&
      p.R <= rows &&
      p.C >= 1 &&
      p.C <= cols &&
      p.HP > 0,
  );
  const zombies = w.oS?.HaveFog
    ? []
    : Object.values(w.$Z || {}).filter(
        (z) =>
          z &&
          Number.isInteger(z.R) &&
          z.R >= 1 &&
          z.R <= rows &&
          z.HP > 0 &&
          Number.isFinite(z.ZX ?? z.X ?? z.AttackedLX),
      );
  const plantView = plants.map((p) => ({
    entity_id: uid(p, entityIds),
    plant_type: p.EName,
    name: label(p),
    row: p.R,
    col: p.C,
    layer: Number.isInteger(p.PKind) ? p.PKind : 1,
    hp: null,
    state: p.Sleep
      ? 'sleeping'
      : p.EName === 'oPotatoMine'
        ? p.Status
          ? 'armed'
          : 'arming'
        : 'active',
    role: names[p.EName]?.[1] || 'other',
    x: w.GetX(p.C),
  }));
  const motionNow = performance.now();
  const zombieView = zombies.map((z) => {
    const entity_id = uid(z, entityIds),
      x = z.ZX ?? z.X ?? z.AttackedLX,
      old = zombieMotion.get(entity_id);
    let speed = old?.speed ?? null;
    if (old && motionNow - old.at >= 300) {
      const measured = ((old.x - x) / (motionNow - old.at)) * 1000;
      if (measured >= 1 && measured < 1000) speed = Math.round(measured);
    }
    if (!old || motionNow - old.at >= 300) zombieMotion.set(entity_id, { x, at: motionNow, speed });
    return {
      entity_id,
      zombie_type: z.EName,
      name: label(z),
      row: z.R,
      x,
      zone: zone(z),
      hp: null,
      state: z.isAttacking ? 'eating' : 'moving',
      observed_speed: speed,
    };
  });
  for (const entity of zombieMotion.keys())
    if (!zombieView.some((z) => z.entity_id === entity)) zombieMotion.delete(entity);
  const cards = (w.ArCard || [])
    .filter((c) => c?.PName)
    .map((c, i) => ({
      slot: i + 1,
      seed_id: uid(c, cardIds),
      plant_type: c.PName.prototype.EName,
      name: label(c.PName.prototype),
      cost: c.PName.prototype.SunNum,
      ready: !!(c.CDReady && c.SunReady),
      cooldown_ready: !!c.CDReady,
      affordable: Number.isFinite(w.oS?.SunNum) && w.oS.SunNum >= c.PName.prototype.SunNum,
      cooldown_ms: null,
      role: names[c.PName.prototype.EName]?.[1] || 'other',
      effect:
        effects[c.PName.prototype.EName] ||
        String(c.PName.prototype.Tooltip || '')
          .replace(/<[^>]*>/g, '')
          .slice(0, 600),
    }));
  const suns = Object.keys(w.ArSun || {})
    .filter(collectible)
    .slice(0, 128)
    .map((sunId) => ({
      sunId,
      entity_id: uid(w.ArSun[sunId], entityIds),
      amount: w.ArSun[sunId].N,
    }));

  const semantic = JSON.stringify([
    currentPhase,
    battleReady,
    hasFocus,
    guard.controlEpoch,
    normalRules(),
    w.oS?.Chose,
    w.oS?.R,
    w.oS?.C,
    cards.map((c) => c.seed_id),
  ]);
  const changed = semantic !== fingerprint;
  if (changed) {
    decision = id();
    fingerprint = semantic;
  }

  {
    candidateSet = id();
    candidateTime = performance.now();
    const all = [];
    const included = (kind, type = null, row = null, col = null) =>
      !focus ||
      kind === 'COLLECT_SUN' ||
      ((!focus.kinds?.length || focus.kinds.includes(kind)) &&
        (!focus.plant_types?.length || focus.plant_types.includes(type)) &&
        (!focus.rows?.length || focus.rows.includes(row)) &&
        (!focus.cols?.length || focus.cols.includes(col)));
    if (available())
      for (let slot = 0; slot < cards.length; slot++)
        for (let row = 1; row <= w.oS.R; row++)
          for (let col = 1; col <= w.oS.C; col++) {
            if (!included('PLANT', cards[slot].plant_type, row, col)) continue;
            if (legal(slot, row, col))
              all.push({
                slot,
                row,
                col,
                cost: cards[slot].cost,
                seedId: uid(w.ArCard[slot], cardIds),
                plantType: cards[slot].plant_type,
                lane: laneSignature(row),
                relative: relativeToCell(row, col),
                kind: 'PLANT',
                expiresAt: candidateTime + 3000,
              });
          }

    const rows = [1, 2, 3, 4, 5].sort((a, b) => {
      const distance = (r) =>
        Math.min(3000, ...zombieView.filter((z) => z.row === r).map((z) => z.x));
      return distance(a) - distance(b) || a - b;
    });
    const ordered = [];
    for (let index = 0; index < 9; index++)
      for (const row of rows)
        for (let slot = 0; slot < cards.length; slot++) {
          const cols =
            cards[slot].role === 'economy'
              ? [2, 1, 3, 4, 5, 6, 7, 8, 9]
              : cards[slot].role === 'attack'
                ? [4, 2, 6, 1, 3, 5, 7, 8, 9]
                : [6, 4, 2, 5, 3, 7, 1, 8, 9];
          const c = all.find((c) => c.row === row && c.col === cols[index] && c.slot === slot);
          if (c) ordered.push(c);
        }
    const sunChoices = available() ? suns.slice(0, 3) : [];

    const chosen = ordered
      .slice(0, 63 - sunChoices.length)
      .map((c, i) => ({ ...c, action_id: `plant:${i + 1}` }));
    publicCandidates = chosen.map((c) => ({
      action_id: c.action_id,
      label: `${cards[c.slot].name}，第${c.row}路第${c.col}格`,
      kind: 'PLANT',
      facts: {
        seed_id: c.seedId,
        plant_type: c.plantType,
        row: c.row,
        col: c.col,
        cost: cards[c.slot].cost,
        effect: cards[c.slot].effect,
      },
      dependencies: [`lane:${c.row}`, `cell:${c.row}:${c.col}`, c.seedId],
    }));
    for (const [i, sun] of sunChoices.entries()) {
      const action_id = `sun:${i + 1}`;
      chosen.push({
        action_id,
        kind: 'COLLECT_SUN',
        sunId: sun.sunId,
        sunEntity: sun.entity_id,
        expiresAt: candidateTime + 3000,
      });
      publicCandidates.push({
        action_id,
        label: `收集${sun.amount}点阳光`,
        kind: 'COLLECT_SUN',
        facts: {
          target_id: sun.entity_id,
          effect: `收集画面中的${sun.amount}点阳光，正常动画完成后到账`,
        },
        dependencies: [sun.entity_id],
      });
    }
    if (available() && uiStage() === 'select_cards')
      for (const card of Object.values(w.ArPCard || {}).filter((c) => c?.PName)) {
        const plantType = card.PName.prototype.EName,
          kind = card.Select ? 'DESELECT_SEED' : 'SELECT_SEED';
        if (
          !included(kind, plantType) ||
          (kind === 'SELECT_SEED' && (w.ArPCard.SelNum || 0) >= 10) ||
          chosen.length >= 63
        )
          continue;
        const action_id = (kind === 'SELECT_SEED' ? 'select:' : 'deselect:') + plantType;
        chosen.push({ action_id, kind, plantType, expiresAt: candidateTime + 3000 });
        publicCandidates.push({
          action_id,
          kind,
          label: (card.Select ? '取消' : '选择') + label(card.PName.prototype),
          facts: { plant_type: plantType },
          dependencies: [plantType],
        });
      }
    if (available() && focus?.kinds?.includes('SHOVEL'))
      for (const p of plants) {
        if (!included('SHOVEL', p.EName, p.R, p.C) || chosen.length >= 63) continue;
        const c = {
          action_id: 'shovel:' + uid(p, entityIds),
          kind: 'SHOVEL',
          plantId: uid(p, entityIds),
          row: p.R,
          col: p.C,
          expiresAt: candidateTime + 3000,
        };
        if (!shovelTarget(c)) continue;
        chosen.push(c);
        publicCandidates.push({
          action_id: c.action_id,
          kind: 'SHOVEL',
          label: `铲掉${label(p)}，第${p.R}路第${p.C}格`,
          facts: { target_id: c.plantId, plant_type: p.EName, row: p.R, col: p.C },
          dependencies: [c.plantId],
        });
      }
    publicCandidates.push({
      action_id: 'wait',
      label: '暂时等待，观察局面',
      kind: 'WAIT',
      facts: { effect: '本次不操作；仅已获持续代打授权时才会重新判断下一步' },
      dependencies: [],
    });
    guard.snapshot(decision, candidateSet, seq + 1, chosen);
    observe.total = Math.max(publicCandidates.length, all.length + suns.length + 1);
  }
  const currentIds = new Set(plantView.map((p) => p.entity_id));
  for (const [entity, p] of previousPlants)
    if (!currentIds.has(entity))
      event('entity_lost', 'unknown', 'state_diff', {
        entity_id: entity,
        row: p.R,
        col: p.C,
        text: label(p),
      });
  previousPlants = new Map(plants.map((p) => [uid(p, entityIds), p]));
  const canControl = guard.available();
  const payload = {
    snapshot_seq: ++seq,
    decision_id: decision,
    control_epoch: guard.controlEpoch,
    candidate_set_id: candidateSet,
    candidate_coverage: canControl ? 'filtered_legal_subset' : 'manual_only',
    total_legal_actions: canControl ? observe.total || 0 : 0,
    phase: currentPhase,
    paused,
    focused: hasFocus,
    awaiting_action: canControl,
    observed_at_host_mono_ms: performance.now(),
    view: {
      game: 'pvz_web',
      scene: supportedLawn() ? 'day_lawn' : 'unsupported',
      rows,
      cols,
      sun: Number.isFinite(w.oS?.SunNum) ? w.oS.SunNum : null,
      enhanced_telemetry: false,
      seed_bank: cards,
      plants: plantView,
      zombies: zombieView,
      mowers: [],
      selected_seed:
        w.oS?.Chose === 1 && w.ArCard?.[w.oS.ChoseCard]
          ? uid(w.ArCard[w.oS.ChoseCard], cardIds)
          : null,
      auto_collect_sun: !!w.oS?.AutoSun,
      normal_rules: normalRules(),
      level_name: String(w.oS?.LevelName || '').slice(0, 160),
      plantable_rows: Array.from({ length: rows }, (_, i) => i + 1).filter(
        (row) => w.oGd?.$LF?.[row] === 1,
      ),
      ui_stage: uiStage(),
      column_x: Array.from({ length: cols }, (_, i) => w.GetX(i + 1)),
      seed_selection_limit: 10,
      tutorial_text: visible('DivTeachBar')
        ? String(document.getElementById('DivTeachBar').textContent || '').slice(0, 600)
        : visible('DivTeach')
          ? String(document.getElementById('DivTeach').textContent || '').slice(0, 600)
          : '',
      card_choices: !playing
        ? Object.values(w.ArPCard || {})
            .filter((c) => c?.PName)
            .slice(0, 64)
            .map((c) => ({
              plant_type: c.PName.prototype.EName,
              name: label(c.PName.prototype),
              cost: c.PName.prototype.SunNum,
              selected: !!c.Select,
              role: names[c.PName.prototype.EName]?.[1] || 'other',
              effect:
                effects[c.PName.prototype.EName] ||
                String(c.PName.prototype.Tooltip || '')
                  .replace(/<[^>]*>/g, '')
                  .slice(0, 600),
            }))
        : [],
      suns: suns.map(({ entity_id, amount }) => ({ entity_id, amount })),
      input_state:
        w.oS?.Chose === 1
          ? 'plant_selected'
          : w.oS?.Chose === -1
            ? 'shovel_selected'
            : heldPointers.size || heldKeys.size
              ? 'held'
              : 'idle',
    },
    legal_actions: canControl ? publicCandidates : [],
    recent_events: recentEvents,
    field_quality: {
      mower_state: 'unknown',
      health: 'unknown',
      cooldown_ms: 'unknown',
      zombies: w.oS?.HaveFog ? 'unsupported' : 'observed',
    },
    nonce,
    autoplay: guard.autoplayState(),
    control_reason: guard.controlReason,
  };
  send('snapshot', payload, messageId || id());
  return payload;
}
function hook(name, fn) {
  original[name] = w[name];
  w[name] = fn;
}
function install() {
  if (!w.oS?.Init || !w.GrowPlant || !w.oPeashooter) return false;

  StorageUtil.setItem('JSPVZAutoSun', 0);
  w.oS.AutoSun = 0;
  w.$User.Visitor.NowStep = 1;
  w.$User.Visitor.TimeStep = 10;
  const init = w.oS.Init;
  w.oS.Init = function (...args) {
    playing = false;
    battleReady = false;
    ended = false;
    guard.reset();
    seq = eventSeq = 0;
    recentEvents = [];
    sunClicks.clear();
    zombieMotion.clear();
    entityIds = new WeakMap();
    cardIds = new WeakMap();
    fingerprint = '';
    previousPlants.clear();
    StorageUtil.setItem('JSPVZAutoSun', 0);
    this.AutoSun = 0;
    const result = init.apply(this, args);
    const finish = w.oP.FlagToEnd;
    w.oP.FlagToEnd = function (...a) {
      ended = true;
      guard.revoke('battle_ended');
      const result = finish.apply(this, a);
      observe();
      return result;
    };
    if (port) hello();
    return result;
  };
  hook('LetsGO', function (...args) {
    const result = original.LetsGO.apply(this, args);
    playing = true;
    observe();
    return result;
  });
  hook('BeginCool', function (...args) {
    const result = original.BeginCool.apply(this, args);
    battleReady = true;
    observe();
    return result;
  });
  hook('ChosePlant', function (...args) {
    const result = original.ChosePlant.apply(this, args);
    if (w.oS.Chose === 1 && performance.now() - trustedInputAt < 1500) {
      const card = w.ArCard?.[w.oS.ChoseCard];
      if (card) event('select_seed', 'human', 'native_hook', { text: label(card.PName.prototype) });
    }
    return result;
  });
  hook('PauseGame', function (...args) {
    const wasPaused = !w.oSym?.Timer;
    const result = original.PauseGame.apply(this, args);
    if (wasPaused !== !w.oSym?.Timer) {
      const human = performance.now() - trustedInputAt < 1500;
      event(w.oSym?.Timer ? 'resume' : 'pause', human ? 'human' : 'engine', 'native_hook');
      guard.revoke('pause_changed');
    }
    observe();
    return result;
  });
  hook('ResetGame', function (...args) {
    const result = original.ResetGame.apply(this, args);
    guard.revoke('resume');
    event('resume', performance.now() - trustedInputAt < 1500 ? 'human' : 'engine', 'native_hook');
    observe();
    return result;
  });
  hook('GrowPlant', function (plants, x, y, row, col) {
    if (performance.now() - trustedInputAt > 1500 || guard.ownCommand) return;

    if (
      phase() !== 'playing' ||
      !normalRules() ||
      !names[w.ArCard?.[w.oS.ChoseCard]?.PName?.prototype.EName]
    )
      return original.GrowPlant.call(this, plants, x, y, row, col);
    return plant(w.oS.ChoseCard, row, col);
  });
  hook('ShovelPlant', function (data) {
    const target = data?.[1],
      entity = target && uid(target, entityIds);
    const result = original.ShovelPlant.call(this, data);
    if (
      target &&
      !Object.values(w.$P || {}).includes(target) &&
      performance.now() - trustedInputAt < 1500
    ) {
      event('shovel', 'human', 'native_hook', {
        entity_id: entity,
        row: target.R,
        col: target.C,
        text: label(target),
      });
      previousPlants.delete(entity);
    }
    return result;
  });
  hook('ClickSun', function (sunId) {
    const sun = w.ArSun?.[sunId],
      human = performance.now() - trustedInputAt < 1500;
    if (sun && sun.C && human) sunClicks.set(sunId, { sun, instance: guard.instanceId });
    const result = original.ClickSun.call(this, sunId);
    return result;
  });
  hook('MoveClickSun', function (sunId) {
    const clicked = sunClicks.get(sunId),
      el = w.$(sunId);
    const result = original.MoveClickSun.call(this, sunId);

    if (clicked && el) {
      const completed = (e) => {
        if (e.target !== el || w.ArSun[sunId]) return;
        el.removeEventListener('animationend', completed);
        sunClicks.delete(sunId);
        if (clicked.instance === guard.instanceId) {
          const commandId = clicked.commandId;
          const eventId = event(
            'collect_sun',
            commandId ? 'jev' : 'human',
            commandId ? 'own_command' : 'native_hook',
            {
              text: '已收集' + clicked.sun.N + '点阳光',
              ...(commandId ? { command_id: commandId } : {}),
            },
          );
          if (commandId) {
            const result = guard.complete(commandId, { confirmed: true, eventId });
            if (result) send('action_result', result);
            observe();
          }
        }
      };
      el.addEventListener('animationend', completed);
    }
    return result;
  });
  hook('GameOver', function (...a) {
    ended = true;
    guard.revoke('battle_ended');
    const result = original.GameOver.apply(this, a);
    observe();
    return result;
  });
  for (const kind of ['pointerdown', 'keydown'])
    document.addEventListener(
      kind,
      (e) => {
        if (!e.isTrusted) return;
        if (kind === 'pointerdown') heldPointers.add(e.pointerId);
        else heldKeys.add(e.code);
        trustedInputAt = performance.now();
        guard.nativeInput(kind);
        event('human_input', 'human', 'native_hook', { text: kind });
      },
      true,
    );
  document.addEventListener('pointerup', (e) => heldPointers.delete(e.pointerId), true);
  document.addEventListener('keyup', (e) => heldKeys.delete(e.code), true);
  document.addEventListener(
    'pointercancel',
    (e) => {
      heldPointers.delete(e.pointerId);
      guard.revoke('pointer_cancelled');
    },
    true,
  );
  document.addEventListener('visibilitychange', () => {
    guard.revoke('visibility_changed');
    if (document.hidden && w.oSym?.Timer) {
      w.PauseGame(w.$('dMenu0'), 0);
      systemPaused = true;
    }
    if (!document.hidden && systemPaused) {
      systemPaused = false;
    }
    observe();
  });
  window.addEventListener('blur', () => {
    heldPointers.clear();
    heldKeys.clear();
    if (!focused()) guard.revoke('focus_lost');
  });
  window.addEventListener('pagehide', () => guard.disconnect());
  setInterval(() => {
    if (!port) return;
    guard.available();
    send('heartbeat', { nonce: id() });
    observe();
  }, 500);
  ready = true;
  parent.postMessage({ type: 'game.ready' }, location.origin);
  return true;
}
window.addEventListener('message', (e) => {
  if (
    e.origin !== location.origin ||
    e.source !== parent ||
    e.data?.type !== 'game.bind' ||
    e.ports.length !== 1 ||
    !ready
  )
    return;
  const m = e.data;
  if (
    m.game_id !== 'pvz_web' ||
    typeof m.session_id !== 'string' ||
    !Number.isSafeInteger(m.active_game_epoch)
  )
    return;
  port?.close();
  port = e.ports[0];
  binding = {
    session_id: m.session_id,
    game_id: 'pvz_web',
    active_game_epoch: m.active_game_epoch,
  };
  guard.connect(id());
  fingerprint = '';
  hello();
  port.onmessage = (e) => {
    const m = e.data;
    if (m?.type === 'local_volume') {
      if (typeof m.value === 'number') applyVolume(m.value);
      return;
    }
    if (m?.type === 'local_start_lawn' || m?.type === 'local_start_tutorial') {
      if (w.$Pn && w.EDNewAll) {
        guard.revoke('user_selected_level');
        w.SelectModal(m.type === 'local_start_tutorial' ? 1 : 8);
      }
      return;
    }
    if (m?.type === 'local_reclaim') {
      const ack = guard.revoke();
      send('revoke_ack', { control_epoch: ack.control_epoch });
      observe();
      return;
    }
    if (m?.type === 'transport_disconnected') {
      guard.disconnect();
      port.close();
      port = null;
      return;
    }
    if (
      !m ||
      m.protocol_version !== 1 ||
      m.session_id !== binding.session_id ||
      m.active_game_epoch !== binding.active_game_epoch ||
      m.game_instance_id !== guard.instanceId ||
      m.bridge_epoch !== guard.bridgeEpoch
    )
      return;
    try {
      validateCommand(m);
      if (m.type === 'heartbeat' || m.type === 'hello_ack') {
        guard.heartbeat();
        return;
      }
      if (m.type === 'observe') {
        guard.observedFresh();
        observe(m.payload.nonce, m.message_id, m.payload.focus);
        return;
      }
      if (m.type === 'start_autoplay') {
        send('autoplay_result', guard.startAutoplay(m), m.message_id);
        observe();
        return;
      }
      if (m.type === 'discard_step') {
        guard.discardStep(m.payload.grant_id);
        return;
      }
      if (m.type === 'arm_once' || m.type === 'arm_autoplay_step') {
        send('grant_result', guard.arm(m), m.message_id);
        return;
      }
      if (m.type === 'submit_once') {
        send('action_result', guard.submit(m), m.message_id);
        observe();
        return;
      }
      if (m.type === 'revoke') {
        send(
          'revoke_ack',
          { control_epoch: guard.revoke(m.payload.reason).control_epoch },
          m.message_id,
        );
        observe();
        return;
      }
      if (m.type === 'get_action_status')
        send('action_status', guard.status(m.payload.command_id), m.message_id);
    } catch {
      guard.revoke('protocol_error');
      send('error', { code: 'invalid_local_command' }, m.message_id);
    }
  };
  observe();
});
function validateCommand(m) {
  const keys = (o, names) => {
    if (
      !o ||
      typeof o !== 'object' ||
      Array.isArray(o) ||
      Object.keys(o).length !== names.length ||
      Object.keys(o).some((k) => !names.includes(k))
    )
      throw new Error('fields');
  };
  const ident = (v) => {
    if (typeof v !== 'string' || !/^[-A-Za-z0-9_.:]{1,128}$/.test(v)) throw new Error('identifier');
  };
  const counter = (v) => {
    if (!Number.isSafeInteger(v) || v < 0) throw new Error('counter');
  };
  const anchor = (a) => {
    keys(a, ['snapshot_seq', 'decision_id', 'control_epoch', 'candidate_set_id']);
    counter(a.snapshot_seq);
    counter(a.control_epoch);
    ident(a.decision_id);
    ident(a.candidate_set_id);
  };
  keys(m, [
    'protocol_version',
    'type',
    'message_id',
    'session_id',
    'game_id',
    'game_instance_id',
    'bridge_epoch',
    'active_game_epoch',
    'payload',
  ]);
  ident(m.message_id);
  if (m.game_id !== 'pvz_web') throw new Error('game');
  const p = m.payload;
  switch (m.type) {
    case 'start_autoplay':
      keys(p, ['source_event_id', 'anchor', 'scope', 'duration_ms', 'allowed_kinds']);
      ident(p.source_event_id);
      anchor(p.anchor);
      if (
        !['continuous', 'timed', 'level', 'task'].includes(p.scope) ||
        (p.scope === 'timed'
          ? !Number.isSafeInteger(p.duration_ms) || p.duration_ms < 1000 || p.duration_ms > 43200000
          : p.duration_ms !== null) ||
        !Array.isArray(p.allowed_kinds) ||
        !p.allowed_kinds.length ||
        p.allowed_kinds.some(
          (k) => !['PLANT', 'COLLECT_SUN', 'SHOVEL', 'SELECT_SEED', 'DESELECT_SEED'].includes(k),
        )
      )
        throw new Error('scope');
      break;
    case 'arm_once':
    case 'arm_autoplay_step':
      keys(p, [
        'source_event_id',
        'anchor',
        'allowed_kinds',
        'ttl_ms',
        ...(m.type === 'arm_autoplay_step' ? ['autoplay_id'] : []),
      ]);
      if (m.type === 'arm_autoplay_step') ident(p.autoplay_id);
      ident(p.source_event_id);
      anchor(p.anchor);
      counter(p.ttl_ms);
      if (
        p.ttl_ms < 1 ||
        p.ttl_ms > 15000 ||
        !Array.isArray(p.allowed_kinds) ||
        !p.allowed_kinds.length ||
        p.allowed_kinds.length > 5 ||
        p.allowed_kinds.some(
          (k) => !['PLANT', 'COLLECT_SUN', 'SHOVEL', 'SELECT_SEED', 'DESELECT_SEED'].includes(k),
        )
      )
        throw new Error('grant');
      break;
    case 'submit_once':
      keys(p, ['command_id', 'grant_id', 'action_id', 'anchor']);
      for (const k of ['command_id', 'grant_id', 'action_id']) ident(p[k]);
      anchor(p.anchor);
      break;
    case 'discard_step':
      keys(p, ['grant_id']);
      ident(p.grant_id);
      break;
    case 'observe':
      keys(p, ['nonce', ...('focus' in p ? ['focus'] : [])]);
      ident(p.nonce);
      if (p.focus) {
        keys(p.focus, ['kinds', 'plant_types', 'rows', 'cols']);
        if (
          !Array.isArray(p.focus.kinds) ||
          !Array.isArray(p.focus.plant_types) ||
          !Array.isArray(p.focus.rows) ||
          !Array.isArray(p.focus.cols)
        )
          throw new Error('focus');
        p.focus.plant_types.forEach(ident);
        if (
          p.focus.kinds.some(
            (k) =>
              !['PLANT', 'COLLECT_SUN', 'SHOVEL', 'SELECT_SEED', 'DESELECT_SEED', 'WAIT'].includes(
                k,
              ),
          ) ||
          p.focus.plant_types.length > 16 ||
          p.focus.rows.some((x) => !Number.isInteger(x) || x < 1 || x > 6) ||
          p.focus.cols.some((x) => !Number.isInteger(x) || x < 1 || x > 9)
        )
          throw new Error('focus');
      }
      break;
    case 'heartbeat':
      keys(p, ['nonce']);
      ident(p.nonce);
      break;
    case 'revoke':
      keys(p, ['reason']);
      if (typeof p.reason !== 'string' || p.reason.length > 160) throw new Error('reason');
      break;
    case 'get_action_status':
      keys(p, ['command_id']);
      ident(p.command_id);
      break;
    case 'hello_ack':
      break;
    default:
      throw new Error('operation');
  }
}
const init = setInterval(() => {
  if (install()) clearInterval(init);
}, 100);
