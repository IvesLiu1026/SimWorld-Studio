const LEFT_PANEL_BY_MODE = {
  scene: "chat",
  task: "taskgen",
  training: "trainconfig",
  coevolve: "curriculum",
};

const RIGHT_PANEL_BY_MODE = {
  scene: "sceneinsp",
  task: "taskinsp",
  training: "agentmonitor",
  coevolve: "roundinsp",
};

const LEFT_PANEL_META = {
  chat: { icon: "clipboard", title: "Scene Specification" },
  taskgen: { icon: "target", title: "Task Parameters" },
  trainconfig: { icon: "activity", title: "Run Configuration" },
  curriculum: { icon: "refresh", title: "Curriculum Configuration" },
};

const RIGHT_PANEL_META = {
  sceneinsp: { icon: "scan", title: "Scene Validation" },
  taskinsp: { icon: "check", title: "Task Set" },
  agentmonitor: { icon: "activity", title: "Run Monitor" },
  roundinsp: { icon: "chartBar", title: "Round Review" },
};

const DRAWER_TABS_BY_MODE = {
  scene: [
    { id: "assets", label: "Assets" },
    { id: "scenes", label: "Revisions" },
    { id: "context", label: "Operation Log" },
    { id: "vista_import", label: "VISTA Import" },
  ],
  task: [
    { id: "assets", label: "Task Sets" },
    { id: "scenes", label: "Episodes" },
    { id: "context", label: "Validation" },
  ],
  training: [
    { id: "assets", label: "Episodes" },
    { id: "scenes", label: "Trajectories" },
    { id: "context", label: "Metrics" },
  ],
  coevolve: [
    { id: "assets", label: "Rounds" },
    { id: "scenes", label: "Difficulty" },
    { id: "context", label: "Rules" },
  ],
};

export function getStudioPanels(studioMode) {
  return {
    leftPanel: LEFT_PANEL_BY_MODE[studioMode] || LEFT_PANEL_BY_MODE.scene,
    rightPanel: RIGHT_PANEL_BY_MODE[studioMode] || RIGHT_PANEL_BY_MODE.scene,
  };
}

export function getLeftPanelMeta(leftPanel) {
  return LEFT_PANEL_META[leftPanel] || LEFT_PANEL_META.chat;
}

export function getRightPanelMeta(rightPanel) {
  return RIGHT_PANEL_META[rightPanel] || RIGHT_PANEL_META.sceneinsp;
}

export function getDrawerTabs(studioMode) {
  return DRAWER_TABS_BY_MODE[studioMode] || DRAWER_TABS_BY_MODE.scene;
}
