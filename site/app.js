'use strict';

const percent = value => (value * 100).toFixed(1);
const agentOrder = ['Brackett', 'Codex', 'Claude'];
let resultData;
let cohort = 'shared';

function showResults() {
  const agents = cohort === 'shared' ? resultData.shared_agents : resultData.agents;
  const chart = document.getElementById('bar-chart');
  [...agentOrder].sort((a, b) => agents[b].average_score - agents[a].average_score).forEach(agent => {
    const stats = agents[agent];
    const row = chart.querySelector(`[data-agent="${agent}"]`);
    row.querySelector('.bar-detail').textContent = `${stats.completed_runs} / ${stats.attempted_runs} completed attempts`;
    row.querySelector('.bar-fill').style.width = `${stats.average_score * 100}%`;
    const value = row.querySelector('.bar-value');
    value.replaceChildren(document.createTextNode(percent(stats.average_score)));
    const unit = document.createElement('span');
    unit.textContent = '%';
    value.append(unit);
    chart.insertBefore(row, chart.querySelector('.axis-row'));
  });
  const context = cohort === 'shared'
    ? `${resultData.shared_cases.length} cases attempted by all three systems · equal weight per case`
    : `${resultData.tested_cases.length} cases tested by at least one system · coverage differs by system`;
  document.getElementById('chart-context').textContent = context;
  document.getElementById('result-announcement').textContent = `${context}. ${agentOrder.map(a => `${a}: ${percent(agents[a].average_score)} percent`).join('. ')}.`;
  document.querySelectorAll('[data-cohort]').forEach(button => {
    const active = button.dataset.cohort === cohort;
    button.setAttribute('aria-pressed', String(active));
    button.classList.toggle('selected', active);
  });
}

function showCases() {
  const query = document.getElementById('case-search').value.trim().normalize('NFKC').toLocaleLowerCase();
  const scope = document.getElementById('case-scope').value;
  const cases = (scope === 'shared' ? resultData.shared_cases : resultData.tested_cases)
    .filter(name => name.normalize('NFKC').toLocaleLowerCase().includes(query))
    .slice().sort((a, b) => a.localeCompare(b));
  const tbody = document.getElementById('case-rows');
  tbody.replaceChildren();
  for (const name of cases) {
    const row = document.createElement('tr');
    const label = document.createElement('td');
    label.textContent = name;
    row.append(label);
    for (const agent of agentOrder) {
      const cell = document.createElement('td');
      const value = resultData.case_scores[agent][name];
      cell.textContent = value === undefined ? '—' : `${percent(value)}%`;
      if (value === undefined) cell.setAttribute('aria-label', 'Untested');
      row.append(cell);
    }
    tbody.append(row);
  }
  if (!cases.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 4;
    cell.textContent = 'No matching process. Try a different name or include all tested cases.';
    row.append(cell);
    tbody.append(row);
  }
  document.getElementById('case-count').textContent = `${cases.length} ${cases.length === 1 ? 'process' : 'processes'} · mean comprehension score across selected attempts`;
}

async function loadResults() {
  const buttons = document.querySelectorAll('[data-cohort]');
  buttons.forEach(b => { b.disabled = true; });
  try {
    const response = await fetch('data/results.json');
    if (!response.ok) throw new Error(`Results unavailable (${response.status})`);
    resultData = await response.json();
    showResults();
    showCases();
    buttons.forEach(button => {
      button.disabled = false;
      button.addEventListener('click', () => { cohort = button.dataset.cohort; showResults(); });
    });
    document.getElementById('case-search').addEventListener('input', showCases);
    document.getElementById('case-scope').addEventListener('change', showCases);
  } catch (error) {
    document.getElementById('case-count').textContent = 'Interactive data could not load. The chart shows the saved shared-case snapshot; download the CSV to see every attempt.';
    document.getElementById('case-search').disabled = true;
    document.getElementById('case-scope').disabled = true;
    buttons.forEach(button => { button.title = 'Interactive data unavailable; use the CSV download.'; });
  }
}

const tabs = [...document.querySelectorAll('[role="tab"]')];
function selectQuestion(tab, moveFocus = false) {
  tabs.forEach(item => {
    const selected = item === tab;
    item.setAttribute('aria-selected', String(selected));
    item.tabIndex = selected ? 0 : -1;
    document.getElementById(item.getAttribute('aria-controls')).hidden = !selected;
  });
  if (moveFocus) tab.focus();
}
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => selectQuestion(tab));
  tab.addEventListener('keydown', event => {
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    if (next === undefined) return;
    event.preventDefault();
    selectQuestion(tabs[next], true);
  });
});

document.querySelectorAll('a[href="#result-method"]').forEach(link => {
  link.addEventListener('click', () => { document.getElementById('result-method').open = true; });
});

document.querySelectorAll('[data-video-time]').forEach(button => {
  button.addEventListener('click', () => {
    const video = document.getElementById('demo-video');
    const time = Number(button.dataset.videoTime);
    const seekAndPlay = () => {
      video.currentTime = time;
      video.play().catch(() => { /* The native controls remain available if autoplay is blocked. */ });
    };
    if (video.readyState >= 1) seekAndPlay();
    else {
      video.addEventListener('loadedmetadata', seekAndPlay, { once: true });
      video.load();
    }
    video.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
    video.focus({ preventScroll: true });
  });
});

loadResults();
