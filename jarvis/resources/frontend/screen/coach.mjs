export function coachPanel(root, event) {
  const text = (tag, value) => {
    const node = document.createElement(tag);
    node.textContent = value;
    return node;
  };
  const { type, payload = {} } = event;
  if (['screen.capture.started', 'screen.capture.stopped', 'history.cleared'].includes(type)) {
    root.replaceChildren();
    delete root.dataset.pending;
    return;
  }
  if (type === 'screen.coach.started') {
    root.replaceChildren(text('p', '正在比较方案，可以继续说话。'));
    root.dataset.pending = 'true';
    return;
  }
  if (type === 'screen.coach.result') {
    delete root.dataset.pending;
    const card = document.createElement('article');
    const advice = payload.advice;
    card.append(
      text('h2', advice.clarification ? '需要确认' : '基于刚才画面的选项'),
      text('p', payload.request),
    );
    if (advice.clarification) card.append(text('p', advice.clarification));
    for (const option of advice.alternatives) {
      card.append(text('h3', option.label), text('p', option.suggestion));
      const conditions = document.createElement('ul');
      for (const condition of option.conditions) conditions.append(text('li', condition));
      card.append(text('p', '需要先确认：'), conditions, text('p', option.tradeoff));
    }
    root.replaceChildren(card);
    return;
  }
  if (
    root.dataset.pending &&
    ['screen.coach.discarded', 'screen.analysis.cancelled', 'screen.analysis.failed'].includes(type)
  ) {
    root.replaceChildren(text('p', '这次分析已停止，可以重新提问。'));
    delete root.dataset.pending;
  }
}
