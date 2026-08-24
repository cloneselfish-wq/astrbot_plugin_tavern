function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  element.className = className;
  if (text !== "") element.textContent = String(text);
  return element;
}

function inlineText(value) {
  return String(value || "")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1")
    .replace(/(`{1,3}|\*\*|__|~~)/g, "")
    .replace(/(^|\s)[*_]([^*_]+)[*_](?=\s|$)/g, "$1$2")
    .trim();
}

function isTableDivider(line) {
  const cells = line.trim().replace(/^\||\|$/g, "").split("|");
  return cells.length > 1 && cells.every((cell) => /^\s*:?-{3,}:?\s*$/.test(cell));
}

function tableCells(line) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => inlineText(cell));
}

function parseMarkdown(source) {
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let index = 0;
  while (index < lines.length) {
    const raw = lines[index];
    const line = raw.trim();
    if (!line) { index += 1; continue; }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: inlineText(heading[2]) });
      index += 1;
      continue;
    }
    if (line.startsWith(">")) {
      const values = [];
      while (index < lines.length && lines[index].trim().startsWith(">")) {
        values.push(inlineText(lines[index].trim().replace(/^>\s?/, "")));
        index += 1;
      }
      blocks.push({ type: "callout", text: values.filter(Boolean).join(" ") });
      continue;
    }
    const listMatch = /^(?:[-*+]\s+|\d+[.)]\s+)(.+)$/.exec(line);
    if (listMatch) {
      const ordered = /^\d/.test(line);
      const items = [];
      while (index < lines.length) {
        const current = lines[index].trim();
        const match = (ordered ? /^\d+[.)]\s+(.+)$/ : /^[-*+]\s+(.+)$/).exec(current);
        if (!match) break;
        items.push(inlineText(match[1]));
        index += 1;
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }
    if (line.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
      const headers = tableCells(line);
      const rows = [];
      index += 2;
      while (index < lines.length && lines[index].trim().includes("|")) {
        rows.push(tableCells(lines[index]));
        index += 1;
      }
      blocks.push({ type: "table", headers, rows });
      continue;
    }
    const paragraph = [inlineText(line)];
    index += 1;
    while (index < lines.length) {
      const next = lines[index].trim();
      if (!next || /^(#{1,6})\s+/.test(next) || next.startsWith(">")
          || /^(?:[-*+]\s+|\d+[.)]\s+)/.test(next)
          || (next.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1]))) break;
      paragraph.push(inlineText(next));
      index += 1;
    }
    blocks.push({ type: "paragraph", text: paragraph.filter(Boolean).join(" ") });
  }
  return blocks;
}

function renderTable(block) {
  const wrapper = node("div", "tavern-world-readme-table-wrap");
  const table = node("table", "tavern-world-readme-table");
  const head = node("thead");
  const headRow = node("tr");
  for (const value of block.headers) headRow.append(node("th", "", value));
  head.append(headRow);
  const body = node("tbody");
  for (const values of block.rows) {
    const row = node("tr");
    for (let index = 0; index < block.headers.length; index += 1) {
      row.append(node("td", "", values[index] || "—"));
    }
    body.append(row);
  }
  table.append(head, body);
  wrapper.append(table);
  return wrapper;
}

function renderBlock(block) {
  if (block.type === "callout") return node("aside", "tavern-world-readme-callout", block.text);
  if (block.type === "paragraph") return node("p", "tavern-world-readme-paragraph", block.text);
  if (block.type === "list") {
    const list = node(block.ordered ? "ol" : "ul", "tavern-world-readme-list");
    for (const value of block.items) list.append(node("li", "", value));
    return list;
  }
  if (block.type === "table") return renderTable(block);
  if (block.type === "heading") {
    const level = Math.max(4, Math.min(6, block.level + 2));
    return node(`h${level}`, "tavern-world-readme-subheading", block.text);
  }
  return null;
}

export function renderWorldReadmeDocument(source, { title = "世界设定" } = {}) {
  const blocks = parseMarkdown(source);
  const root = node("div", "tavern-world-readme-document");
  if (!blocks.length) {
    root.append(node("p", "tavern-world-readme-empty", "该章节没有可显示的公开内容。"));
    return root;
  }
  let section = null;
  const ensureSection = (heading = "章节概览") => {
    section = node("section", "tavern-world-readme-section");
    section.append(node("header", "", heading));
    root.append(section);
    return section;
  };
  for (const block of blocks) {
    if (block.type === "heading" && block.level <= 2) {
      const heading = block.text === title ? "章节导读" : block.text;
      ensureSection(heading);
      continue;
    }
    const rendered = renderBlock(block);
    if (rendered) (section || ensureSection()).append(rendered);
  }
  return root;
}

export { parseMarkdown as parseWorldReadmeMarkdown };
