const ICONS = Object.freeze({
  dashboard:'<path d="M4 13h6V4H4zm10 7h6V11h-6zM4 20h6v-3H4zm10-13h6V4h-6z"/>',
  sessions:'<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 9h8M8 13h5"/>', todo:'<path d="m5 12 4 4L19 6"/>', tendencies:'<path d="M4 15c3-8 5 5 8-3s5 5 8-3"/>', characters:'<circle cx="12" cy="8" r="3"/><path d="M5 20c1-5 3-7 7-7s6 2 7 7"/>',
  worlds:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>', designer:'<path d="m4 20 5-1 10-10-4-4L5 15zM13 7l4 4"/>', author_jobs:'<rect x="5" y="4" width="14" height="16" rx="2"/><path d="M8 9h8M8 13h8M8 17h5"/>', session_detail:'<path d="m8 5 11 7-11 7z"/>', memories:'<path d="M6 4h12v16H6zM9 8h6M9 12h6M9 16h4"/>',
  audit:'<path d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5"/>', health:'<path d="M4 12h4l2-5 4 10 2-5h4"/>', settings:'<circle cx="12" cy="12" r="3"/><path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1m0-12.8-2.1 2.1m-8.6 8.6-2.1 2.1"/>', modules:'<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9zM4 7.5l8 4.5 8-4.5M12 12v9"/>', about:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  menu:'<path d="M4 6h16M4 12h16M4 18h16"/>', home:'<path d="m3 11 9-8 9 8v10h-6v-6H9v6H3z"/>', story:'<path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3zM8 8h7M8 12h7"/>', user:'<circle cx="12" cy="8" r="3.5"/><path d="M4.5 21c.8-5 3.3-7.5 7.5-7.5s6.7 2.5 7.5 7.5"/>',
  shield:'<path stroke-width="1.6" d="M12 2 5 6v6c0 5 3 8 7 10 4-2 7-5 7-10V6l-7-4Zm0 5v10M8 11h8"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4m0-14.2-1.4 1.4M6.3 17.7l-1.4 1.4"/>', refresh:'<path d="M20 7v5h-5M4 17v-5h5M18.5 10A7 7 0 0 0 6 6.5L4 9m2 5a7 7 0 0 0 12 3.5L20 15"/>', plus:'<path d="M12 4v16M4 12h16"/>', bell:'<path d="M6 9a6 6 0 0 1 12 0v5l2 3H4l2-3zM10 20h4"/>', mark:'<path d="m4 13 5 5L20 6"/>',
  vote:'<path d="M5 4h14v16H5zM8 8h8M8 12h5M16 15l1.5 1.5L21 13"/>', clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', relationship:'<circle cx="7" cy="8" r="3"/><circle cx="17" cy="16" r="3"/><path d="M9.5 9.5 14.5 14.5"/>', ai_teammate:'<path d="M7 7h10v10H7zM9 3v4m6-4v4M3 10h4m10 0h4M10 11h.01M14 11h.01M10 14h4"/>',
  warning:'<path d="M12 3 22 21H2Zm0 6v5m0 3v.1"/>', error:'<path d="m7 7 10 10M17 7 7 17"/><circle cx="12" cy="12" r="9"/>', success:'<circle cx="12" cy="12" r="9"/><path d="m7.5 12 3 3 6-7"/>', retry:'<path d="M19 8a7 7 0 1 0 1 7M19 4v4h-4"/>', lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>', archive:'<path d="M4 7h16v13H4zM3 3h18v4H3zM9 11h6"/>', delivery:'<path d="M3 6h12v11H3zM15 10h3l3 3v4h-6zM7 20a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm10 0a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z"/>',
});

export function icon(name) {
  const ns = "http://www.w3.org/2000/svg"; const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true"); svg.setAttribute("focusable", "false"); svg.dataset.icon = ICONS[name] ? name : "unknown";
  svg.innerHTML = ICONS[name] || '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 17h.01"/>';
  svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "1.75"); svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round"); return svg;
}
