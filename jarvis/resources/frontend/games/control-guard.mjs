export class ControlGuard {
  constructor(
    host,
    {
      clock = () => performance.now(),
      id = () => crypto.randomUUID(),
      leaseMs = 3000,
      contextTtlMs = 3000,
      maxGrantMs = 3000,
      ledgerLimit = 2048,
    } = {},
  ) {
    Object.assign(this, { host, clock, id, leaseMs, contextTtlMs, maxGrantMs, ledgerLimit });
    this.controlEpoch = 0;
    this.bridgeEpoch = id();
    this.instanceId = id();
    this.connected = false;
    this.lastHeartbeat = 0;
    this.anchor = null;
    this.contexts = new Map();
    this.grant = null;
    this.sources = new Map();
    this.commands = new Map();
    this.disabledReason = null;
    this.ownCommand = null;
    this.autoplay = null;
    this.controlReason = '';
    this.pendingCommand = null;
  }
  connect(bridgeEpoch) {
    this.revoke('reconnect');
    this.bridgeEpoch = bridgeEpoch;
    this.connected = true;
    this.lastHeartbeat = this.clock();
    this.anchor = null;
    this.contexts.clear();
  }
  disconnect() {
    this.connected = false;
    this.revoke('disconnected');
  }
  heartbeat() {
    if (this.connected) this.lastHeartbeat = this.clock();
  }
  reset(instanceId = this.id()) {
    this.revoke('instance_changed');
    this.instanceId = instanceId;
    this.anchor = null;
    this.contexts.clear();
    this.sources.clear();
    this.commands.clear();
    this.disabledReason = null;
    this.pendingCommand = null;
  }
  revoke(reason = 'user_reclaimed') {
    this.controlEpoch++;
    this.grant = null;
    this.autoplay = null;
    this.controlReason = reason;
    return { control_epoch: this.controlEpoch, reason };
  }
  nativeInput(kind) {
    if (!this.ownCommand) this.revoke('native_input:' + kind);
  }
  discardStep(grantId) {
    if (this.grant?.grantId === grantId) this.grant = null;
  }
  available() {
    if (!this.connected || this.clock() - this.lastHeartbeat > this.leaseMs) {
      if (this.connected) this.disconnect();
      return false;
    }
    if (this.autoplay && this.clock() >= this.autoplay.expires) this.revoke('time_expired');
    return !this.disabledReason && this.host.available();
  }
  autoplayState() {
    this.available();
    const run = this.autoplay;
    return run
      ? {
          autoplay_id: run.id,
          scope: run.scope,
          remaining_ms: Number.isFinite(run.expires)
            ? Math.max(0, Math.floor(run.expires - this.clock()))
            : null,
        }
      : null;
  }
  startAutoplay(message) {
    const p = message.payload,
      reject = (reason) => ({
        source_event_id: p.source_event_id,
        status: 'REJECTED',
        autoplay_id: null,
        remaining_ms: null,
        reason,
      });
    if (!this.bound(message)) return reject('stale_binding');
    if (this.sources.has(p.source_event_id)) return reject('source_consumed');
    if (this.sources.size >= this.ledgerLimit) return reject('source_ledger_full');
    this.sources.set(p.source_event_id, this.clock());
    if (!this.available() || !this.matches(p.anchor) || this.pendingCommand || this.grant)
      return reject('stale_or_unavailable');
    const context = this.contexts.get(p.anchor.snapshot_seq);
    if (this.clock() - context.createdAt >= this.contextTtlMs) return reject('context_expired');
    if (
      !['continuous', 'timed', 'level', 'task'].includes(p.scope) ||
      (p.scope === 'timed'
        ? !Number.isInteger(p.duration_ms) || p.duration_ms < 1000 || p.duration_ms > 43200000
        : p.duration_ms != null)
    )
      return reject('invalid_scope');
    this.autoplay = {
      id: this.id(),
      scope: p.scope,
      epoch: this.controlEpoch,
      kinds: p.allowed_kinds || ['PLANT', 'COLLECT_SUN'],
      expires: p.scope === 'timed' ? this.clock() + p.duration_ms : Infinity,
    };
    this.controlReason = '';
    return {
      source_event_id: p.source_event_id,
      status: 'STARTED',
      autoplay_id: this.autoplay.id,
      remaining_ms: p.duration_ms ?? null,
      reason: '',
    };
  }
  snapshot(decisionId, candidateSetId, snapshotSeq, candidates) {
    const now = this.clock();
    if (this.anchor && this.anchor.decision_id !== decisionId) this.grant = null;
    this.anchor = {
      decision_id: decisionId,
      candidate_set_id: candidateSetId,
      snapshot_seq: snapshotSeq,
      control_epoch: this.controlEpoch,
      createdAt: now,
    };
    this.contexts.set(snapshotSeq, {
      ...this.anchor,
      candidates: new Map(candidates.map((c) => [c.action_id, structuredClone(c)])),
    });
    for (const [seq, context] of this.contexts)
      if (now - context.createdAt >= this.contextTtlMs) this.contexts.delete(seq);
    while (this.contexts.size > 32) this.contexts.delete(this.contexts.keys().next().value);
    return {
      snapshot_seq: snapshotSeq,
      decision_id: decisionId,
      candidate_set_id: candidateSetId,
      control_epoch: this.controlEpoch,
    };
  }
  bound(message) {
    return (
      message.game_instance_id === this.instanceId && message.bridge_epoch === this.bridgeEpoch
    );
  }
  matches(anchor) {
    const context = anchor && this.contexts.get(anchor.snapshot_seq);
    return (
      context &&
      this.anchor &&
      anchor.decision_id === this.anchor.decision_id &&
      anchor.decision_id === context.decision_id &&
      anchor.candidate_set_id === context.candidate_set_id &&
      anchor.control_epoch === context.control_epoch &&
      anchor.control_epoch === this.controlEpoch
    );
  }
  arm(message) {
    const p = message.payload;
    const reject = (reason) => ({
      source_event_id: p.source_event_id,
      status: 'REJECTED',
      reason,
      remaining_ms: 0,
      grant_id: null,
    });
    if (!this.bound(message)) return reject('stale_binding');
    const ongoing = message.type === 'arm_autoplay_step';
    this.available();
    if (
      ongoing &&
      (!this.autoplay ||
        p.autoplay_id !== this.autoplay.id ||
        this.autoplay.epoch !== this.controlEpoch)
    )
      return reject('autoplay_ended');
    if (ongoing && p.allowed_kinds?.some((k) => !this.autoplay.kinds.includes(k)))
      return reject('outside_task_scope');
    if (!ongoing && this.autoplay) return reject('autoplay_active');
    if (this.sources.has(p.source_event_id)) return reject('source_consumed');
    if (this.sources.size >= this.ledgerLimit) {
      this.disabledReason = 'source_ledger_full';
      return reject(this.disabledReason);
    }
    this.sources.set(p.source_event_id, this.clock());
    if (!this.available() || !this.matches(p.anchor)) return reject('stale_or_unavailable');
    if (this.grant || this.pendingCommand) return reject('grant_already_pending');
    if (
      !Number.isInteger(p.ttl_ms) ||
      p.ttl_ms <= 0 ||
      p.ttl_ms > 15000 ||
      !Array.isArray(p.allowed_kinds)
    )
      return reject('bad_grant');
    const context = this.contexts.get(p.anchor.snapshot_seq);
    const now = this.clock(),
      expires = Math.min(
        now + Math.min(p.ttl_ms, this.maxGrantMs),
        context.createdAt + this.contextTtlMs,
        ongoing ? this.autoplay.expires : Infinity,
      );
    if (expires <= now) return reject('context_expired');
    this.grant = {
      grantId: this.id(),
      source: p.source_event_id,
      expires,
      epoch: this.controlEpoch,
      decision: context.decision_id,
      set: context.candidate_set_id,
      context,
      kinds: [...p.allowed_kinds],
      autoplayId: ongoing ? this.autoplay.id : null,
    };
    return {
      source_event_id: p.source_event_id,
      status: 'ARMED',
      grant_id: this.grant.grantId,
      reason: '',
      remaining_ms: Math.floor(expires - now),
    };
  }
  submit(message) {
    const p = message.payload;
    if (!this.bound(message))
      return {
        command_id: p.command_id,
        status: 'REJECTED',
        action_id: p.action_id,
        reason: 'stale_binding',
      };
    if (this.commands.has(p.command_id)) return structuredClone(this.commands.get(p.command_id));
    const result = {
      command_id: p.command_id,
      action_id: p.action_id,
      status: 'REJECTED',
      reason: '',
    };
    const finish = (status, reason, extra = {}) => {
      Object.assign(result, { status, reason }, extra);
      this.commands.set(p.command_id, structuredClone(result));
      return result;
    };
    if (this.commands.size >= this.ledgerLimit) {
      this.disabledReason = 'command_ledger_full';
      this.revoke(this.disabledReason);
      return { ...result, reason: this.disabledReason };
    }
    const grant = this.grant;
    this.grant = null;
    if (
      !grant ||
      grant.grantId !== p.grant_id ||
      grant.epoch !== this.controlEpoch ||
      this.clock() >= grant.expires
    )
      return finish('REJECTED', 'expired_or_revoked');
    if (
      !this.available() ||
      !this.matches(p.anchor) ||
      grant.decision !== p.anchor.decision_id ||
      grant.set !== p.anchor.candidate_set_id
    )
      return finish('REJECTED', 'context_changed');
    if (grant.autoplayId && this.autoplay?.id !== grant.autoplayId)
      return finish('REJECTED', 'autoplay_ended');
    const candidate = grant.context.candidates.get(p.action_id);
    if (!candidate || !grant.kinds.includes(candidate.kind))
      return finish('REJECTED', 'illegal_candidate');
    if (candidate.expiresAt <= this.clock()) return finish('REJECTED', 'candidate_expired');
    if (!this.host.validate(candidate)) return finish('REJECTED', 'local_precondition_changed');

    this.commands.set(p.command_id, { ...result, status: 'ACCEPTED', reason: 'reserved' });
    this.ownCommand = p.command_id;
    try {
      const evidence = this.host.execute(candidate, p.command_id);
      if (evidence && typeof evidence.then === 'function')
        throw new Error('execute must be synchronous');
      if (evidence?.pending) {
        this.pendingCommand = p.command_id;
        return finish('ACCEPTED', 'normal_animation_pending');
      }
      if (!evidence || !evidence.confirmed) {
        this.disabledReason = 'action_outcome_unknown';
        this.revoke(this.disabledReason);
        return finish('UNKNOWN', this.disabledReason);
      }
      this.anchor = null;
      this.contexts.clear();
      return finish(
        evidence.requiresHumanContinuation ? 'CONFIRMED_REQUIRES_HUMAN_CONTINUATION' : 'CONFIRMED',
        '',
        { confirmed_event_id: evidence.eventId },
      );
    } catch {
      this.disabledReason = 'action_outcome_unknown';
      this.revoke(this.disabledReason);
      return finish('UNKNOWN', this.disabledReason);
    } finally {
      this.ownCommand = null;
    }
  }
  complete(commandId, evidence) {
    const current = this.commands.get(commandId);
    if (!current || current.status !== 'ACCEPTED') return current;
    const result = {
      ...current,
      status: evidence?.confirmed ? 'CONFIRMED' : 'UNKNOWN',
      reason: evidence?.confirmed ? '' : 'action_outcome_unknown',
      confirmed_event_id: evidence?.eventId ?? null,
    };
    this.commands.set(commandId, result);
    this.pendingCommand = null;
    this.anchor = null;
    this.contexts.clear();
    if (!evidence?.confirmed) {
      this.disabledReason = 'action_outcome_unknown';
      this.revoke(this.disabledReason);
    }
    return structuredClone(result);
  }
  status(commandId) {
    return structuredClone(
      this.commands.get(commandId) || {
        command_id: commandId,
        status: 'UNKNOWN',
        reason: 'no_record',
      },
    );
  }
  observedFresh() {
    if (this.disabledReason === 'action_outcome_unknown') {
      this.disabledReason = null;
      this.revoke('fresh_after_unknown');
    }
  }
}
