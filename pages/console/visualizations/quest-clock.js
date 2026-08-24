import { copy } from "../copy/catalog.js";
import { renderProgress } from "./progress.js";

function card(title, className) {
  const article = document.createElement("article");
  article.className = className;
  const heading = document.createElement("h4");
  heading.textContent = title;
  article.append(heading);
  return article;
}

export function renderQuestTracks(items = []) {
  const grid = document.createElement("section");
  grid.className = "tavern-quest-grid";
  grid.dataset.visualization = "quest-tracks";
  grid.setAttribute("aria-label", copy("visualizations.quest_clock.renderQuestTracks.message.e167e594ca"));
  for (const item of Array.isArray(items) ? items : []) {
    const article = card(item.label || item.title || copy("visualizations.quest_clock.renderQuestTracks.message.e167e594ca"), "tavern-quest-card");
    const objective = Array.isArray(item.current_objectives)
      ? item.current_objectives.filter(Boolean).join("；")
      : item.objective || item.summary || "";
    if (objective) {
      const summary = document.createElement("p");
      summary.textContent = objective;
      article.append(summary);
    }
    article.append(renderProgress({
      label: copy("visualizations.quest_clock.renderQuestTracks.label.d2f2242680"),
      current: item.current,
      total: item.total,
      segments: Array.isArray(item.segment_items) ? item.segment_items : [],
      state: item.state_label || "",
      detail: item.blocked_reason ? copy("visualizations.quest_clock.renderQuestTracks.message.d99b8cb7ff", {p0: item.blocked_reason}) : item.blocker ? copy("visualizations.quest_clock.renderQuestTracks.message.d99b8cb7ff", {p0: item.blocker}) : "",
    }));
    grid.append(article);
  }
  if (!grid.childElementCount) {
    const empty = document.createElement("p");
    empty.textContent = copy("visualizations.quest_clock.renderQuestTracks.message.128871fa4a");
    grid.append(empty);
  }
  return grid;
}

export function renderClocks(items = []) {
  const grid = document.createElement("section");
  grid.className = "tavern-clock-grid";
  grid.dataset.visualization = "clocks";
  grid.setAttribute("aria-label", copy("visualizations.quest_clock.renderClocks.message.a141861b0e"));
  for (const item of Array.isArray(items) ? items : []) {
    const article = card(item.label || item.title || copy("visualizations.quest_clock.renderClocks.message.a141861b0e"), "tavern-clock-card");
    const isSegmentClock = item.type === "segments" && Number(item.segments) > 0;
    const segmentCount = isSegmentClock
      ? Math.max(1, Math.min(24, Math.floor(Number(item.segments))))
      : 0;
    const completedSegments = isSegmentClock && Number.isFinite(Number(item.current))
      ? Math.max(0, Math.min(segmentCount, Math.floor(Number(item.current))))
      : null;
    const timeDetail = item.type === "time" && Number.isFinite(Number(item.remaining_seconds))
      ? copy("visualizations.quest_clock.renderClocks.message.3a4c61693e", {p0: Math.max(0, Number(item.remaining_seconds))})
      : "";
    article.append(renderProgress({
      label: copy("visualizations.quest_clock.renderClocks.label.6147192f8c"),
      current: isSegmentClock ? item.current : undefined,
      total: isSegmentClock ? item.segments : undefined,
      segments: completedSegments === null ? [] : Array.from({ length: segmentCount }, (_, index) => ({
        label: copy("visualizations.quest_clock.renderClocks.label.c81ad1e1da", {p0: index + 1}),
        state: index < completedSegments ? "ready" : "waiting",
      })),
      state: item.state_label || "",
      detail: timeDetail,
    }));
    const consequence = document.createElement("p");
    consequence.className = "tavern-clock-consequence";
    consequence.textContent = item.trigger_summary || item.threshold_summary || item.consequence || copy("visualizations.quest_clock.renderClocks.message.c62c51e645");
    article.append(consequence);
    grid.append(article);
  }
  if (!grid.childElementCount) {
    const empty = document.createElement("p");
    empty.textContent = copy("visualizations.quest_clock.renderClocks.message.fd4f475694");
    grid.append(empty);
  }
  return grid;
}

export const renderQuestClock = renderQuestTracks;
