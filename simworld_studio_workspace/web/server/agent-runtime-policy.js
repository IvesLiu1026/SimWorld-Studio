'use strict';

const RESPONSE_LANGUAGE_RULES = [
  'All user-visible chat, progress, and status text must be in English unless the user explicitly asks for another language.',
  'Understand non-English user requests, but do not mirror the user language automatically.',
  'Keep tool names, actor names, code, and log markers as exact ASCII identifiers.',
];

const PYTHON_SCRIPT_BATCHING_RULES = [
  'Prefer normal MCP tools for simple spawning, transforms, screenshots, verification, and single-actor edits.',
  'Use execute_python_script only for UE API work that normal tools cannot express, or for carefully scoped bulk edits.',
  'For scene construction, plan sequential phases: clear/setup, major layout, buildings, props, vegetation/vehicles/agents, then validation/save.',
  'Keep each execute_python_script batch to roughly 6-12 actors or operations; use fewer when assets are heavy or logic is complex.',
  'End every script with print("[DONE] <batch_name> count=<n>") on success, and catch exceptions so failures print "[ERROR] <batch_name>: <message>".',
  'After each batch, read log_path until it contains [DONE] or [ERROR], inspect the result, and only then continue with the next batch.',
  'Never generate one giant Python script for an entire large scene or full task pipeline.',
];

const DEFAULT_HARNESS_BEHAVIORS = [
  {
    id: 'batch_python_scripts',
    label: 'Batch Python scripts',
    enabledByDefault: true,
    prompt:
      'Use small, verifiable execute_python_script batches instead of one large script whenever automation is required.',
  },
  {
    id: 'screenshot_after_scene_edit',
    label: 'Screenshot after scene edits',
    enabledByDefault: true,
    prompt:
      'After meaningful scene edits, capture a screenshot or otherwise expose a visual result to the UI.',
  },
];

const OPTIONAL_HARNESS_BEHAVIORS = [
  {
    id: 'verify_after_batch',
    label: 'Verifier loop after batches',
    enabledByDefault: false,
    prompt:
      'When enabled, run verify_scene after substantial construction batches and use verifier feedback to decide the next batch.',
  },
  {
    id: 'context_snapshot_before_replan',
    label: 'Context snapshot before replanning',
    enabledByDefault: false,
    prompt:
      'When enabled, refresh scene context before replanning so agent decisions are grounded in live UE state.',
  },
];

function linesForBatchingRules() {
  return ['## PYTHON SCRIPT BATCHING', ...PYTHON_SCRIPT_BATCHING_RULES.map((rule) => `- ${rule}`)];
}

function linesForResponseLanguageRules() {
  return ['## RESPONSE LANGUAGE', ...RESPONSE_LANGUAGE_RULES.map((rule) => `- ${rule}`)];
}

function buildHarnessBehaviorLines({ includeOptional = [] } = {}) {
  const enabledOptional = new Set(includeOptional);
  const enabled = [
    ...DEFAULT_HARNESS_BEHAVIORS,
    ...OPTIONAL_HARNESS_BEHAVIORS.filter((behavior) => enabledOptional.has(behavior.id)),
  ];

  return [
    '## HARNESS BEHAVIOR CONTRACT',
    '- Default behaviors are mandatory unless the user explicitly asks otherwise.',
    '- Optional behaviors may be enabled by future UI/runtime toggles without changing each runner prompt.',
    ...enabled.map((behavior) => `- ${behavior.id}: ${behavior.prompt}`),
  ];
}

function buildSceneAgentRuntimeAppendix(options = {}) {
  return [
    ...linesForResponseLanguageRules(),
    '',
    ...linesForBatchingRules(),
    '',
    ...buildHarnessBehaviorLines(options),
  ].join('\n');
}

function buildPanelAgentRuleLines({ agentName }) {
  return [
    '## Response Language',
    ...RESPONSE_LANGUAGE_RULES.map((rule) => `- ${rule}`),
    '',
    '## Rules',
    `- Always use agent_name="${agentName}"`,
    '- Only control YOUR agent.',
    '- Think step by step: observe -> think -> act -> verify.',
    ...PYTHON_SCRIPT_BATCHING_RULES.map((rule) => `- ${rule}`),
    '- Be concise.',
    '',
    ...buildHarnessBehaviorLines(),
  ];
}

module.exports = {
  DEFAULT_HARNESS_BEHAVIORS,
  OPTIONAL_HARNESS_BEHAVIORS,
  PYTHON_SCRIPT_BATCHING_RULES,
  RESPONSE_LANGUAGE_RULES,
  buildHarnessBehaviorLines,
  buildPanelAgentRuleLines,
  buildSceneAgentRuntimeAppendix,
  linesForBatchingRules,
  linesForResponseLanguageRules,
};
