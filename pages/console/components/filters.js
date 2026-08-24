import { copy } from "../copy/catalog.js";
import { renderButton } from "./buttons.js";
import { createElement, testToken } from "./dom.js";

function renderFilterControl(field, value) {
  const fieldToken = testToken(field.name, "filter");
  const common = {
    class: "tavern-control",
    name: field.name,
    value,
    required: field.required,
    disabled: field.disabled,
    "aria-describedby": field.hint ? `${fieldToken}-hint` : undefined,
  };
  if (field.type === "select" || field.type === "enum") {
    if ((field.options || []).length > 12) {
      const listId = `tavern-filter-${fieldToken}-options`;
      const input = createElement("input", {
        ...common,
        type: "search",
        list: listId,
        role: "combobox",
        "aria-autocomplete": "list",
      });
      const list = createElement("datalist", { id: listId }, (field.options || []).map((option) => {
        const optionValue = typeof option === "object" ? option.value : option;
        const optionLabel = typeof option === "object" ? option.label : option;
        return createElement("option", { value: optionValue, label: optionLabel });
      }));
      return createElement("span", { class: "tavern-combobox" }, [input, list]);
    }
    const select = createElement("select", common);
    for (const option of field.options || []) {
      const optionValue = typeof option === "object" ? option.value : option;
      const optionLabel = typeof option === "object" ? option.label : option;
      select.append(createElement("option", {
        value: optionValue,
        text: optionLabel,
        selected: String(optionValue) === String(value),
      }));
    }
    return select;
  }
  return createElement("input", {
    ...common,
    type: field.type === "search" ? "search" : field.type === "integer" ? "number" : "text",
    min: field.minimum,
    max: field.maximum,
    inputmode: field.type === "integer" ? "numeric" : undefined,
  });
}

export function renderFilterBar({
  workspace,
  fields = [],
  values = {},
  activeChips = [],
  onApply,
  onClear,
} = {}) {
  const safeFields = Array.isArray(fields)
    ? fields.filter((field) => field && typeof field === "object" && field.name && field.label)
    : [];
  const safeValues = values && typeof values === "object" ? values : {};
  const safeChips = Array.isArray(activeChips) ? activeChips.filter(Boolean) : [];
  const form = createElement("form", {
    class: "tavern-filter-bar",
    "data-workspace": workspace,
    "data-testid": `tavern-filters-${workspace}`,
  });
  for (const field of safeFields) {
    const fieldToken = testToken(field.name, "filter");
    const control = renderFilterControl(field, safeValues[field.name] ?? field.default ?? "");
    const label = createElement("label", { class: "tavern-field" }, [
      createElement("span", { text: field.label }),
      control,
    ]);
    if (field.hint) label.append(createElement("small", { id: `${fieldToken}-hint`, text: field.hint }));
    form.append(label);
  }
  if (safeChips.length) {
    form.append(createElement("div", {
      class: "tavern-filter-chips",
      "aria-label": copy("components.primitives.renderFilterBar.message.f325d740ec"),
    }, safeChips.map((chip) => createElement("span", {
      class: "tavern-filter-chip",
      text: chip,
    }))));
  }
  form.append(
    renderButton({
      variant: "primary",
      label: copy("components.primitives.renderFilterBar.label.dd0a97ab36"),
      buttonType: "submit",
    }),
    renderButton({
      variant: "quiet",
      label: copy("components.primitives.renderFilterBar.label.bce2377283"),
      onActivate: () => {
        form.reset();
        if (typeof onClear === "function") onClear();
      },
    }),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (typeof onApply === "function") {
      onApply(Object.fromEntries(new FormData(form)));
    }
  });
  return form;
}
