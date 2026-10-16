export class VoxelPanel {
  constructor(host, { command, state, audio }) {
    this.host = host;
    this.command = command;
    this.onstate = state;
    this.audio = audio;
    this.seq = 0;
    this.socket = null;
    this.mode = 'released';
    this.dx = 0;
    this.dy = 0;
    this.keys = new Set();
    this.root = document.createElement('div');
    this.root.className = 'voxel-panel';
    this.root.innerHTML = `<div class="voxel-toolbar"><button data-enter>进入游戏</button><button data-inventory>背包 / 继续</button><button data-fullscreen>全屏</button><span data-status>正在连接画面</span></div><canvas width="1024" height="640" tabindex="0" aria-label="VoxeLibre 游戏画面"></canvas><p>点击进入后：WASD 移动，空格跳跃，Shift 潜行；鼠标转向，左键采集，右键放置，滚轮切换。E 打开背包，Esc 释放鼠标。P 请求放一块，R 收回。</p>`;
    this.canvas = this.root.querySelector('canvas');
    this.ctx = this.canvas.getContext('2d', { alpha: false });
    this.status = this.root.querySelector('[data-status]');
    host.append(this.root);
    const watch = document.createElement('button');
    watch.textContent = '旁观 / 对话';
    watch.dataset.watch = '';
    watch.onclick = () => this.watch();
    this.root.querySelector('.voxel-toolbar').append(watch);
    this.root.querySelector('p').textContent +=
      ' 可以说“帮我造个小屋”一起商量设计；旁观时可继续语音或输入文字。';
    const project = document.createElement('section');
    project.className = 'voxel-project';
    project.hidden = true;
    project.setAttribute('aria-label', '一起建造');
    project.innerHTML = `<div><strong data-project-title></strong><span data-project-status></span></div><p data-project-brief></p><p data-project-question></p><div class="voxel-project-design" hidden><canvas data-blueprint width="180" height="180" aria-label="方案逐层预览"></canvas><div><label>查看方案 <select data-layer aria-label="预览楼层"></select></label><p data-project-materials></p></div></div><progress data-progress value="0" max="1" aria-label="建造进度"></progress><div class="voxel-toolbar"><button data-project-start>开始建造</button><button data-project-pause>暂停，我自己来</button></div>`;
    this.root.append(project);
    this.root.querySelector('[data-project-start]').onclick = async () => {
      if (!this.project) return;
      const expected = { project_id: this.project.id, revision: this.project.revision };
      if (await this.watch()) this.command('project_start', expected);
    };
    this.root.querySelector('[data-project-pause]').onclick = () => this.command('project_pause');
    this.root.querySelector('[data-layer]').onchange = () => this.drawBlueprint();
    const task = document.createElement('section');
    task.className = 'voxel-project voxel-survival-task';
    task.hidden = true;
    task.innerHTML =
      '<strong data-task-title></strong><span data-task-status></span><p data-task-request></p><p data-task-progress></p><p data-task-question></p><button data-task-resume>继续任务</button><button data-task-stop>停下，我自己来</button>';
    this.root.append(task);
    task.querySelector('[data-task-stop]').onclick = () => this.command('reclaim');
    task.querySelector('[data-task-resume]').onclick = async () => {
      if (await this.watch()) this.command('task_resume', { task_id: this.task?.id });
    };
    this.abort = new AbortController();
    const signal = this.abort.signal;
    const on = (target, type, fn) => target.addEventListener(type, fn, { signal });
    this.root.querySelector('[data-enter]').onclick = () => this.enter();
    this.root.querySelector('[data-inventory]').onclick = () => this.inventory();
    this.root.querySelector('[data-fullscreen]').onclick = () => this.root.requestFullscreen();
    on(this.canvas, 'click', () => {
      if (this.mode === 'released') this.enter();
    });
    on(this.canvas, 'contextmenu', (e) => e.preventDefault());
    on(document, 'pointerlockchange', () => {
      if (document.pointerLockElement === this.canvas) {
        this.mode = 'look';
        this.send({ kind: 'mode', mode: 'look' });
        this.status.textContent = '正在操作 · Esc 释放鼠标';
      } else if (this.mode === 'watch') {
        this.openingMenu = false;
        return;
      } else if (!this.openingMenu) this.release('鼠标已释放，自动操作已停止');
      this.openingMenu = false;
    });
    on(document, 'mousemove', (e) => {
      if (this.mode === 'look' && document.pointerLockElement === this.canvas) {
        this.dx += e.movementX;
        this.dy += e.movementY;
      } else if (this.mode === 'menu' && e.target === this.canvas) {
        const r = this.canvas.getBoundingClientRect();
        this.pointer = {
          x: ((e.clientX - r.left) * 1024) / r.width,
          y: ((e.clientY - r.top) * 640) / r.height,
        };
      }
    });
    on(this.canvas, 'mousedown', (e) => {
      if (this.mode === 'watch') {
        this.command('reclaim');
        this.enter();
        return;
      }
      if (this.mode === 'released') return;
      e.preventDefault();
      this.flushMove();
      this.send({ kind: 'button', button: e.button, down: true });
    });
    on(document, 'mouseup', (e) => {
      if (this.mode !== 'released') this.send({ kind: 'button', button: e.button, down: false });
    });
    document.addEventListener(
      'wheel',
      (e) => {
        if (this.mode !== 'look' || document.pointerLockElement !== this.canvas) return;
        e.preventDefault();
        this.send({ kind: 'wheel', delta: e.deltaY });
      },
      { signal, passive: false },
    );
    on(document, 'keydown', (e) => {
      if (this.mode === 'released' || e.target.matches('input,textarea,select')) return;
      if (e.code === 'Escape') {
        e.preventDefault();
        if (this.mode === 'menu')
          this.tap('Escape').then(() => this.release('已关闭背包，点击进入继续'));
        else this.release('鼠标已释放，点击进入继续');
        return;
      }
      if (e.code === 'KeyR') {
        e.preventDefault();
        this.command('reclaim');
        return;
      }
      if (e.code === 'KeyP') {
        e.preventDefault();
        if (!e.repeat) this.command('play_once');
        return;
      }
      if (e.code === 'KeyE' || e.code === 'KeyI') {
        e.preventDefault();
        if (!e.repeat) this.inventory();
        return;
      }
      if (!/^(Key[WASD]|Space|ShiftLeft|ControlLeft|Digit[1-9])$/.test(e.code)) return;
      if (this.mode === 'watch') {
        this.command('reclaim');
        this.mode = 'look';
        this.send({ kind: 'mode', mode: 'look' });
        this.canvas.requestPointerLock().catch(() => {});
      }
      e.preventDefault();
      if (!e.repeat) {
        this.keys.add(e.code);
        this.send({ kind: 'key', code: e.code, down: true });
      }
    });
    on(document, 'keyup', (e) => {
      if (this.keys.has(e.code)) {
        e.preventDefault();
        this.keys.delete(e.code);
        this.send({ kind: 'key', code: e.code, down: false });
      }
    });
    on(window, 'blur', () => this.release('窗口失焦，点击进入继续'));
    on(document, 'visibilitychange', () => {
      if (document.hidden) this.release('页面在后台，点击进入继续');
    });
    on(document, 'fullscreenchange', () => this.release('显示模式已变化，点击进入继续'));
    this.timer = setInterval(() => {
      this.flushMove();
    }, 1000 / 30);
    this.heartbeat = setInterval(() => this.send({ kind: 'heartbeat' }), 400);
  }
  connect(token) {
    const ws = (this.socket = new WebSocket(
      `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/games/voxel/stream`,
    ));
    ws.binaryType = 'blob';
    ws.onopen = () => {
      ws.send(JSON.stringify({ token, frame_ack: true }));
      this.status.textContent = '点击进入游戏';
    };
    ws.onmessage = async (e) => {
      if (typeof e.data === 'string') {
        const m = JSON.parse(e.data);
        if (m.type === 'game_audio') this.playAudio(m);
        return;
      }
      if (this.decoding) return;
      this.decoding = true;
      try {
        const bitmap = await createImageBitmap(e.data);
        this.ctx.drawImage(bitmap, 0, 0);
        bitmap.close();
        this.frames = (this.frames || 0) + 1;
      } finally {
        this.decoding = false;
        this.send({ kind: 'frame_ack' });
      }
    };
    ws.onclose = () => {
      this.release('画面连接已断开，请重新连接游戏');
    };
  }
  send(data) {
    if (this.socket?.readyState === 1)
      this.socket.send(JSON.stringify({ ...data, seq: ++this.seq }));
  }
  async tap(code) {
    this.send({ kind: 'key', code, down: true });
    await new Promise((resolve) => setTimeout(resolve, 80));
    this.send({ kind: 'key', code, down: false });
  }
  flushMove() {
    if (this.mode === 'look' && (this.dx || this.dy)) {
      this.send({ kind: 'motion', x: this.dx, y: this.dy });
      this.dx = 0;
      this.dy = 0;
    }
    if (this.mode === 'menu' && this.pointer) {
      this.send({ kind: 'pointer', ...this.pointer });
      this.pointer = null;
    }
  }
  async enter() {
    if (this.mode === 'watch') this.command('reclaim');
    this.canvas.focus();
    if (!this.audioContext) {
      this.audioContext = new AudioContext({ sampleRate: 24000 });
      this.audioGain = this.audioContext.createGain();
      this.audioGain.gain.value = this.volume ?? 1;
      this.audioGain.connect(this.audioContext.destination);
      this.recordingDestination = this.audioContext.createMediaStreamDestination();
      this.audioGain.connect(this.recordingDestination);
    }
    await this.audioContext.resume();
    if (this.phase === 'dead' || this.clientMenu) {
      this.mode = 'menu';
      this.send({ kind: 'mode', mode: 'menu' });
      this.status.textContent = '可以点击游戏菜单；关闭后再点进入';
      return;
    }
    try {
      await this.canvas.requestPointerLock();
    } catch {
      this.status.textContent = '浏览器未允许锁定鼠标，请再次点击进入';
    }
  }
  async watch() {
    if (this.phase !== 'playing' || this.clientMenu) {
      this.status.textContent = '请先关闭游戏菜单，再开始旁观建造';
      return false;
    }
    if (this.mode !== 'watch') {
      this.dx = 0;
      this.dy = 0;
      this.keys.clear();
      this.mode = 'watch';
      this.send({ kind: 'mode', mode: 'watch' });
      if (document.pointerLockElement === this.canvas) document.exitPointerLock();
    }
    this.status.textContent = '旁观中 · 可以继续对话，按 R 收回';
    const started = performance.now();
    while (performance.now() - started < 2500) {
      if (this.mode !== 'watch') return false;
      if (this.snapshot?.control.input_mode === 'watch') return true;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    this.status.textContent = '控制状态尚未同步，请稍后再试';
    return false;
  }
  updateProject(project) {
    this.project = project;
    const host = this.root.querySelector('.voxel-project');
    host.hidden = !project || project.status === 'CANCELLED';
    if (!project) return;
    const q = (selector) => this.root.querySelector(selector),
      plan = project.blueprint;
    q('[data-project-title]').textContent = project.title;
    const statuses = {
      DISCUSSING: '商量设计',
      PLANNING: '正在整理方案',
      READY: '方案已就绪',
      ARMING: '准备开始',
      RUNNING: '正在建造',
      PAUSED: '已暂停',
      NEEDS_MATERIALS: '等待材料',
      COMPLETED: '方案已完成',
    };
    q('[data-project-status]').textContent = statuses[project.status] || project.status;
    q('[data-project-brief]').textContent = Object.entries(project.requirements || {})
      .map(([k, v]) => `${k}：${typeof v === 'object' ? v.value : v}`)
      .join(' · ');
    q('[data-project-question]').textContent = project.pending_question?.question || '';
    q('.voxel-project-design').hidden = !plan;
    if (plan) {
      const layer = q('[data-layer]'),
        old = layer.value;
      layer.replaceChildren();
      for (let y = 0; y < (plan.size?.[2] || 0); y++) {
        const option = document.createElement('option');
        option.value = y;
        option.textContent = `第 ${y + 1} 层`;
        layer.append(option);
      }
      if ([...layer.options].some((o) => o.value === old)) layer.value = old;
      const names = Object.fromEntries(
        (this.snapshot?.view.construction?.materials || []).map((m) => [m.item_id, m.name]),
      );
      q('[data-project-materials]').textContent =
        `${plan.size.join(' × ')}，共 ${plan.count} 块。` +
        Object.entries(plan.materials || {})
          .map(([id, count]) => `${names[id] || id} ${count}`)
          .join('；');
      this.drawBlueprint();
    }
    q('[data-progress]').max = plan?.count || 1;
    q('[data-progress]').value = project.completed_count || 0;
    q('[data-project-start]').disabled =
      !plan || ['PLANNING', 'ARMING', 'RUNNING', 'COMPLETED'].includes(project.status);
    q('[data-project-start]').textContent = project.completed_count ? '继续建造' : '开始建造';
    q('[data-project-pause]').disabled = !['RUNNING', 'ARMING', 'PLANNING'].includes(
      project.status,
    );
  }
  updateTask(task) {
    this.task = task;
    const root = this.root.querySelector('.voxel-survival-task');
    root.hidden = !task || task.status === 'CANCELLED';
    if (!task) return;
    root.querySelector('[data-task-title]').textContent =
      {
        DIG: '打通与采挖',
        PLACE: '放置',
        CRAFT: '合成',
        COMBAT: '战斗',
        TRAVEL: '移动与脱险',
        AGENT: '持续接手',
      }[task.kind] || '游戏任务';
    root.querySelector('[data-task-status]').textContent =
      ' · ' +
      ({
        PLANNING: '正在理解要求',
        READY: '等待开始',
        ARMING: '准备操作',
        RUNNING: '进行中',
        PAUSED: '已暂停',
        NEEDS_INPUT: '需要补充',
        COMPLETED: '已完成',
      }[task.status] || task.status);
    root.querySelector('[data-task-request]').textContent = task.agenda?.summary || task.request;
    root.querySelector('[data-task-progress]').textContent = task.agenda
      ? task.agenda.steps
          .map(
            (s, i) =>
              `${i < task.agenda_index ? '✓' : i === task.agenda_index ? '→' : '·'} ${s.label}`,
          )
          .join('　')
      : `已完成 ${task.completed || 0}${task.quantity ? ` / ${task.quantity}` : ''}${task.kind === 'COMBAT' ? ' 次攻击' : ' 个'}`;
    root.querySelector('[data-task-question]').textContent = task.pending_question || '';
    root.querySelector('[data-task-resume]').disabled = !['PAUSED', 'READY'].includes(task.status);
    root.querySelector('[data-task-stop]').disabled = task.status === 'COMPLETED';
  }
  drawBlueprint() {
    const plan = this.project?.blueprint;
    if (!plan) return;
    const c = this.root.querySelector('[data-blueprint]'),
      ctx = c.getContext('2d'),
      y = Number(this.root.querySelector('[data-layer]').value);
    const layer = plan.layers.find((l) => l.y <= y && (l.through_y ?? l.y) >= y);
    ctx.fillStyle = '#172e30';
    ctx.fillRect(0, 0, c.width, c.height);
    if (!layer) return;
    const colors = [
        '#d4b078',
        '#90b1b1',
        '#bf8063',
        '#e5dab5',
        '#85bda5',
        '#a39ac9',
        '#9abbdd',
        '#d3a1b1',
      ],
      symbols = Object.keys(plan.palette),
      size = Math.min(160 / plan.size[0], 160 / plan.size[1]);
    for (let z = 0; z < layer.rows.length; z++)
      for (let x = 0; x < layer.rows[z].length; x++) {
        const symbol = layer.rows[z][x];
        ctx.strokeStyle = '#3b5555';
        ctx.strokeRect(10 + x * size, 10 + z * size, size, size);
        if (symbol !== '.') {
          ctx.fillStyle = colors[symbols.indexOf(symbol) % colors.length];
          ctx.fillRect(11 + x * size, 11 + z * size, size - 2, size - 2);
        }
      }
  }
  async inventory() {
    if (this.mode === 'menu') {
      await this.tap('Escape');
      this.mode = 'released';
      this.send({ kind: 'mode', mode: 'released' });
      this.status.textContent = '背包已关闭，点击进入继续';
      return;
    }
    if (this.mode === 'released') {
      this.mode = 'look';
      this.send({ kind: 'mode', mode: 'look' });
    }
    await this.tap('KeyI');
    this.openingMenu = true;
    document.exitPointerLock();
    this.mode = 'menu';
    this.send({ kind: 'mode', mode: 'menu' });
    this.status.textContent = '背包 · 鼠标点击物品，E 继续游戏';
    this.canvas.focus();
  }
  release(message) {
    this.dx = 0;
    this.dy = 0;
    this.pointer = null;
    this.keys.clear();
    this.mode = 'released';
    this.send({ kind: 'release' });
    this.send({ kind: 'mode', mode: 'released' });
    this.status.textContent = message;
    if (document.pointerLockElement === this.canvas) document.exitPointerLock();
  }
  playAudio(message) {
    const ctx = this.audioContext;
    if (!ctx || ctx.state !== 'running' || this.mode === 'released') return;
    const bytes = Uint8Array.from(atob(message.pcm), (c) => c.charCodeAt(0));
    const data =
        message.format === 's16le' ? new Int16Array(bytes.buffer) : new Float32Array(bytes.buffer),
      scale = message.format === 's16le' ? 1 / 32768 : 1,
      frames = data.length / 2;
    const now = ctx.currentTime;
    if ((this.audioAt || 0) > now + 0.35) return;
    const buffer = ctx.createBuffer(2, frames, 24000);
    for (let ch = 0; ch < 2; ch++) {
      const output = buffer.getChannelData(ch);
      for (let i = 0; i < frames; i++) output[i] = data[i * 2 + ch] * scale;
    }
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.audioGain);
    const at = Math.max(now + 0.02, this.audioAt || 0);
    source.start(at);
    this.audioAt = at + frames / 24000;
  }
  setVolume(value) {
    this.volume = value;
    if (this.audioGain) this.audioGain.gain.value = value;
  }
  recordingStream() {
    return this.recordingDestination?.stream || null;
  }
  close() {
    this.release('已结束');
    clearInterval(this.timer);
    clearInterval(this.heartbeat);
    this.abort.abort();
    this.socket?.close();
    this.audioContext?.close();
    this.root.remove();
  }
  update(snapshot) {
    this.snapshot = snapshot;
    const state = snapshot.view.construction?.project;
    if (state && this.project?.id === state.project_id) {
      this.root.querySelector('[data-progress]').max = state.total || 1;
      this.root.querySelector('[data-progress]').value = state.completed || 0;
    }
    this.clientMenu = !!snapshot.control.client_menu;
    if (this.phase !== snapshot.phase) {
      if (snapshot.phase === 'dead') {
        this.release('点击进入后，选择 Respawn 复活');
        this.mode = 'menu';
        this.send({ kind: 'mode', mode: 'menu' });
      }
      if (this.phase === 'dead' && snapshot.phase === 'playing')
        this.release('已复活，点击进入继续');
    }
    this.phase = snapshot.phase;
  }
}
