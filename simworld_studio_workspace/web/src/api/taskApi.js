import { apiJson } from "./client.js";

export const taskQueryKeys = {
  all: ["tasksets"],
  lists: () => [...taskQueryKeys.all, "list"],
  detail: (taskSetId) => [...taskQueryKeys.all, "detail", taskSetId],
};

export function listTaskSets() {
  return apiJson("/tasksets");
}

export function getTaskSet(taskSetId) {
  return apiJson(`/tasksets/${encodeURIComponent(taskSetId)}`);
}

export function deleteTaskSet(taskSetId) {
  return apiJson(`/tasksets/${encodeURIComponent(taskSetId)}`, { method: "DELETE" });
}
