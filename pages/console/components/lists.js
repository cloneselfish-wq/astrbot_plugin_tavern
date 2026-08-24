import { copy } from "../copy/catalog.js";
import { createElement, testToken } from "./dom.js";
import { renderStatePanel } from "./empty-problem.js";
import { formatTimeField } from "./time.js";

export function renderDataList({
  id,
  columns = [],
  rows = [],
  rowActions = () => [],
  mobileLabels = {},
  pagination = null,
  emptyCopy = copy("components.primitives.renderDataList.message.70a40a534c"),
} = {}) {
  const section = createElement("section", {
    class: "tavern-data-list",
    "data-list-id": id,
    "data-testid": `tavern-list-${testToken(id, "list")}`,
  });
  if (!rows.length) {
    section.append(renderStatePanel({ phase: "empty", emptyCopy }));
    if (pagination) section.append(pagination);
    return section;
  }
  const preparedRows = rows.map((row) => ({ row, actions: rowActions(row) || [] }));
  const hasActions = preparedRows.some((entry) => entry.actions.length);
  section.dataset.hasActions = String(hasActions);
  const table = createElement("table");
  table.append(createElement("thead", {}, [createElement("tr", {}, [
    ...columns.map((column) => createElement("th", { scope: "col", text: column.label })),
    hasActions ? createElement("th", {
      scope: "col",
      text: copy("components.primitives.renderDataList.text.ed31fbb483"),
    }) : null,
  ])]));
  const tbody = createElement("tbody");
  for (const { row, actions } of preparedRows) {
    tbody.append(createElement("tr", {}, [
      ...columns.map((column) => createElement("td", {
        "data-label": mobileLabels[column.key] || column.label,
        text: formatTimeField(column.key, row[column.key] ?? ""),
      })),
      hasActions ? createElement("td", {
        "data-label": copy("components.primitives.renderDataList.message.ed31fbb483"),
      }, actions) : null,
    ]));
  }
  table.append(tbody);
  section.append(table);
  if (pagination) section.append(pagination);
  return section;
}
