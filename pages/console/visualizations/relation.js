import { copy } from "../copy/catalog.js";
function relationList(nodes, edges) {
  const list = document.createElement("ul");
  list.className = "tavern-relation-list";
  const labels = new Map(nodes.map((node) => [node.key, node.label || node.name || copy("visualizations.relation.relationList.message.d873a0831c")]));
  for (const edge of edges.slice(0, 30)) {
    const item = document.createElement("li");
    const label = document.createElement("strong");
    label.textContent = copy("visualizations.relation.relationList.message.a4525d5ff6", {p0: labels.get(edge.source || edge.from), p1: edge.label || copy("visualizations.relation.relationList.message.20da58b8d0"), p2: labels.get(edge.target || edge.to)});
    const summary = document.createElement("span");
    summary.textContent = { forward: copy("visualizations.relation.relationList.message.bf4e1825e9"), backward: copy("visualizations.relation.relationList.message.3677053dcd"), both: copy("visualizations.relation.relationList.message.52904a7c88"), none: copy("visualizations.relation.relationList.message.9ad4aa01e0") }[edge.direction] || copy("visualizations.relation.relationList.message.eee8163550");
    item.append(label, summary);
    list.append(item);
  }
  if (!edges.length) {
    for (const node of nodes.slice(0, 12)) {
      const item = document.createElement("li");
      const label = document.createElement("strong");
      label.textContent = node.label || node.name || copy("visualizations.relation.relationList.message.d873a0831c");
      const summary = document.createElement("span");
      summary.textContent = copy("visualizations.relation.relationList.message.19837b9bbe");
      item.append(label, summary);
      list.append(item);
    }
  }
  return list;
}

function validRelationEdges(nodes, edges) {
  const keys = new Set(nodes.map((node) => node.key).filter(Boolean));
  return edges.filter((edge) => {
    const source = edge.source || edge.from;
    const target = edge.target || edge.to;
    return source && target && source !== target && keys.has(source) && keys.has(target);
  });
}

function relationGraph(nodes, edges) {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.classList.add("tavern-relation-graph");
  svg.setAttribute("viewBox", "0 0 360 260");
  svg.setAttribute("role", "img");
  const graphLabel = copy("visualizations.relation.relationGraph.message.5199177f1e");
  svg.setAttribute("aria-label", graphLabel);
  const title = document.createElementNS(namespace, "title");
  title.textContent = graphLabel;
  svg.append(title);
  const visible = nodes.slice(0, 12);
  const positions = new Map(visible.map((node, index) => [node.key, {
    x: 180 + Math.cos((Math.PI * 2 * index) / visible.length - Math.PI / 2) * 115,
    y: 130 + Math.sin((Math.PI * 2 * index) / visible.length - Math.PI / 2) * 90,
  }]));
  for (const edge of edges) {
    const start = positions.get(edge.source || edge.from);
    const end = positions.get(edge.target || edge.to);
    if (!start || !end) continue;
    const line = document.createElementNS(namespace, "line");
    line.setAttribute("x1", start.x); line.setAttribute("y1", start.y);
    line.setAttribute("x2", end.x); line.setAttribute("y2", end.y);
    line.setAttribute("data-state", ["known", "warning", "hostile"].includes(edge.state) ? edge.state : "known");
    svg.append(line);
  }
  for (const [index, node] of visible.entries()) {
    const position = positions.get(node.key);
    const group = document.createElementNS(namespace, "g");
    const circle = document.createElementNS(namespace, "circle");
    circle.setAttribute("cx", position.x); circle.setAttribute("cy", position.y); circle.setAttribute("r", 16);
    const label = document.createElementNS(namespace, "text");
    label.setAttribute("x", position.x); label.setAttribute("y", position.y + 4); label.textContent = String(index + 1);
    group.append(circle, label); svg.append(group);
  }
  return svg;
}

export function renderRelation({ nodes = [], edges = [] } = {}) {
  const safeNodes = [...new Map((Array.isArray(nodes) ? nodes : [])
    .filter((node) => node?.key)
    .map((node) => [node.key, node])).values()];
  const safeEdges = validRelationEdges(safeNodes, Array.isArray(edges) ? edges : []);
  const graphReady = safeNodes.length >= 3 && safeEdges.length > 0;
  const section = document.createElement("section");
  section.className = "tavern-relation-view";
  section.dataset.visualization = graphReady ? "relation-graph" : "relation-list";
  const heading = document.createElement("h3");
  heading.textContent = copy("visualizations.relation.renderRelation.message.79921dbfdd");
  section.setAttribute("aria-label", heading.textContent);
  section.append(heading);
  if (graphReady) section.append(relationGraph(safeNodes, safeEdges));
  const explanation = document.createElement("p");
  explanation.textContent = graphReady
    ? copy("visualizations.relation.renderRelation.message.8728e11bb2")
    : copy("visualizations.relation.renderRelation.message.3b5a8b8c61");
  section.append(explanation, relationList(safeNodes, safeEdges));
  return section;
}

export const renderRelationView = renderRelation;
