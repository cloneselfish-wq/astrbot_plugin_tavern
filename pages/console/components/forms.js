import { copy } from "../copy/catalog.js";
import { createElement, testToken } from "./dom.js";

export function renderFormField({
  name,
  label,
  value = "",
  error = "",
  hint = "",
  type = "text",
  required = false,
  readonly = false,
  disabled = false,
  accept = "",
  min,
  max,
  step,
  options = [],
} = {}) {
  const token = testToken(name, "field");
  const errorId = `tavern-field-${token}-error`;
  const hintId = `tavern-field-${token}-hint`;
  const describedBy = [hint ? hintId : "", error ? errorId : ""].filter(Boolean).join(" ");
  if (readonly) {
    const display = value === undefined
      ? copy("components.primitives.renderFormField.message.cdea037991")
      : value === null
        ? copy("components.primitives.renderFormField.message.756762e293")
        : String(value);
    const row = createElement("dl", {
      class: "tavern-definition-row",
      "data-readonly": "true",
    }, [createElement("div", {}, [
      createElement("dt", { text: label }),
      createElement("dd", { text: display }),
    ])]);
    if (hint) row.append(createElement("small", { id: hintId, text: hint }));
    return row;
  }
  let input;
  if (type === "textarea") {
    input = createElement("textarea", {
      class: "tavern-control",
      name,
      readonly,
      disabled,
      required,
      "aria-invalid": error ? "true" : "false",
      "aria-describedby": describedBy || undefined,
      text: value,
    });
  } else if (type === "select" && options.length > 12) {
    const listId = `tavern-field-${token}-options`;
    const control = createElement("input", {
      class: "tavern-control",
      name,
      type: "search",
      value,
      list: listId,
      role: "combobox",
      "aria-autocomplete": "list",
      disabled,
      required,
      "aria-invalid": error ? "true" : "false",
      "aria-describedby": describedBy || undefined,
    });
    const list = createElement("datalist", { id: listId }, options.map((option) => {
      const optionValue = typeof option === "object" ? option.value : option;
      const optionLabel = typeof option === "object" ? option.label : option;
      return createElement("option", { value: optionValue, label: optionLabel });
    }));
    input = createElement("span", { class: "tavern-combobox" }, [control, list]);
  } else if (type === "select") {
    input = createElement("select", {
      class: "tavern-control",
      name,
      disabled,
      required,
      "aria-invalid": error ? "true" : "false",
      "aria-describedby": describedBy || undefined,
    }, options.map((option) => {
      const optionValue = typeof option === "object" ? option.value : option;
      const optionLabel = typeof option === "object" ? option.label : option;
      return createElement("option", {
        value: optionValue,
        selected: String(optionValue) === String(value),
        text: optionLabel,
      });
    }));
  } else if (["boolean", "checkbox"].includes(type)) {
    input = createElement("input", {
      class: "tavern-control tavern-checkbox",
      name,
      type: "checkbox",
      checked: Boolean(value),
      disabled,
      required,
      "aria-invalid": error ? "true" : "false",
      "aria-describedby": describedBy || undefined,
    });
  } else {
    input = createElement("input", {
      class: "tavern-control",
      name,
      type,
      value: type === "file" ? undefined : value,
      accept: type === "file" ? accept || undefined : undefined,
      min: type === "number" ? min : undefined,
      max: type === "number" ? max : undefined,
      step: type === "number" ? step : undefined,
      inputmode: type === "number" && Number(step) > 0 && Number(step) < 1
        ? "decimal"
        : undefined,
      readonly,
      disabled,
      required,
      "aria-invalid": error ? "true" : "false",
      "aria-describedby": describedBy || undefined,
    });
  }
  const wrapper = createElement("label", {
    class: "tavern-form-field tavern-field",
    "data-readonly": String(Boolean(readonly)),
  }, [createElement("span", { text: label }), input]);
  if (hint) wrapper.append(createElement("small", { id: hintId, text: hint }));
  if (error) {
    wrapper.append(createElement("span", {
      id: errorId,
      class: "tavern-field-error",
      role: "alert",
      text: error,
    }));
  }
  return wrapper;
}
