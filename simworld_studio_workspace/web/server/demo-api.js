'use strict';

const fs = require('fs');
const net = require('net');
const path = require('path');
const { spawn } = require('child_process');

const { SkillRegistry } = require('./skills');
const { LearnedToolStore } = require('./learned-tools-store');
const { EvolutionManager, readEvolutionConfig, writeEvolutionConfig } = require('./evolution');
const { selectSkillsWithClaude } = require('./skill-selector');

const NL = String.fromCharCode(10);
const DEFAULT_PRESET_FILE = path.join(__dirname, 'demo-presets.json');
const DEMO_CAMERA = {
  location: [-11884.017, 223.891, 24081.935],
  rotation: [-69.6, 0.0, 0.0], // pitch, yaw, roll
};

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function nowIso() {
  return new Date().toISOString();
}

function safeJsonParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function extractTextFromToolResultContent(content) {
  if (Array.isArray(content)) {
    return content
      .map((c) => (c && typeof c === 'object' && typeof c.text === 'string' ? c.text : ''))
      .join('');
  }
  return String(content || '');
}

function findPngPath(text) {
  const m = String(text || '').match(/([/][\w/\-._]+\.png)/);
  return m ? m[1] : null;
}

function encodeScreenshotPath(filePath) {
  if (!filePath) return null;
  return `/api/screenshot/file?path=${encodeURIComponent(filePath)}`;
}

function countLearnedSkills(skillRegistry) {
  return skillRegistry
    .list()
    .filter((s) => s.source === 'custom' && Array.isArray(s.tags) && s.tags.includes('learned')).length;
}

function parseNumber(value) {
  if (value == null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function parseUsageFromResultEvent(ev) {
  if (!ev || typeof ev !== 'object') {
    return { inputTokens: null, outputTokens: null, totalTokens: null };
  }

  const usage = ev.usage && typeof ev.usage === 'object' ? ev.usage : null;
  const inputTokens = parseNumber(
    (usage && (usage.input_tokens ?? usage.prompt_tokens)) ??
      ev.input_tokens ??
      ev.prompt_tokens
  );
  const outputTokens = parseNumber(
    (usage && (usage.output_tokens ?? usage.completion_tokens)) ??
      ev.output_tokens ??
      ev.completion_tokens
  );
  const totalTokens = parseNumber(
    (usage && usage.total_tokens) ??
      ev.total_tokens ??
      (inputTokens != null && outputTokens != null ? inputTokens + outputTokens : null)
  );

  return {
    inputTokens,
    outputTokens,
    totalTokens,
  };
}

function snapshotLearnedTools(learnedToolStore) {
  const out = new Map();
  for (const t of learnedToolStore.list({ includeArchived: true })) {
    if (!t || !t.id) continue;
    out.set(String(t.id), Number(t.version || 1));
  }
  return out;
}

function snapshotLearnedSkills(skillRegistry) {
  const out = new Map();
  for (const s of skillRegistry.list()) {
    const learned = s && s.source === 'custom' && Array.isArray(s.tags) && s.tags.includes('learned');
    if (!learned || !s.id) continue;
    out.set(String(s.id), String(s.version || '1.0.0'));
  }
  return out;
}

function countCreatedAndUpdated(beforeMap, afterMap) {
  let created = 0;
  let updated = 0;

  for (const [id, afterVersion] of afterMap.entries()) {
    if (!beforeMap.has(id)) {
      created += 1;
      continue;
    }
    const beforeVersion = beforeMap.get(id);
    if (String(afterVersion) !== String(beforeVersion)) {
      updated += 1;
    }
  }

  return { created, updated };
}

function readJsonFile(filePath, fallback) {
  try {
    const parsed = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
    return parsed == null ? fallback : parsed;
  } catch {
    return fallback;
  }
}

function writeJsonFile(filePath, value) {
  ensureDir(path.dirname(filePath));
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}${NL}`, 'utf-8');
}

function loadPresets(presetFile) {
  const fromFile = readJsonFile(presetFile, null);
  if (Array.isArray(fromFile) && fromFile.length > 0) {
    return fromFile
      .map((p) => ({
        id: String(p.id || '').trim(),
        name: String(p.name || p.id || '').trim(),
        description: String(p.description || ''),
        queries: Array.isArray(p.queries) ? p.queries.map((q) => String(q || '').trim()).filter(Boolean) : [],
      }))
      .filter((p) => p.id && p.queries.length > 0);
  }
  return [];
}

function createRunId() {
  return `demo_${new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14)}_${Math.random().toString(36).slice(2, 8)}`;
}

function createSseWriter(res) {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders();

  const write = (event, data) => {
    if (res.writableEnded) return;
    const payload = data == null ? {} : data;
    res.write(`event: ${event}${NL}data: ${JSON.stringify(payload)}${NL}${NL}`);
  };

  return { write };
}

function runUnrealCommand(host, port, type, params, timeoutMs) {
  return new Promise((resolve, reject) => {
    const sock = new net.Socket();
    const timer = setTimeout(() => {
      try { sock.destroy(); } catch {}
      reject(new Error(`UE command timeout after ${timeoutMs}ms (${type})`));
    }, timeoutMs);

    let buffer = '';

    sock.connect(port, host, () => {
      const payload = JSON.stringify({
        type: String(type || ''),
        params: params || {},
      });
      sock.write(`${payload}${NL}`);
    });

    sock.on('data', (chunk) => {
      buffer += chunk.toString();
      try {
        const parsed = JSON.parse(buffer);
        clearTimeout(timer);
        try { sock.destroy(); } catch {}
        resolve(parsed);
      } catch {
        // wait for full frame
      }
    });

    sock.on('error', (err) => {
      clearTimeout(timer);
      try { sock.destroy(); } catch {}
      reject(err);
    });
  });
}

function runUnrealPython(host, port, script, timeoutMs) {
  return runUnrealCommand(host, port, 'execute_python_script', { script: String(script || '') }, timeoutMs);
}

async function setDemoCamera(host, port) {
  const loc = DEMO_CAMERA.location;
  const rot = DEMO_CAMERA.rotation;
  const script = [
    'import unreal',
    'subsys = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)',
    `loc = unreal.Vector(${loc[0]}, ${loc[1]}, ${loc[2]})`,
    `rot = unreal.Rotator(${rot[0]}, ${rot[1]}, ${rot[2]})`,
    'subsys.set_level_viewport_camera_info(loc, rot)',
    'print("DEMO_CAMERA_SET")',
  ].join(NL);

  return runUnrealPython(host, port, script, 8000);
}

async function captureDemoRoundScreenshot(host, port, laneDir, laneId, round) {
  const shotsDir = path.join(laneDir, 'screenshots');
  ensureDir(shotsDir);
  const filename = `round_${String(round)}_${String(laneId)}.png`;
  const filePath = path.join(shotsDir, filename);
  await runUnrealCommand(host, port, 'take_screenshot', { filepath: filePath }, 15000);
  if (!fs.existsSync(filePath)) {
    throw new Error(`Screenshot command returned but file missing: ${filePath}`);
  }
  return filePath;
}

function createDemoStore(arenaRoot, presetFilePath) {
  const demoRoot = path.join(arenaRoot, 'arena_data', 'demo');
  const runsRoot = path.join(demoRoot, 'runs');
  const runsIndexPath = path.join(demoRoot, 'runs_index.json');
  const presetsPath = presetFilePath || DEFAULT_PRESET_FILE;

  ensureDir(demoRoot);
  ensureDir(runsRoot);
  if (!fs.existsSync(runsIndexPath)) writeJsonFile(runsIndexPath, []);

  function getPresets() {
    const presets = loadPresets(presetsPath);
    return presets.length > 0 ? presets : [];
  }

  function getPresetById(id) {
    const target = String(id || '').trim();
    return getPresets().find((p) => p.id === target) || null;
  }

  function listRuns(limit) {
    const rows = readJsonFile(runsIndexPath, []);
    const arr = Array.isArray(rows) ? rows : [];
    const lim = Math.max(1, Number(limit || 50));
    return arr.slice(0, lim);
  }

  function saveRunSummary(summary) {
    const rows = readJsonFile(runsIndexPath, []);
    const arr = Array.isArray(rows) ? rows : [];
    const idx = arr.findIndex((r) => r && r.id === summary.id);
    if (idx >= 0) arr[idx] = summary;
    else arr.unshift(summary);
    writeJsonFile(runsIndexPath, arr.slice(0, 500));
  }

  function runDir(runId) {
    return path.join(runsRoot, runId);
  }

  function runJsonPath(runId) {
    return path.join(runDir(runId), 'run.json');
  }

  function saveRunRecord(runId, record) {
    writeJsonFile(runJsonPath(runId), record);
  }

  function loadRunRecord(runId) {
    const p = runJsonPath(runId);
    if (!fs.existsSync(p)) return null;
    return readJsonFile(p, null);
  }

  return {
    demoRoot,
    runsRoot,
    runsIndexPath,
    getPresets,
    getPresetById,
    listRuns,
    saveRunSummary,
    runDir,
    saveRunRecord,
    loadRunRecord,
  };
}

function writeLaneMcpConfig(laneDir, laneLearnedToolsPath, opts) {
  const mcpConfigPath = path.join(laneDir, 'mcp.json');
  const config = {
    mcpServers: {
      simworld: {
        command: 'node',
        args: [path.resolve(__dirname, 'mcp-server.js')],
        env: {
          UNREAL_HOST: String(opts.unrealHost || process.env.UNREAL_HOST || '127.0.0.1'),
          UNREAL_PORT: String(opts.unrealPort || process.env.UNREAL_PORT || '55559'),
          LEARNED_TOOLS_FILE: laneLearnedToolsPath,
          PORT: String(opts.port || process.env.PORT || '3002'),
        },
      },
    },
  };
  writeJsonFile(mcpConfigPath, config);
  return mcpConfigPath;
}

function buildSystemPrompt(basePrompt, skillRegistry, prompt) {
  const learnedSkillIds = skillRegistry.retrieveForPrompt(prompt, {
    limit: 3,
    threshold: 0.25,
    onlyLearned: true,
  });

  let promptText = basePrompt;
  if (learnedSkillIds.length > 0) {
    const docs = skillRegistry.compose(learnedSkillIds);
    if (docs) {
      promptText += `${NL}${NL}## ACTIVE SKILLS (reference documentation)${NL}${docs}`;
    }
  }

  return {
    promptText,
    mergedSkillIds: learnedSkillIds,
  };
}

async function selectSkillsForDemoRound(opts) {
  const {
    prompt,
    skillRegistry,
    claudeBin,
    model,
    emitEvent,
    eventBase,
  } = opts;

  emitEvent('skill_selection_start', {
    ...(eventBase || {}),
    mode: 'auto',
    availableSkills: skillRegistry.list().length,
  });

  let selectedSkillsBySelector = [];
  let selectorReasoning = '';
  let selectorError = '';

  try {
    const selectorResult = await selectSkillsWithClaude({
      prompt,
      skillRegistry,
      claudeBin,
      model: process.env.CLAUDE_SKILL_SELECTOR_MODEL || model || null,
      timeoutMs: Number(process.env.CLAUDE_SKILL_SELECTOR_TIMEOUT_MS || 45000),
    });
    selectedSkillsBySelector = Array.isArray(selectorResult.selectedSkillIds)
      ? selectorResult.selectedSkillIds.map((s) => String(s || '').trim()).filter(Boolean)
      : [];
    selectorReasoning = String(selectorResult.reasoning || '');
  } catch (err) {
    selectorError = err && err.message ? err.message : String(err);
  }

  const mergedSkills = [...selectedSkillsBySelector];
  const learnedSkillIds = skillRegistry.retrieveForPrompt(prompt, {
    limit: 3,
    threshold: 0.25,
    onlyLearned: true,
  });
  for (const sid of learnedSkillIds) {
    if (!mergedSkills.includes(sid)) mergedSkills.push(sid);
  }

  if (selectorError) {
    emitEvent('skill_selection_error', {
      ...(eventBase || {}),
      mode: 'auto',
      message: selectorError,
    });
  }

  emitEvent('skill_selection_done', {
    ...(eventBase || {}),
    mode: 'auto',
    selectedSkills: mergedSkills,
    reasoning: selectorReasoning,
    fallbackUsed: Boolean(selectorError),
  });

  return {
    mergedSkills,
  };
}

async function runClaudeRound(opts) {
  const {
    claudeBin,
    prompt,
    sessionId,
    laneRawPath,
    mcpConfigPath,
    baseSystemPrompt,
    skillRegistry,
    model,
    emitEvent,
    eventBase,
  } = opts;

  ensureDir(path.dirname(laneRawPath));
  fs.writeFileSync(laneRawPath, '', 'utf-8');

  const selection = await selectSkillsForDemoRound({
    prompt,
    skillRegistry,
    claudeBin,
    model,
    emitEvent,
    eventBase,
  });

  let promptText = baseSystemPrompt;
  if (selection.mergedSkills && selection.mergedSkills.length > 0) {
    const docs = skillRegistry.compose(selection.mergedSkills);
    if (docs) {
      promptText += `${NL}${NL}## ACTIVE SKILLS (reference documentation)${NL}${docs}`;
    }
  }

  const args = [
    '-p',
    prompt,
    '--output-format',
    'stream-json',
    '--include-partial-messages',
    '--verbose',
    '--dangerously-skip-permissions',
    '--mcp-config',
    mcpConfigPath,
    '--append-system-prompt',
    promptText,
  ];

  if (sessionId) {
    args.push('--resume', sessionId);
  }

  if (model) {
    args.push('--model', model);
  }

  const env = { ...process.env };
  delete env.CLAUDECODE;
  delete env.CLAUDE_SESSION_ID;
  delete env.CLAUDE_CODE_ENTRYPOINT;

  return new Promise((resolve, reject) => {
    const proc = spawn(claudeBin, args, {
      cwd: path.resolve(__dirname, '..'),
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let stdoutBuffer = '';
    let stderrText = '';
    let resolvedSessionId = sessionId || null;
    let resultIsError = false;
    let resultCost = null;
    let resultSubtype = null;
    let resultText = '';
    let usage = {
      inputTokens: null,
      outputTokens: null,
      totalTokens: null,
    };
    let latestScreenshot = null;
    let selectedModel = null;

    function appendRaw(line) {
      fs.appendFileSync(laneRawPath, `${line}${NL}`, 'utf-8');
    }

    function handleLine(line) {
      const text = String(line || '').trim();
      if (!text) return;
      appendRaw(text);
      const ev = safeJsonParse(text);
      if (!ev || typeof ev !== 'object') return;

      if (ev.type === 'system' && ev.subtype === 'init') {
        if (ev.session_id) resolvedSessionId = ev.session_id;
        if (ev.model) selectedModel = ev.model;
        return;
      }

      if (ev.type === 'assistant') {
        const content = ev.message && Array.isArray(ev.message.content) ? ev.message.content : [];
        for (const block of content) {
          if (block.type === 'text' && block.text) {
            resultText += block.text;
          }
          if (ev.message && ev.message.model && ev.message.model !== '<synthetic>' && !selectedModel) {
            selectedModel = ev.message.model;
          }
        }
        return;
      }

      if (ev.type === 'user') {
        const content = ev.message && Array.isArray(ev.message.content) ? ev.message.content : [];
        for (const block of content) {
          if (!block || block.type !== 'tool_result') continue;
          const resultTextChunk = extractTextFromToolResultContent(block.content);
          const pngPath = findPngPath(resultTextChunk);
          if (pngPath && fs.existsSync(pngPath)) {
            latestScreenshot = pngPath;
          }
        }
        return;
      }

      if (ev.type === 'result') {
        if (ev.session_id) resolvedSessionId = ev.session_id;
        resultIsError = Boolean(ev.is_error || ev.subtype === 'error_during_turn');
        resultCost = ev.total_cost_usd == null ? null : Number(ev.total_cost_usd);
        resultSubtype = ev.subtype || null;
        usage = parseUsageFromResultEvent(ev);
        if (typeof ev.result === 'string' && ev.result) {
          resultText += `${ev.result}${NL}`;
        }
      }
    }

    proc.stdout.on('data', (chunk) => {
      stdoutBuffer += chunk.toString();
      const lines = stdoutBuffer.split(NL);
      stdoutBuffer = lines.pop() || '';
      for (const line of lines) handleLine(line);
    });

    proc.stderr.on('data', (chunk) => {
      stderrText += chunk.toString();
    });

    proc.on('error', (err) => {
      reject(err);
    });

    proc.on('close', (code) => {
      if (stdoutBuffer.trim()) {
        handleLine(stdoutBuffer.trim());
      }
      resolve({
        exitCode: code,
        sessionId: resolvedSessionId,
        isError: resultIsError || code !== 0,
        costUsd: resultCost,
        subtype: resultSubtype,
        text: resultText,
        latestScreenshot,
        latestScreenshotApiPath: encodeScreenshotPath(latestScreenshot),
        rawLogPath: laneRawPath,
        mergedSkillIds: selection.mergedSkills || [],
        stderr: stderrText,
        model: selectedModel,
        usage,
      });
    });
  });
}

async function waitForEvolutionDrain(evolutionManager, emitEvent, payloadBase) {
  const waitStartMs = Date.now();
  emitEvent('evolution_wait_start', {
    ...payloadBase,
    phase: 'waiting_for_evolution',
    message: 'waiting for evolution to finish',
  });

  let stableSince = 0;
  let lastProgress = 0;

  for (;;) {
    const st = evolutionManager.getStatus(20);
    const now = Date.now();
    const isDrained = st.processing === false && Number(st.queueLength || 0) === 0;

    if (now - lastProgress >= 1000) {
      emitEvent('evolution_wait_progress', {
        ...payloadBase,
        phase: 'waiting_for_evolution',
        queueLength: st.queueLength,
        pendingCount: st.pendingCount,
        processing: st.processing,
      });
      lastProgress = now;
    }

    if (isDrained) {
      if (!stableSince) stableSince = now;
      if (now - stableSince >= 1200) break;
    } else {
      stableSince = 0;
    }

    await new Promise((r) => setTimeout(r, 500));
  }

  emitEvent('evolution_wait_done', {
    ...payloadBase,
    phase: 'evolution_completed',
    message: 'evolution queue drained',
    waitDurationMs: Date.now() - waitStartMs,
  });

  return {
    waitDurationMs: Date.now() - waitStartMs,
  };
}

async function runDemoLane(opts) {
  const {
    laneId,
    runId,
    runDir,
    queries,
    emitEvent,
    claudeBin,
    baseSystemPrompt,
    arenaRoot,
    model,
    unrealHost,
    unrealPort,
    port,
  } = opts;

  const laneDir = path.join(runDir, laneId);
  const laneArenaDataDir = path.join(laneDir, 'arena_data');
  const laneSkillsDir = path.join(laneDir, 'skills');
  const laneLogsDir = path.join(laneDir, 'logs');
  const laneRawDir = path.join(laneDir, 'raw');
  const laneLearnedToolsPath = path.join(laneArenaDataDir, 'learned_tools.json');

  ensureDir(laneDir);
  ensureDir(laneArenaDataDir);
  ensureDir(laneSkillsDir);
  ensureDir(laneLogsDir);
  ensureDir(laneRawDir);
  fs.writeFileSync(laneLearnedToolsPath, '[]\n', 'utf-8');

  const mcpConfigPath = writeLaneMcpConfig(laneDir, laneLearnedToolsPath, {
    unrealHost,
    unrealPort,
    port,
  });

  const skillRegistry = new SkillRegistry({
    customDir: laneSkillsDir,
  });

  const learnedToolStore = new LearnedToolStore({
    filePath: laneLearnedToolsPath,
  });

  const evolutionEnabled = laneId === 'evolution';
  const evolutionManager = evolutionEnabled
    ? new EvolutionManager({
      skillRegistry,
      learnedToolStore,
      claudeBin,
      logsDir: laneLogsDir,
      arenaDataDir: laneArenaDataDir,
      skillsDir: laneSkillsDir,
      autoSweepEnabled: true,
      autoSweepIntervalMs: 4000,
    })
    : null;

  const laneRecord = {
    id: laneId,
    status: 'running',
    evolutionEnabled,
    startedAt: nowIso(),
    finishedAt: null,
    laneDir,
    arenaDataDir: laneArenaDataDir,
    skillsDir: laneSkillsDir,
    logsDir: laneLogsDir,
    rawDir: laneRawDir,
    learnedToolsFile: laneLearnedToolsPath,
    mcpConfigPath,
    rounds: [],
    summary: null,
    metrics: {
      laneStartMs: Date.now(),
      laneEndMs: null,
      laneDurationMs: null,
      roundDurationsMs: [],
      evolutionWaitMs: 0,
      tokens: {
        input: 0,
        output: 0,
        total: 0,
      },
      costUsd: 0,
      artifacts: {
        learnedToolsCreated: 0,
        learnedToolsUpdated: 0,
        learnedSkillsCreated: 0,
        learnedSkillsUpdated: 0,
      },
    },
  };

  let currentSessionId = null;
  const toolsBefore = snapshotLearnedTools(learnedToolStore);
  const skillsBefore = snapshotLearnedSkills(skillRegistry);

  emitEvent('lane_started', {
    runId,
    lane: laneId,
    evolutionEnabled,
    startedAt: laneRecord.startedAt,
  });

  try {
    for (let idx = 0; idx < queries.length; idx += 1) {
      const round = idx + 1;
      const prompt = queries[idx];

      if (evolutionEnabled && round === 5) {
        const barrier = await waitForEvolutionDrain(evolutionManager, emitEvent, { runId, lane: laneId, round });
        laneRecord.metrics.evolutionWaitMs += Number(barrier.waitDurationMs || 0);
      }

      emitEvent('round_started', {
        runId,
        lane: laneId,
        round,
        totalRounds: queries.length,
        prompt,
      });

      const rawRoundPath = path.join(laneRawDir, `round_${round}.jsonl`);
      const roundStartMs = Date.now();
      let evolutionPromptSession = null;

      if (evolutionEnabled) {
        evolutionPromptSession = evolutionManager.queueFromPrompt({
          sessionId: currentSessionId,
          prompt,
          skills: [],
          model: model || null,
          source: 'demo_prompt',
          trigger: 'demo_round_start',
        });
      }

      const turn = await runClaudeRound({
        claudeBin,
        prompt,
        sessionId: currentSessionId,
        laneRawPath: rawRoundPath,
        mcpConfigPath,
        baseSystemPrompt,
        skillRegistry,
        model,
        emitEvent,
        eventBase: { runId, lane: laneId, round, totalRounds: queries.length },
      });

      currentSessionId = turn.sessionId || currentSessionId;

      if (evolutionEnabled && !turn.isError) {
        evolutionManager.queueFromChat({
          evolutionSessionId: evolutionPromptSession && evolutionPromptSession.id ? evolutionPromptSession.id : null,
          sessionId: currentSessionId,
          prompt,
          skills: turn.mergedSkillIds,
          result: {
            isError: false,
            screenshot: turn.latestScreenshotApiPath,
          },
          rawLogPath: turn.rawLogPath,
          model: turn.model || model || null,
        });
      }

      const roundRecord = {
        round,
        prompt,
        sessionId: currentSessionId,
        startedAt: new Date(roundStartMs).toISOString(),
        finishedAt: nowIso(),
        durationMs: Date.now() - roundStartMs,
        isError: Boolean(turn.isError),
        exitCode: turn.exitCode,
        latestScreenshot: turn.latestScreenshotApiPath,
        rawLogPath: rawRoundPath,
        mergedSkillIds: turn.mergedSkillIds,
        costUsd: turn.costUsd,
        usage: {
          inputTokens: turn.usage && turn.usage.inputTokens != null ? Number(turn.usage.inputTokens) : null,
          outputTokens: turn.usage && turn.usage.outputTokens != null ? Number(turn.usage.outputTokens) : null,
          totalTokens: turn.usage && turn.usage.totalTokens != null ? Number(turn.usage.totalTokens) : null,
        },
      };

      // Guarantee one round screenshot per lane for visual comparison.
      try {
        const forcedShotPath = await captureDemoRoundScreenshot(
          unrealHost,
          Number(unrealPort),
          laneDir,
          laneId,
          round
        );
        roundRecord.latestScreenshot = encodeScreenshotPath(forcedShotPath);
        roundRecord.roundScreenshot = {
          path: forcedShotPath,
          apiPath: roundRecord.latestScreenshot,
          source: 'forced_round_capture',
        };
        emitEvent('round_screenshot', {
          runId,
          lane: laneId,
          round,
          path: roundRecord.latestScreenshot,
        });
      } catch (sErr) {
        roundRecord.roundScreenshot = {
          path: null,
          apiPath: roundRecord.latestScreenshot || null,
          source: roundRecord.latestScreenshot ? 'model_or_tool_capture' : 'none',
          error: sErr && sErr.message ? sErr.message : String(sErr),
        };
        emitEvent('round_screenshot_failed', {
          runId,
          lane: laneId,
          round,
          message: roundRecord.roundScreenshot.error,
        });
      }

      laneRecord.rounds.push(roundRecord);
      laneRecord.metrics.roundDurationsMs.push(roundRecord.durationMs);
      laneRecord.metrics.costUsd += Number(roundRecord.costUsd || 0);
      laneRecord.metrics.tokens.input += Number(roundRecord.usage.inputTokens || 0);
      laneRecord.metrics.tokens.output += Number(roundRecord.usage.outputTokens || 0);
      laneRecord.metrics.tokens.total += Number(roundRecord.usage.totalTokens || 0);

      emitEvent('round_completed', {
        runId,
        lane: laneId,
        round,
        totalRounds: queries.length,
        isError: roundRecord.isError,
        sessionId: roundRecord.sessionId,
        latestScreenshot: roundRecord.latestScreenshot,
        assistantText: String(turn.text || ''),
      });
    }

    if (evolutionEnabled) {
      const finalDrain = await waitForEvolutionDrain(evolutionManager, emitEvent, {
        runId,
        lane: laneId,
        round: queries.length,
      });
      laneRecord.metrics.evolutionWaitMs += Number(finalDrain.waitDurationMs || 0);
    }

    laneRecord.status = 'completed';
  } catch (err) {
    laneRecord.status = 'failed';
    laneRecord.error = err && err.message ? err.message : String(err);
    throw err;
  } finally {
    laneRecord.metrics.laneEndMs = Date.now();
    laneRecord.metrics.laneDurationMs = laneRecord.metrics.laneEndMs - laneRecord.metrics.laneStartMs;
    const toolsAfter = snapshotLearnedTools(learnedToolStore);
    const skillsAfter = snapshotLearnedSkills(skillRegistry);
    const toolDelta = countCreatedAndUpdated(toolsBefore, toolsAfter);
    const skillDelta = countCreatedAndUpdated(skillsBefore, skillsAfter);
    laneRecord.metrics.artifacts.learnedToolsCreated = toolDelta.created;
    laneRecord.metrics.artifacts.learnedToolsUpdated = toolDelta.updated;
    laneRecord.metrics.artifacts.learnedSkillsCreated = skillDelta.created;
    laneRecord.metrics.artifacts.learnedSkillsUpdated = skillDelta.updated;

    laneRecord.finishedAt = nowIso();
    laneRecord.summary = {
      roundsTotal: queries.length,
      roundsCompleted: laneRecord.rounds.length,
      roundsErrored: laneRecord.rounds.filter((r) => r.isError).length,
      learnedToolsCount: learnedToolStore.list().length,
      learnedSkillsCount: countLearnedSkills(skillRegistry),
      evolutionEnabled,
      totalDurationMs: laneRecord.metrics.laneDurationMs,
      totalCostUsd: laneRecord.metrics.costUsd,
      totalInputTokens: laneRecord.metrics.tokens.input,
      totalOutputTokens: laneRecord.metrics.tokens.output,
      totalTokens: laneRecord.metrics.tokens.total,
      evolutionWaitMs: laneRecord.metrics.evolutionWaitMs,
      learnedToolsCreated: laneRecord.metrics.artifacts.learnedToolsCreated,
      learnedToolsUpdated: laneRecord.metrics.artifacts.learnedToolsUpdated,
      learnedSkillsCreated: laneRecord.metrics.artifacts.learnedSkillsCreated,
      learnedSkillsUpdated: laneRecord.metrics.artifacts.learnedSkillsUpdated,
    };

    emitEvent('lane_completed', {
      runId,
      lane: laneId,
      status: laneRecord.status,
      summary: laneRecord.summary,
      finishedAt: laneRecord.finishedAt,
    });
  }

  return laneRecord;
}

function summarizeRun(runRecord) {
  return {
    id: runRecord.id,
    status: runRecord.status,
    mode: runRecord.mode,
    createdAt: runRecord.createdAt,
    finishedAt: runRecord.finishedAt || null,
    presetId: runRecord.presetId,
    laneIds: (runRecord.lanes || []).map((l) => l.id),
    lanes: (runRecord.lanes || []).map((l) => ({
      id: l.id,
      status: l.status,
      summary: l.summary || null,
    })),
  };
}

function buildComparison(runRecord) {
  const baseline = (runRecord.lanes || []).find((l) => l.id === 'baseline');
  const evolution = (runRecord.lanes || []).find((l) => l.id === 'evolution');
  if (!baseline || !evolution) return null;

  return {
    baseline: {
      roundsCompleted: baseline.summary?.roundsCompleted || 0,
      roundsErrored: baseline.summary?.roundsErrored || 0,
      learnedToolsCount: baseline.summary?.learnedToolsCount || 0,
      learnedSkillsCount: baseline.summary?.learnedSkillsCount || 0,
    },
    evolution: {
      roundsCompleted: evolution.summary?.roundsCompleted || 0,
      roundsErrored: evolution.summary?.roundsErrored || 0,
      learnedToolsCount: evolution.summary?.learnedToolsCount || 0,
      learnedSkillsCount: evolution.summary?.learnedSkillsCount || 0,
    },
  };
}

function resolveQueries(body, presets) {
  const rawQueries = Array.isArray(body && body.queries) ? body.queries : null;
  const inlineQueries = rawQueries
    ? rawQueries.map((q) => String(q || '').trim()).filter(Boolean)
    : null;
  if (inlineQueries && inlineQueries.length > 0) {
    return {
      presetId: null,
      presetName: 'inline',
      queries: inlineQueries,
    };
  }

  const presetId = String((body && body.presetId) || 'default_5_rounds').trim();
  const preset = presets.find((p) => p.id === presetId) || presets[0] || null;
  if (!preset) {
    throw new Error('No demo presets are configured');
  }

  return {
    presetId: preset.id,
    presetName: preset.name,
    queries: preset.queries,
  };
}

function installDemoRoutes(app, deps) {
  const options = deps || {};
  const arenaRoot = options.arenaRoot || path.resolve(__dirname, '../..');
  const baseSystemPrompt = options.systemPrompt || '';
  const claudeBin = options.claudeBin || process.env.CLAUDE_BIN || 'claude';
  const unrealHost = options.unrealHost || process.env.UNREAL_HOST || '127.0.0.1';
  const unrealPort = options.unrealPort || process.env.UNREAL_PORT || '55559';
  const port = options.port || process.env.PORT || '3002';
  const model = options.model || process.env.CLAUDE_MODEL || null;
  const presetFile = options.presetFile || DEFAULT_PRESET_FILE;
  const arenaDataDir = path.join(arenaRoot, 'arena_data');

  const store = createDemoStore(arenaRoot, presetFile);

  app.get('/api/demo/presets', (req, res) => {
    res.json(store.getPresets());
  });

  app.get('/api/evolution/config', (req, res) => {
    res.json(readEvolutionConfig(arenaDataDir));
  });

  app.patch('/api/evolution/config', (req, res) => {
    const body = req.body || {};
    if (typeof body.enabled !== 'boolean') {
      return res.status(400).json({ error: 'enabled boolean required' });
    }
    const source = body.source != null ? String(body.source) : 'ui_toggle';
    res.json(writeEvolutionConfig(arenaDataDir, body.enabled, source));
  });

  app.get('/api/demo/runs', (req, res) => {
    const limit = Number(req.query.limit || 50);
    res.json(store.listRuns(limit));
  });

  app.get('/api/demo/runs/:id', (req, res) => {
    const run = store.loadRunRecord(req.params.id);
    if (!run) return res.status(404).json({ error: 'Run not found' });
    res.json(run);
  });

  app.post('/api/demo/runs', async (req, res) => {
    const sse = createSseWriter(res);
    const emit = (event, data) => sse.write(event, data);

    const rawMode = String((req.body && req.body.mode) || 'evolution').toLowerCase();
    const mode = rawMode === 'baseline' ? 'baseline' : 'evolution';

    const presets = store.getPresets();

    let runRecord = null;

    try {
      try {
        await setDemoCamera(unrealHost, Number(unrealPort));
        emit('camera_set', {
          location: DEMO_CAMERA.location,
          rotation: DEMO_CAMERA.rotation,
        });
      } catch (camErr) {
        emit('camera_set_failed', {
          message: camErr && camErr.message ? camErr.message : String(camErr),
        });
      }

      const selected = resolveQueries(req.body || {}, presets);
      const runId = createRunId();
      const createdAt = nowIso();
      const runDir = store.runDir(runId);

      const laneIds = mode === 'baseline' ? ['baseline'] : ['evolution'];

      runRecord = {
        id: runId,
        status: 'running',
        mode,
        presetId: selected.presetId,
        presetName: selected.presetName,
        queries: selected.queries,
        lanes: [],
        comparison: null,
        createdAt,
        finishedAt: null,
        runDir,
      };

      store.saveRunRecord(runId, runRecord);
      store.saveRunSummary(summarizeRun(runRecord));

      emit('run_started', {
        runId,
        mode,
        presetId: selected.presetId,
        queries: selected.queries,
        createdAt,
      });

      for (const laneId of laneIds) {
        const laneRecord = await runDemoLane({
          laneId,
          runId,
          runDir,
          queries: selected.queries,
          emitEvent: emit,
          claudeBin,
          baseSystemPrompt,
          arenaRoot,
          model,
          unrealHost,
          unrealPort,
          port,
        });

        runRecord.lanes.push(laneRecord);
        store.saveRunRecord(runId, runRecord);
        store.saveRunSummary(summarizeRun(runRecord));
      }

      runRecord.comparison = null;

      runRecord.status = 'completed';
      runRecord.finishedAt = nowIso();
      store.saveRunRecord(runId, runRecord);
      store.saveRunSummary(summarizeRun(runRecord));

      emit('run_completed', {
        runId,
        status: runRecord.status,
        finishedAt: runRecord.finishedAt,
      });
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      if (runRecord) {
        runRecord.status = 'failed';
        runRecord.finishedAt = nowIso();
        runRecord.error = message;
        store.saveRunRecord(runRecord.id, runRecord);
        store.saveRunSummary(summarizeRun(runRecord));
      }
      emit('run_failed', {
        runId: runRecord ? runRecord.id : null,
        message,
      });
    }

    res.end();
  });
}

module.exports = {
  installDemoRoutes,
};
